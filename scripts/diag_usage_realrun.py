"""用量台账真跑自检（ADR 0025 P1）：真发模型请求，验 usage 真的按正确口径落进 usage.db。

**为什么必须真跑**：P1 的单测用的是造出来的 usage 对象，只能证明"给定这个形状我算得对"，
证明不了"真端点回传的就是这个形状"。本项目栽过同类跟头（mcp SDK 2.0 改字段名，纯 mock 全绿、
真 server 全挂）。这里走**完整链路**：真 provider → AgentLoop → Conversation 的 emit 咽喉 → 落库。

用法（无默认值，照 diag_*_realrun.py 的约定）：
    HERMES_RT_BASE=https://api.deepseek.com/anthropic \
    HERMES_RT_KEY_ENV=DEEPSEEK_API_KEY \
    HERMES_RT_MODEL=deepseek-v4-flash \
    python scripts/diag_usage_realrun.py

产生真实费用（一轮小请求，量很小）。库写到临时目录，不碰 data/usage.db。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _env_or_die() -> tuple:
    base = os.environ.get("HERMES_RT_BASE")
    key_env = os.environ.get("HERMES_RT_KEY_ENV")
    model = os.environ.get("HERMES_RT_MODEL")
    # 两条 provider 路径的用量口径**完全不同**（anthropic 的 input 已排除缓存，
    # OpenAI 系的 prompt_tokens 却含缓存），所以两条都要真验，不能只验一条就当都对。
    kind = (os.environ.get("HERMES_RT_PROVIDER") or "anthropic").strip().lower()
    if not (base and key_env and model):
        print("需要 HERMES_RT_BASE / HERMES_RT_KEY_ENV / HERMES_RT_MODEL（无默认）。", file=sys.stderr)
        sys.exit(2)
    if kind not in ("anthropic", "openai"):
        print(f"HERMES_RT_PROVIDER 只能是 anthropic / openai，收到 {kind}", file=sys.stderr)
        sys.exit(2)
    if not os.environ.get(key_env):
        print(f"环境变量 {key_env} 没有值（.env 里配好或 export）。", file=sys.stderr)
        sys.exit(2)
    return base, key_env, model, kind


def run(prompt: str, tmp: Path) -> tuple[list, Path]:
    """跑一轮真实对话，返回 (事件流, usage.db 路径)。"""
    from agentcore.bridge.api import Api
    from agentcore.config import ModelConfig, load_config

    base, key_env, model, kind = _env_or_die()
    cwd = os.getcwd()
    os.chdir(ROOT)                     # load_config 读项目根 config.yaml / .env
    try:
        cfg = load_config()
    finally:
        os.chdir(cwd)

    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "hello.txt").write_text("四十二\n", encoding="utf-8")

    cfg.models["rt"] = ModelConfig(provider=kind, model=model,
                                   api_key_env=key_env, base_url=base, max_tokens=1024)
    cfg.active_model = "rt"
    cfg.agent.workspace = str(ws)
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.auto_conventions = False
    cfg.agent.screenshot = False
    cfg.memory.enabled = False
    cfg.mcp.enabled = False
    cfg.storage.db_path = str(tmp / "sessions.db")
    usage_db = tmp / "usage.db"
    cfg.usage.db_path = str(usage_db)   # 不碰真库

    events: list = []

    def fake_emit(self, event, data, cid=None):  # noqa: ANN001
        events.append((event, data))

    orig = Api._emit
    Api._emit = fake_emit
    api = None
    try:
        api = Api(cfg)
        api.active.gate._allow_all = True
        ret = api.active.send_message(prompt)
        if not ret.get("ok"):
            print(f"❌ 对话失败：{ret.get('error')}", file=sys.stderr)
            sys.exit(1)
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass
        Api._emit = orig
    return events, usage_db


def main() -> int:
    _, _, model, kind = _env_or_die()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print(f"=== provider 路径：{kind}  模型：{model} ===")
        # 让它读一个文件：制造 ≥2 步（工具往返），从而验证跨步累加
        events, usage_db = run("读一下工作区里的 hello.txt，把内容原样告诉我。", tmp)

        emitted = [d for e, d in events if e == "usage"]
        print(f"\n--- emit 的 usage 事件（{len(emitted)} 条）---")
        for u in emitted:
            print("   ", u)

        rows = []
        if usage_db.exists():
            con = sqlite3.connect(str(usage_db))
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute("SELECT * FROM usage_log ORDER BY id")]
            con.close()
        print(f"\n--- usage.db 落库（{len(rows)} 行）---")
        for r in rows:
            print("   ", {k: r[k] for k in (
                "session_id", "provider", "model_id", "model_profile", "agent_role",
                "input_uncached", "input_cache_write", "input_cache_read", "output",
                "measured", "steps", "harness_version")})

        checks: list[tuple[str, bool, str]] = []
        ok = bool(rows)
        checks.append(("落库至少一行", ok, f"{len(rows)} 行"))
        if ok:
            r = rows[0]
            checks.append((f"provider 归一为 {kind}", r["provider"] == kind, str(r["provider"])))
            checks.append(("model_id 是真实模型名（不是档名）", r["model_id"] == model, str(r["model_id"])))
            checks.append(("model_profile 记的是档名", r["model_profile"] == "rt", str(r["model_profile"])))
            checks.append(("真端点回传用量 → measured=1", r["measured"] == 1, str(r["measured"])))
            checks.append(("输入 token > 0", r["input_uncached"] > 0, str(r["input_uncached"])))
            checks.append(("输出 token > 0", r["output"] > 0, str(r["output"])))
            checks.append(("绑到了会话", r["session_id"] is not None, str(r["session_id"])))
            checks.append(("记了 harness 版本", bool(r["harness_version"]), str(r["harness_version"])))
            checks.append(("agent_role=main", r["agent_role"] == "main", str(r["agent_role"])))
            # 落库总量必须等于 emit 的总量——中间少一环就是账丢了
            if emitted:
                same = (sum(r["input_uncached"] for r in rows) == sum(u.get("input", 0) for u in emitted)
                        and sum(r["output"] for r in rows) == sum(u.get("output", 0) for u in emitted))
                checks.append(("落库总量 == emit 总量", same, ""))
            # 缓存字段：真端点未必命中，但列必须存在且非负（不能是 None）
            cache_ok = all(r[c] is not None and r[c] >= 0
                           for r in rows for c in ("input_cache_read", "input_cache_write"))
            checks.append(("缓存读/写列有值（可为 0）", cache_ok, ""))

        print("\n--- 检查 ---")
        bad = 0
        for name, passed, detail in checks:
            print(f"  {'✅' if passed else '❌'} {name}" + (f"  （{detail}）" if detail else ""))
            bad += 0 if passed else 1
        print(f"\n{len(checks) - bad}/{len(checks)} 通过")
        return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
