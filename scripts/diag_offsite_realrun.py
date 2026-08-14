"""站外协同真跑自检（ADR 0026 W1）：真模型 + 真进程，验"自己醒来 + 带着上下文接着干"。

    HERMES_RT_BASE=https://api.deepseek.com/anthropic \
    HERMES_RT_KEY_ENV=DEEPSEEK_API_KEY \
    HERMES_RT_MODEL=deepseek-v4-flash \
    python scripts/diag_offsite_realrun.py

**分开验两件事，因为它们会以完全不同的方式失败**：

- **A 机制**：进程退出 → 回投 → 会话真的起了第二轮，且模型**记得当初在等什么**。
  这条不依赖模型选不选参数（脚本直接把进程标成 notify_on_exit），验的是接线。
- **B 模型是否自发使用**：给一个自然的任务描述，看它选不选 `notify_on_exit`。
  **这条正是本项目栽过三次的地方**（trace_run / search_code / request_handoff 都是
  "机制建好了模型不用"）。B 不过不代表 W1 白做，但说明得去改提示词/描述，别自欺。

无 GUI 依赖。产生少量真实费用。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

results = []


def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def _env_or_die():
    base = os.environ.get("HERMES_RT_BASE")
    key_env = os.environ.get("HERMES_RT_KEY_ENV")
    model = os.environ.get("HERMES_RT_MODEL")
    if not (base and key_env and model and os.environ.get(key_env or "")):
        print("需要 HERMES_RT_BASE / HERMES_RT_KEY_ENV / HERMES_RT_MODEL（无默认）。", file=sys.stderr)
        sys.exit(2)
    return base, key_env, model


def build_api(tmp: Path):
    from agentcore.bridge.api import Api
    from agentcore.config import ModelConfig, load_config

    base, key_env, model = _env_or_die()
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        cfg = load_config()
    finally:
        os.chdir(cwd)

    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg.models["rt"] = ModelConfig(provider="anthropic", model=model,
                                   api_key_env=key_env, base_url=base, max_tokens=1024)
    cfg.active_model = "rt"
    cfg.agent.workspace = str(ws)
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.auto_conventions = False
    cfg.agent.screenshot = False
    cfg.memory.enabled = False
    cfg.mcp.enabled = False
    cfg.storage.db_path = str(tmp / "s.db")
    cfg.usage.db_path = str(tmp / "u.db")

    events: list = []
    lock = threading.Lock()

    def fake_emit(self, event, data, cid=None):  # noqa: ANN001
        with lock:
            events.append((time.time(), event, data))

    Api._emit = fake_emit
    api = Api(cfg)
    api.active.gate._allow_all = True
    return api, events, ws


def text_since(events, idx):
    return "".join(d for _, e, d in events[idx:] if e == "chunk" and isinstance(d, str))


def wait_turn(events, idx, timeout=120):
    """等一轮跑完：以该轮末尾的 usage 事件为界（loop 每轮末必发）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(e == "usage" for _, e, _ in events[idx:]):
            time.sleep(1.0)      # 让收尾的 chunk 落定
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        api, events, ws = build_api(tmp)
        conv = api.active
        try:
            # ---- A 机制：不依赖模型选参数，直接把进程标成 notify_on_exit ----
            print("\n=== A 机制：进程退出 → 自动起第二轮 ===")
            r = conv.send_message(
                "我要在后台跑一个构建。你先回一句「好的，我等它完成」就行，别做别的。")
            check("第一轮正常完成", bool(r.get("ok")), str(r.get("error", ""))[:80])
            mark = len(events)
            first = text_since(events, 0)
            print(f"  [第一轮回答] {first.strip()[:80]}")

            from agentcore.tools.shell import build_argv
            shell = "powershell" if os.name == "nt" else "bash"
            cmd = ("Start-Sleep -Seconds 3; Write-Host BUILD_OK_42" if os.name == "nt"
                   else "sleep 3; echo BUILD_OK_42")
            entry = conv.procs.start(build_argv(shell, cmd), str(ws), cmd)
            entry.notify_on_exit = True
            print(f"  [已起后台进程 #{entry.id}]，等它退出触发第二轮…")

            woke = wait_turn(events, mark, timeout=150)
            check("进程退出后**自动起了第二轮**（没人再输入）", woke)
            second = text_since(events, mark)
            print(f"  [第二轮回答] {second.strip()[:200]}")

            check("第二轮认出这是构建结果（带着上下文，不是复述「进程退出了」）",
                  ("BUILD_OK_42" in second) or ("构建" in second and "完成" in second),
                  second.strip()[:100])
            check("会话回到空闲", conv.state == "idle", conv.state)

            # ---- B 模型是否自发使用新参数 ----
            print("\n=== B 模型会不会自己用 notify_on_exit ===")
            mark2 = len(events)
            conv.send_message(
                "在后台跑 `sleep 5; echo DONE_B` 这条命令。我不想你反复回来查它好了没，"
                "你起完就可以结束这一轮，它跑完你再告诉我。")
            wait_turn(events, mark2, timeout=120)
            used = [d for _, e, d in events[mark2:]
                    if e == "tool_use" and isinstance(d, dict) and str(d.get("name", "")).startswith("run_")]
            params = [u.get("input", {}) for u in used]
            print(f"  [模型给的参数] {params}")
            check("模型自发用了 notify_on_exit（提示里有引导）",
                  any(p.get("notify_on_exit") or p.get("wait_until_success") for p in params),
                  str(params)[:120])

            # ---- B2 中性提问：不给任何暗示，看它还选不选 ----
            # **这条才是真信号**。上面那条提示里写了"我不想你反复回来查"，几乎等于把答案说了；
            # 本项目的 delegate_implicit 评测任务就是为防这种自欺而设（"精简 prompt 曾在此翻车"）。
            print("\n=== B2 中性提问（无任何暗示）===")
            mark3 = len(events)
            conv.send_message("帮我在后台跑一下 `sleep 5; echo DONE_C`，跑完把结果告诉我。")
            wait_turn(events, mark3, timeout=120)
            used3 = [d for _, e, d in events[mark3:]
                     if e == "tool_use" and isinstance(d, dict)
                     and str(d.get("name", "")).startswith("run_")]
            params3 = [u.get("input", {}) for u in used3]
            print(f"  [模型给的参数] {params3}")
            check("中性提问下仍选了通知式等待（这条才是真信号）",
                  any(p.get("notify_on_exit") or p.get("wait_until_success") for p in params3),
                  str(params3)[:120])
        finally:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass

    bad = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
