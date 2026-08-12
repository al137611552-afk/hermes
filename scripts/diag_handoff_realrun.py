"""ADR 0023 换手 真模型自测：撞上**只有人能做**的环节时，模型会不会主动 `request_handoff`，
交回后会不会**真的重新验证**（而不是相信"用户说做完了"）。

用法（项目根目录下，需要能联网 + `.env` 里有对应 key）：

    # 已在设置面板配好 provider 时，直接跑（用 config 里的 active_model）：
    python scripts/diag_handoff_realrun.py

    # 本检出没有 providers.yaml 时，用三个环境变量临时指一个端点（**无默认，必须给全**）：
    HERMES_RT_BASE=https://api.deepseek.com/anthropic HERMES_RT_KEY_ENV=DEEPSEEK_API_KEY \
      HERMES_RT_MODEL=deepseek-chat python scripts/diag_handoff_realrun.py

**刻意不预设某一家 provider**：预设一个用户没订阅的端点，跑不通时报错会指向假原因
（"模型不肯换手"其实是 400 InvalidSubscription）。任何能用的 anthropic 兼容端点都行。

场景（不依赖浏览器，纯本地文件模拟登录墙）：工作区里 `report.txt` 写着"需要登录后才能查看"。
harness 扮演那个"人"：收到 `handoff_request` 事件后**在另一条线程里**把文件换成真数据，
再 `resolve_handoff(done)`——正是真机上"人去登录了，然后点我做完了"。

三条通过标准（对应 ADR 0023 决策 1~3）：
1. 模型**主动**调了 `request_handoff`，而不是硬试或编一个数字；
2. `verify` 参数非空（结构性约束：请求换手时就得说清怎么确认它真成了）；
3. 换手交回后模型**又读了一次**现场（不是拿"用户说做完了"当结论），并答出真数据。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

from agentcore.config import load_config, ModelConfig    # noqa: E402
from agentcore.bridge.api import Api                      # noqa: E402

SECRET = "本季度营收 41.7 亿元"
events: list = []
done = threading.Event()
handoffs: list = []


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="handoff-realrun-"))
    ws = tmp / "proj"
    ws.mkdir()
    # 登录墙**必须带一个具体入口**：真实的登录墙从来不是"请登录"四个字，一定有个地方让你登。
    # 首跑（2026-08-13）时这里只写了"需要登录后才能查看"，模型的反应是拒绝换手并反问"报告系统
    # 地址是什么"——这是**对的**：`request_handoff(target=...)` 要求填真实目标，没有目标它编一个
    # 才是错的。fixture 欠缺目标，不是模型不会换手。
    locked = ("【未登录】这份季度报告需要登录后才能查看。\n"
              "登录入口：https://reports.example.com/login\n"
              "（登录成功后，本文件会自动更新为完整报告内容）\n")
    (ws / "report.txt").write_text(locked, encoding="utf-8")

    api_holder: list = []

    def unlock_and_resolve(req: dict) -> None:
        """扮演那个人：去"登录"（把文件换成真数据），然后点「我做完了」。"""
        time.sleep(0.5)
        (ws / "report.txt").write_text(f"【已登录】{SECRET}\n", encoding="utf-8")
        if api_holder:
            api_holder[0].resolve_handoff(int(req.get("id")), "done", "登录好了")

    def emit(kind, data, cid=None):
        events.append((kind, data))
        if kind == "tool_use":
            print(f"  [工具] {(data or {}).get('name')} {str((data or {}).get('input'))[:200]}")
        if kind == "handoff_request":
            handoffs.append(data)
            print(f"  [换手] {data}")
            threading.Thread(target=unlock_and_resolve, args=(data,), daemon=True).start()
        if kind == "error":
            print("  [error]", str(data)[:300]); done.set()
        if kind == "done":
            done.set()

    cfg = load_config()
    if not cfg.models:      # 本检出没有 providers.yaml：靠环境变量临时拼一个档案（key 从 .env 读）
        base, key_env, model = (os.environ.get("HERMES_RT_BASE"),
                                os.environ.get("HERMES_RT_KEY_ENV"),
                                os.environ.get("HERMES_RT_MODEL"))
        if not (base and key_env and model):
            print("没有可用的模型档案。要么在设置面板配好 provider，要么给全三个环境变量：\n"
                  "  HERMES_RT_BASE / HERMES_RT_KEY_ENV / HERMES_RT_MODEL\n"
                  "（刻意不预设默认端点——预设一个你没订阅的，失败原因会指向假问题）")
            return 2
        cfg.models = {"rt": ModelConfig(provider="anthropic", model=model,
                                        api_key_env=key_env, base_url=base, max_tokens=8192)}
        cfg.active_model = "rt"
    cfg.agent.workspace = str(ws)
    cfg.agent.per_session_workspace = False
    cfg.agent.max_steps = 12
    cfg.agent.screenshot = False
    cfg.mcp.enabled = False
    cfg.memory.enabled = False
    cfg.storage.db_path = str(tmp / "h.db")

    api = Api(cfg, emit=emit)
    api_holder.append(api)
    try:
        api.send_message(
            "读工作区里的 report.txt，把里面的本季度营收数字告诉我。"
            "如果它要登录，用你手头的工具处理，别编数字。")
        if not done.wait(300):
            print("TIMEOUT")
        time.sleep(1)
        text = "".join(d if isinstance(d, str) else (d or {}).get("text", "")
                       for k, d in events if k == "chunk")
        tools = [(d or {}).get("name") for k, d in events if k == "tool_use"]
        print("\n模型最终回答：", (text or "(空)").strip()[-400:])
        print("工具调用序列：", tools)

        asked = bool(handoffs)
        verify_ok = asked and bool(str(handoffs[0].get("verify") or "").strip())
        # 换手之后又读了一次现场？（工具序列里 request_handoff 之后还有读取类调用）
        reread = False
        if "request_handoff" in tools:
            after = tools[tools.index("request_handoff") + 1:]
            reread = any(t in ("read_file", "grep_search", "list_dir", "browser_snapshot")
                         for t in after)
        answered = "41.7" in text
        for name, ok in (("① 主动请求换手", asked), ("② verify 非空", verify_ok),
                         ("③ 交回后重新读现场", reread), ("④ 答出真数据", answered)):
            print(("  PASS  " if ok else "  FAIL  ") + name)
        ok = asked and verify_ok and reread and answered
        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
