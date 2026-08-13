"""方案评审 分批评审(②) + verdict 救援(③) 的**真模型**自测。

用法（项目根目录，需要能联网 + `.env` 里有对应 key）：

    HERMES_RT_BASE=https://api.deepseek.com/anthropic HERMES_RT_KEY_ENV=DEEPSEEK_API_KEY \
      HERMES_RT_MODEL=deepseek-chat python scripts/diag_review_batch_rescue.py

**刻意不预设某一家 provider**（同 diag_handoff_realrun）：三个环境变量必须给全，否则明确报错。
任何 anthropic 兼容端点都行。

为什么必须真跑：这两件事的失败模式**恰恰是 mock 兜不住的那一类**——
- 分批的风险不是"切没切开"（纯函数已单测），而是**真模型拿到子集后会不会误判"方案缺了东西"**、
  会不会替其它批次的决策表态。只有真模型会犯这个错。
- 救援的风险不是"能不能拼接 JSON"（纯函数已单测），而是**真模型在被截断后、拿到回喂的散文，
  能不能真的只吐 JSON**（而不是又写一遍散文、又被截断）。

四条通过标准：
1. 分批真的发生，且**每条决策都出现在某一批的评审提示词里**（覆盖，不漏）；
2. 分批的范围说明真的注入了（告知镜头其余决策在别的批次，别替它们表态）；
3. 人为把主模型预算压到极小 → verdict 被截断 → **救援调用把决定救回来**（状态真的变了）；
4. 救援只在需要时发（正常路径零额外调用）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

from agentcore.agent.design_review import (  # noqa: E402
    MAIN, OPEN, REVIEW_BATCH_SIZE, REVIEWERS, Decision,
    batch_decisions, make_review_fn, run_review,
)
from agentcore.config import ModelConfig  # noqa: E402
from agentcore.providers.anthropic_p import AnthropicProvider  # noqa: E402

PASS, FAIL = "✅", "❌"
_results: list = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label, detail))
    print(f"  {PASS if ok else FAIL} {label}" + (f"\n       {detail}" if detail else ""))
    return bool(ok)


def _env_or_die() -> tuple:
    base = os.environ.get("HERMES_RT_BASE")
    key_env = os.environ.get("HERMES_RT_KEY_ENV")
    model = os.environ.get("HERMES_RT_MODEL")
    if not (base and key_env and model):
        print("需要 HERMES_RT_BASE / HERMES_RT_KEY_ENV / HERMES_RT_MODEL 三个环境变量（无默认）。",
              file=sys.stderr)
        sys.exit(2)
    if not os.environ.get(key_env):
        print(f"环境变量 {key_env} 没有值（.env 里配好或 export）。", file=sys.stderr)
        sys.exit(2)
    return base, key_env, model


class LoggingReviewFn:
    """包住 make_review_fn 的产物，记录每次真实调用的 (name, prompt)。

    **必须把 `.scope` 透传给内层**：`run_review` 每批都会往传进来的对象上设 `.scope`，
    而 `make_review_fn` 读的是它自己那个闭包函数的属性——不透传的话按批伸缩预算就失效了。
    """

    def __init__(self, inner, log: list) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "log", log)
        object.__setattr__(self, "base_max_tokens", getattr(inner, "base_max_tokens", 0))

    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)
        if k == "scope":
            setattr(self._inner, k, v)

    def __call__(self, name, prompt):
        out = self._inner(name, prompt)
        self.log.append((name, prompt, out))     # 连**输出**一起记：越批表态只能从输出里看出来
        return out


def make_fn(max_tokens: int, main_max_tokens=None) -> tuple:
    base, key_env, model = _env_or_die()
    mc = ModelConfig(provider="anthropic", model=model, api_key_env=key_env,
                     base_url=base, max_tokens=max(max_tokens, main_max_tokens or 0, 1024))
    provider = AnthropicProvider(model=mc.model, api_key=os.environ[key_env],
                                 max_tokens=mc.max_tokens, base_url=mc.base_url)
    inner = make_review_fn(lambda name: provider, max_tokens=max_tokens,
                           main_max_tokens=main_max_tokens)
    log: list = []
    return LoggingReviewFn(inner, log), log


def _plan_decisions() -> list:
    """一份 12 条决策的真实感方案（> REVIEW_BATCH_SIZE，必然分批）。"""
    raw = [
        ("d1", "数据存储", "SQLite", "本地单机够用；上云要换"),
        ("d2", "前端框架", "原生 JS", "零构建；组件复用差"),
        ("d3", "鉴权方式", "本地密码", "简单；不支持多端"),
        ("d4", "任务队列", "线程池", "无外部依赖；重启丢任务"),
        ("d5", "配置格式", "YAML", "可读；无 schema 校验"),
        ("d6", "日志方案", "标准库 logging", "够用；无结构化查询"),
        ("d7", "打包分发", "zip 源码包", "解压即用；用户要自己装依赖"),
        ("d8", "错误上报", "本地文件", "隐私好；拿不到线上现场"),
        ("d9", "插件机制", "目录约定", "简单；无版本管理"),
        ("d10", "测试策略", "独立 runner", "零依赖；无并行与覆盖率"),
        ("d11", "国际化", "暂不做", "省事；后期改动大"),
        ("d12", "更新方式", "手动下载", "可控；用户流失在更新环节"),
    ]
    return [Decision(i, t, c, alternatives=[{"choice": "另选方案", "tradeoff": tr}],
                     rationale=tr, status=OPEN) for i, t, c, tr in raw]


def test_batching_covers_every_decision():
    print("\n[1] 分批评审：真模型下每条决策都被看到")
    ds = _plan_decisions()
    batches = batch_decisions(ds)
    fn, log = make_fn(max_tokens=1536)
    res = run_review(ds, fn, max_rounds=2, timeout=240, main_timeout=240)

    reviewer_calls = [(p, o) for n, p, o in log if n != MAIN]
    reviewer_prompts = [p for p, _o in reviewer_calls]
    check(len(reviewer_prompts) == len(batches) * len(REVIEWERS),
          f"评审员调用数 = 批次 {len(batches)} × 镜头 {len(REVIEWERS)}",
          f"实际 {len(reviewer_prompts)} 次")
    joined = "\n".join(reviewer_prompts)
    missing = [d.id for d in ds if f"id={d.id}" not in joined]
    check(not missing, "每条决策都进过某一批的评审提示词（覆盖不漏）",
          f"漏掉：{missing}" if missing else f"{len(ds)} 条全覆盖")
    check(reviewer_prompts and "第 1 批" in reviewer_prompts[0],
          "范围说明已注入（明确告知本批只评哪几条、别替其余表态）")
    # **分批最该验的定性风险**：真模型拿到子集后，会不会对不属于本批的决策下结论？
    # 纯函数测不出来——只有真模型会犯。逐批比对该批评审输出里出现的 id 与本批 id。
    from agentcore.agent.design_review import _first_json
    stray = []
    for bi, batch in enumerate(batches):
        own = {d.id for d in batch}
        for _p, out in reviewer_calls[bi * len(REVIEWERS):(bi + 1) * len(REVIEWERS)]:
            seg = out or ""
            fi = seg.rfind("```json")
            if fi >= 0:
                rest = seg[fi + len("```json"):]
                end = rest.find("```")
                seg = rest if end < 0 else rest[:end]
            arr = _first_json(seg, "[", "]")
            for item in (arr if isinstance(arr, list) else []):
                if isinstance(item, dict) and item.get("id") and item["id"] not in own:
                    stray.append((bi + 1, item["id"]))
    check(not stray, "镜头**没有**对不属于本批的决策下结论（分批副作用已被范围说明压住）",
          f"越批表态：{stray}" if stray else f"{len(batches)} 批逐一核对，无越批")
    print(f"       stop_reason={res['stop_reason']}，未决 {res['gate']['blocking_count']}/{len(ds)}")
    decided = [(d.id, d.status) for d in res["decisions"] if d.status != OPEN]
    print(f"       已定状态 {len(decided)} 条：{decided[:6]}{' …' if len(decided) > 6 else ''}")


def test_rescue_recovers_truncated_verdict():
    print("\n[2] verdict 救援：把主模型预算压到 64 token，逼出截断")
    ds = _plan_decisions()[:3]
    fn, log = make_fn(max_tokens=512, main_max_tokens=64)
    res = run_review(ds, fn, max_rounds=2, timeout=240, main_timeout=240)

    main_calls = [p for n, p, _o in log if n == MAIN]
    rescue = [p for p in main_calls if "只输出 JSON" in p]
    check(len(rescue) >= 1, "主模型被截断后**发起了**救援调用",
          f"主模型调用 {len(main_calls)} 次，其中救援 {len(rescue)} 次")
    changed = [(d.id, d.status) for d in res["decisions"] if d.status != OPEN]
    check(bool(changed), "救援后决策状态**真的落地了**（不是全留在 Open）",
          f"已定：{changed}" if changed else "全部仍是 Open —— 救援没生效")


def test_no_rescue_on_healthy_path():
    print("\n[3] 正常路径零额外成本：预算充足时不该发救援调用")
    ds = _plan_decisions()[:3]
    fn, log = make_fn(max_tokens=2048, main_max_tokens=None)
    run_review(ds, fn, max_rounds=2, timeout=240, main_timeout=240)
    rescue = [p for n, p, _o in log if n == MAIN and "只输出 JSON" in p]
    check(not rescue, "预算充足时未发救援调用",
          f"意外发了 {len(rescue)} 次" if rescue else "0 次")


def main() -> int:
    base, _key_env, model = _env_or_die()
    print("方案评审 分批(②) + 救援(③) 真模型自测")
    print(f"端点：{base}  模型：{model}  批大小：{REVIEW_BATCH_SIZE}")
    test_batching_covers_every_decision()
    test_rescue_recovers_truncated_verdict()
    test_no_rescue_on_healthy_path()
    ok = sum(1 for r, _, _ in _results if r)
    print(f"\n{ok}/{len(_results)} 通过")
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
