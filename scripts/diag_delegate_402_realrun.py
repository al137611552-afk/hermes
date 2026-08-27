"""委派 × 模型 API 账户级错误 **真跑**验证（真起子 Agent、真出网搜索、真打模型端点）。

    python scripts/diag_delegate_402_realrun.py <模型档名>            # 段1+段2
    python scripts/diag_delegate_402_realrun.py <模型档名> --only 1   # 只跑不花钱的段1

单测只能证明"假 provider 回 error 事件时代码会走哪个分支"，证明不了真端点回 402/401 时
整条链路的样子。这里两段各盯一件事：

 1. **坏 key + 3 路并发子 Agent**（不花钱：401 立刻返回）。盯四件事：
    ① 报错文案说清"这是模型 API 的鉴权问题"并**指名模型档 @ 端点**（原来只有一行
       `APIStatusError: Error code: 402 - {...}`，人会一路误判成搜索配额用尽）；
    ② 账户级**不重试**（重试结果相同，白烧一个往返）；
    ③ 摘要带 `⚠【子任务未完成 · 模型调用失败】`——不带的话主 Agent 会把半截调研当完整结论；
    ④ `subagent_done.ok` 是 False。
 2. **真 key + 3 路并发子 Agent 真调研**（真花钱、真出网）。盯反面：正常路径不能被段1
    的改动带出误报——不许出现 ⚠、`ok` 必须是 True、三路都得真的并发跑起来。

委派本来就是并行发起的（`loop.py` 的 `_PARALLEL_CAP=4`），而交互里单发一次搜索是**单路小
请求**——两种形状在中转/网关那边可能走完全不同的计费判定。段2 复现的正是前一种形状。

任何一项判定失败整体返回非零。输出里会带端点主机名，贴出来前自己扫一眼。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.bridge import Api                     # noqa: E402
from agentcore.config import load_config             # noqa: E402

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> bool:
    CHECKS.append((bool(ok), text))
    print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    return bool(ok)


def _make_api(profile: str, tmp: Path):
    """真配置（读 config.yaml + user_models.yaml + .env），只把落盘/记忆/MCP 挪开或关掉。"""
    cfg = load_config()
    if profile not in cfg.models:
        raise SystemExit(f"没有模型档 {profile}；可用：{', '.join(cfg.models) or '（空）'}")
    cfg.active_model = profile
    cfg.agent.workspaces_root = str(tmp / "ws")
    cfg.agent.auto_conventions = False       # 关后台生成规范，免得额外烧钱
    cfg.agent.max_steps = 10                 # 子 Agent 防跑飞：真跑不需要它做完整调研
    cfg.agent.subagent_model = None          # 跟随主模型（与用户现场一致）
    cfg.storage.db_path = str(tmp / "h.db")
    cfg.memory.enabled = False
    cfg.mcp.enabled = False
    events: list[tuple[str, object]] = []
    lock = threading.Lock()

    def emit(event, data, cid=None):
        with lock:
            events.append((event, data))

    return Api(cfg, emit=emit), events, cfg


def _errors(events) -> list[str]:
    """收齐两条错误通道：主线的 `error`，和子 Agent 包在 `subagent_event` 里的那份。
    子 Agent 的错误**不走顶层 error**——只盯一条会漏掉整个委派链路的报错。"""
    out = []
    for e, d in events:
        if e == "error":
            out.append(str(d))
        elif e == "subagent_event" and isinstance(d, dict) and d.get("event") == "error":
            out.append(str(d.get("data", "")))
    return out


def _fan_out(conv, tasks: list[str]) -> dict[str, str]:
    """3 路并发 run_subagent——委派在真实回合里就是这个形状（同轮多个 delegate 并行）。"""
    out: dict[str, str] = {}
    lock = threading.Lock()

    def one(i, t):
        s = conv.run_subagent(t, role="researcher")
        with lock:
            out[f"sub{i}"] = s

    ths = [threading.Thread(target=one, args=(i, t), daemon=True)
           for i, t in enumerate(tasks)]
    t0 = time.time()
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout=600)
    print(f"  （3 路耗时 {time.time() - t0:.1f}s）")
    return out


TASKS = [
    "查清北京互联网法院网上立案的流程与所需材料清单，给出带来源的要点。",
    "查清杭州互联网法院诉讼全程在线的各阶段时间期限，给出带来源的要点。",
    "查清广州互联网法院的管辖范围与受理案件类型，给出带来源的要点。",
]


def section_bad_key(profile: str) -> None:
    print("\n■ 段 1：坏 key + 3 路并发子 Agent（不花钱：端点直接回 401/402）")
    with tempfile.TemporaryDirectory(prefix="deleg402_") as td:
        api, events, cfg = _make_api(profile, Path(td))
        mc = cfg.models[profile]
        host = (mc.base_url or "").split("//", 1)[-1].split("/", 1)[0]
        real = os.environ.get(mc.api_key_env, "")
        os.environ[mc.api_key_env] = "sk-hermes-diag-invalid-key"   # 故意打坏
        try:
            summaries = _fan_out(api.active, TASKS)
        finally:
            os.environ[mc.api_key_env] = real
        errs = _errors(events)
        subs = [d for e, d in events if e == "subagent_done"]
        blob = "\n".join(errs)
        print(f"  · 报错样本：{(errs[0][:220] + '…') if errs else '（没有 error 事件）'}")
        check(bool(errs), "模型侧错误有 error 事件冒出来（不是静默吞掉）")
        check("模型 API" in blob, "文案点明这是**模型 API** 的问题")
        check(("鉴权" in blob or "计费" in blob), "文案给出了分类（鉴权/计费）")
        check(bool(host) and host in blob, f"文案指名了端点主机（{host or '未配 base_url'}）")
        check("Firecrawl" in blob, "文案明确排除了搜索配额（这次误判的起点）")
        check("subagent_model" in blob, "文案提示委派可能用的是另一个模型档")
        check(len(subs) == 3, f"三路子 Agent 都有终态事件（实得 {len(subs)}）")
        check(all(d.get("ok") is False for d in subs), "subagent_done.ok 全是 False（如实上报失败）")
        marked = [s for s in summaries.values() if "子任务未完成" in s]
        check(len(marked) == 3, f"三份摘要都带「子任务未完成」标记（实得 {len(marked)}）")
        # 账户级不重试：重试会在事件里留下"自动重试一次"的痕迹
        check(not any("自动重试一次" in e for e in errs), "账户级错误没有白重试")


def section_real_key(profile: str) -> None:
    print("\n■ 段 2：真 key + 3 路并发子 Agent 真调研（真出网、真花钱）")
    with tempfile.TemporaryDirectory(prefix="deleg_ok_") as td:
        api, events, _ = _make_api(profile, Path(td))
        summaries = _fan_out(api.active, TASKS)
        errs = _errors(events)
        subs = [d for e, d in events if e == "subagent_done"]
        # 子事件是**两层**的：{"id":.., "event":"tool_use", "data":{"name":..}}，
        # 工具名在里层。只扒外层会数出 0 次搜索（第一次真跑就这么误报了一把）。
        searched = sum(1 for e, d in events
                       if e == "subagent_event" and isinstance(d, dict)
                       and d.get("event") == "tool_use"
                       and (d.get("data") or {}).get("name") == "web_search")
        for k, v in sorted(summaries.items()):
            print(f"  · {k}: {v[:110].replace(chr(10), ' ')}…")
        check(len(subs) == 3, f"三路子 Agent 都跑完（实得 {len(subs)}）")
        check(all(d.get("ok") is True for d in subs), "subagent_done.ok 全是 True（无误报）")
        check(not any("模型 API" in e for e in errs),
              f"没有冒出账户级误报（error 事件 {len(errs)} 条）")
        check(not any("子任务未完成" in s for s in summaries.values()), "摘要没有被误打未完成标记")
        check(searched >= 3, f"三路确实真搜了网（web_search 调用 {searched} 次）")
        if errs:
            print(f"  （其它 error 事件，仅供参考）{errs[0][:200]}")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    only = ""
    if "--only" in args:
        i = args.index("--only")
        only = args[i + 1]; del args[i:i + 2]
    if not args:
        raise SystemExit("用法：python scripts/diag_delegate_402_realrun.py <模型档名> [--only 1|2]")
    profile = args[0]
    if only in ("", "1"):
        section_bad_key(profile)
    if only in ("", "2"):
        section_real_key(profile)
    bad = [t for ok, t in CHECKS if not ok]
    print(f"\n{'='*60}\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 项通过")
    for t in bad:
        print(f"  FAIL  {t}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
