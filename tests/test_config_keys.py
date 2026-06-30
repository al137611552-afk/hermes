"""产品化 key 配置纯逻辑自检：collect_key_requirements / upsert_env_line / mask_key。
运行：python tests/test_config_keys.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile  # noqa: E402

from agentcore.config import (  # noqa: E402
    _apply_user_hooks, _apply_user_mcp, browser_mcp_args, browser_mcp_enabled,
    browser_mcp_headed, collect_key_requirements, mask_key, read_user_hooks,
    read_user_mcp, remove_user_hook, remove_user_mcp_server, set_browser_mcp_state,
    set_user_mcp_server, upsert_env_line, upsert_user_hook,
)


def test_browser_mcp_args_headed_drops_headless():
    assert "--headless" in browser_mcp_args(False)       # 无头：带 --headless
    assert "--headless" not in browser_mcp_args(True)    # 有头登录态：去掉 --headless
    assert browser_mcp_args(True)[:2] == ["-y", "@playwright/mcp@latest"]


def test_browser_mcp_state_roundtrip():
    p = Path(tempfile.mktemp())
    set_browser_mcp_state(True, path=p)
    assert browser_mcp_enabled(p) and not browser_mcp_headed(p)
    set_browser_mcp_state(True, headed=True, path=p)
    assert browser_mcp_headed(p)
    set_browser_mcp_state(False, path=p)             # 关闭保留 headed 设置
    assert not browser_mcp_enabled(p) and browser_mcp_headed(p)


def test_collect_dedup_and_group():
    models = {
        "ark-kimi": {"api_key_env": "ARK_API_KEY"},
        "ark-deepseek": {"api_key_env": "ARK_API_KEY"},
        "claude": {"api_key_env": "ANTHROPIC_API_KEY"},
        "noenv": {},  # 无 api_key_env 的跳过
    }
    assert collect_key_requirements(models) == [
        {"env": "ANTHROPIC_API_KEY", "models": ["claude"]},
        {"env": "ARK_API_KEY", "models": ["ark-deepseek", "ark-kimi"]},
    ]


def test_collect_empty():
    assert collect_key_requirements({}) == []
    assert collect_key_requirements(None) == []


def test_upsert_replace_existing():
    out = upsert_env_line("# 注释\nARK_API_KEY=old\nOTHER=keep\n", "ARK_API_KEY", "new")
    assert "ARK_API_KEY=new" in out
    assert "OTHER=keep" in out
    assert "# 注释" in out           # 注释保留
    assert "old" not in out


def test_upsert_append_when_missing():
    out = upsert_env_line("OTHER=keep\n", "NEW_KEY", "v")
    assert "OTHER=keep" in out and "NEW_KEY=v" in out


def test_upsert_skips_comment_matches_export():
    out = upsert_env_line("# ARK_API_KEY=commented\nexport ARK_API_KEY=real\n", "ARK_API_KEY", "new")
    assert "ARK_API_KEY=new" in out          # 匹配 export 行并替换（统一成 KEY=value）
    assert "real" not in out
    assert "# ARK_API_KEY=commented" in out   # 注释行原样不动


def test_upsert_empty_text():
    assert upsert_env_line("", "K", "v") == "K=v\n"


def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "•••••"            # <=8 位全掩
    assert mask_key("sk-1234567890abcd") == "sk-1…abcd"  # 首尾各 4 位


# ── 统一管理面：用户 MCP server 覆盖层（Tier2-①）────────────────────────────

def test_user_mcp_crud_roundtrip():
    p = Path(tempfile.mktemp())
    assert read_user_mcp(p) == {}
    set_user_mcp_server("fs", {"command": "npx", "args": ["-y", "server-fs", "/proj"],
                               "trust": True, "enabled": True}, path=p)
    servers = read_user_mcp(p)
    assert servers["fs"]["command"] == "npx" and servers["fs"]["trust"] is True
    assert servers["fs"]["args"] == ["-y", "server-fs", "/proj"]
    # 改：trust 关掉
    set_user_mcp_server("fs", {"command": "npx", "args": [], "enabled": False}, path=p)
    assert read_user_mcp(p)["fs"]["enabled"] is False
    # 删
    remove_user_mcp_server("fs", path=p)
    assert read_user_mcp(p) == {}


def test_apply_user_mcp_only_enabled_with_command():
    user = {
        "good": {"command": "uvx", "args": ["mcp-server-git"], "enabled": True, "trust": True},
        "off":  {"command": "npx", "args": [], "enabled": False},        # 停用 → 不挂
        "nocmd": {"command": "", "enabled": True},                       # 无命令 → 不挂
    }
    out = _apply_user_mcp({"mcp": {"enabled": False, "servers": {}}}, user)
    assert "good" in out["mcp"]["servers"] and "off" not in out["mcp"]["servers"]
    assert "nocmd" not in out["mcp"]["servers"]
    assert out["mcp"]["enabled"] is True          # 有启用项 → 自动开 MCP 总开关
    assert out["mcp"]["servers"]["good"]["trust"] is True


def test_apply_user_mcp_empty_keeps_data():
    data = {"mcp": {"enabled": False, "servers": {}}}
    assert _apply_user_mcp(data, {}) is data       # 无用户 server 原样返回


def test_user_hooks_crud_and_index():
    p = Path(tempfile.mktemp())
    assert read_user_hooks(p) == []
    upsert_user_hook(None, {"event": "PreToolUse", "command": "echo a", "matcher": "write_file"}, path=p)
    upsert_user_hook(None, {"event": "PostToolUse", "command": "echo b"}, path=p)
    hooks = read_user_hooks(p)
    assert len(hooks) == 2 and hooks[0]["matcher"] == "write_file" and hooks[0]["timeout"] == 15
    # 改 index 0
    upsert_user_hook(0, {"event": "PreToolUse", "command": "echo a2", "enabled": False}, path=p)
    assert read_user_hooks(p)[0]["command"] == "echo a2"
    # 非法 event 归一
    upsert_user_hook(1, {"event": "bogus", "command": "x"}, path=p)
    assert read_user_hooks(p)[1]["event"] == "PreToolUse"
    # 删
    remove_user_hook(0, path=p)
    assert len(read_user_hooks(p)) == 1


def test_apply_user_hooks_filters_and_strips_enabled():
    user = [
        {"event": "PreToolUse", "command": "scan.sh", "matcher": "write_file", "enabled": True},
        {"event": "PostToolUse", "command": "off.sh", "enabled": False},   # 停用 → 不挂
        {"event": "PreToolUse", "command": "", "enabled": True},           # 无命令 → 不挂
    ]
    out = _apply_user_hooks({"agent": {"hooks": []}}, user)
    hooks = out["agent"]["hooks"]
    assert len(hooks) == 1 and hooks[0]["command"] == "scan.sh"
    assert "enabled" not in hooks[0]      # 剥掉 enabled（HookConfig 没有该字段）
    # 与已有手编 hooks 共存（追加不覆盖）
    out2 = _apply_user_hooks({"agent": {"hooks": [{"event": "PreToolUse", "command": "manual"}]}}, user)
    assert len(out2["agent"]["hooks"]) == 2


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
