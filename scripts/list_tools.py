"""列出当前配置下模型**实际拿到**的工具清单（排查"模型没调某工具"时的第一步）。

    python scripts/list_tools.py

不联网、不调模型：只按当前 config.yaml 建一次注册表把工具名打出来，顺带标出危险工具
（要过权限确认的）。模型没用某个工具时，先用它区分两种原因：**根本没给它** vs **给了但没选**。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

from agentcore.config import load_config      # noqa: E402
from agentcore.bridge.api import Api          # noqa: E402


def main() -> int:
    cfg = load_config()
    cfg.mcp.enabled = False        # 只看内置工具，别为列清单去起 MCP server
    cfg.memory.enabled = False
    api = Api(cfg, emit=lambda *a, **k: None)
    try:
        reg = api.active.registry
        names = sorted(reg.names())
        print(f"共 {len(names)} 个工具：\n")
        for n in names:
            print(("  ⚠ " if reg.is_dangerous(n) else "    ") + n)
        for must in ("request_handoff", "ask_user"):
            print(f"\n{must}: " + ("✅ 已注册" if must in names else "❌ 不在清单里"))
            if must in names:
                t = reg.get(must)
                print("  必填参数：", t.input_schema.get("required"))
                print("  说明首句：", (t.description or "").split("。")[0][:80])
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
