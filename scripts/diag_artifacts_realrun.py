"""ADR 0021 产物化 真模型自测：模型拿到句柄后**会不会真的去下钻**（ADR 风险 1）。

用法（项目根目录下，需要 config.yaml 里当前模型的 key 可用、能联网）：
    python scripts/diag_artifacts_realrun.py

场景刻意为难：命令输出 30 万字符，`SECRET_CODE` 藏在**正中间**——既不在摘要的头 60 行、
也不在尾 40 行里。模型只有 grep/read 产物才拿得到；只看摘要就下结论必然答不出。
通过标准：最终回答里出现那串码。用临时目录，不污染 data/。

2026-08-10 实测（deepseek-v4-flash，anthropic 兼容端点）：
`run_bash` → 看到摘要+句柄 → **自发** `grep_search path=.hermes/artifacts/art_0001.log` → 答对，
全程没重跑命令。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# —— 按脚本位置定位 src/，不依赖 cwd ——
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

from agentcore.config import load_config    # noqa: E402
from agentcore.bridge.api import Api        # noqa: E402

events = []
done = threading.Event()


def emit(kind, data, cid=None):
    events.append((kind, data))
    if kind == "tool_use":
        print(f"  [工具] {(data or {}).get('name')} {str((data or {}).get('input'))[:160]}")
    if kind == "error":
        print("  [error]", str(data)[:300])
        done.set()
    if kind == "done":
        done.set()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="artifact-realrun-"))
    ws = tmp / "proj"
    ws.mkdir()
    (ws / "gen.py").write_text(
        "import sys\n"
        "for i in range(3000):\n"
        "    if i == 1500: sys.stdout.write('SECRET_CODE=ZQ-7741\\n')\n"
        "    sys.stdout.write('noise line %d ' % i + 'y'*90 + '\\n')\n",
        encoding="utf-8")

    cfg = load_config()
    cfg.agent.workspace = str(ws)
    cfg.agent.per_session_workspace = False
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.max_steps = 12
    cfg.agent.screenshot = False
    cfg.mcp.enabled = False
    cfg.memory.enabled = False
    cfg.storage.db_path = str(tmp / "h.db")
    cfg.agent.permissions.allow = [f"run_{cfg.agent.shell}(*)"]   # headless：免确认

    py = "python" if os.name == "nt" else "python3"
    api = Api(cfg, emit=emit)
    try:
        api.send_message(f"跑 `{py} gen.py`，然后告诉我它输出里的 SECRET_CODE 是什么。只回答那一串码。")
        if not done.wait(300):
            print("TIMEOUT")
        time.sleep(1)
        text = "".join(d if isinstance(d, str) else (d or {}).get("text", "")
                       for k, d in events if k == "chunk")
        tools = [(d or {}).get("name") for k, d in events if k == "tool_use"]
        print("\n模型最终回答：", (text or "(空)").strip()[-400:])
        print("工具调用序列：", tools)
        ok = "ZQ-7741" in text
        print("\nRESULT:", "PASS（模型下钻了产物）" if ok
              else "FAIL（只看摘要就作答 / 没拿到中间的码）")
        return 0 if ok else 1
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
