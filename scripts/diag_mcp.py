"""MCP server 连不上时的第一手排查：**照 hermes 相同的方式**起一次，把过程摊开。

    python scripts/diag_mcp.py            # 诊断所有已启用的 server
    python scripts/diag_mcp.py codex      # 只诊断某一个

逻辑收在 `agentcore.mcp_client.diag`，与设置面板的「体检」按钮**共用同一份实现**——
两处各写一份必然漂，而这正是排查工具最不该出的错。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.config import APP_DIR, USER_MCP_FILE, load_config  # noqa: E402
from agentcore.mcp_client.diag import diagnose  # noqa: E402

_ICON = {"ok": "✅", "warn": "⚠ ", "bad": "❌"}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_config()
    f = APP_DIR / USER_MCP_FILE
    print(f"hermes 目录 : {APP_DIR}")
    print(f"面板存盘    : {f}  {'（存在）' if f.is_file() else '（不存在——面板还没存过）'}")
    print(f"mcp.enabled : {cfg.mcp.enabled}"
          + ("" if cfg.mcp.enabled else "   ⚠ 关着，工具不会挂载"))
    servers = {n: s.model_dump() for n, s in cfg.mcp.servers.items()}
    if not servers:
        print("没有配置任何 MCP server。")
        return 1
    bad = 0
    for name, spec in servers.items():
        if only and name != only:
            continue
        print(f"\n{'=' * 62}\n■ server: {name}")
        if not spec.get("enabled", True):
            print("  已停用，跳过")
            continue
        r = diagnose(name, spec, cfg.mcp.call_timeout)
        for item in r["findings"]:
            print(f"  {_ICON.get(item['level'], '  ')} {item['text']}")
            bad += item["level"] == "bad"
    print("\n提示：本会话点过「全部允许」、或在 /crazy 免确认模式下，MCP 工具同样不弹确认"
          "（那是会话状态，配置里看不到）。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
