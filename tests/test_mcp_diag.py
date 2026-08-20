"""MCP server 体检的纯逻辑（不碰盘、不起进程）。

面板原来只能显示一句 `Connection closed` 加 server 的 stderr——真踩到的两种故障
（参数写进「启动命令」框、PATH 里两份同名命令）**都不在那句话里**，用户对着它猜了四轮。
所以每一条结论都要能直接照着改。

运行：python tests/test_mcp_diag.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.mcp_client.diag import BAD, OK, WARN, analyze_spec  # noqa: E402


def _levels(findings, level):
    return [f["text"] for f in findings if f["level"] == level]


def test_args_written_into_command_box_is_called_out():
    """真机故障 ①：`codex mcp-server` 整串写进了命令框 → 被当交互式启动。"""
    out = analyze_spec({"command": "codex mcp-server", "args": []}, 60.0, resolved="")
    bad = " ".join(_levels(out, BAD))
    assert "带空格" in bad and "参数" in bad
    assert "找不到" in bad          # 顺带：整串当然解析不到


def test_duplicate_commands_in_path_are_reported():
    """真机故障 ②：终端解析到新版、子进程解析到旧版，表现成"这个子命令不存在"。"""
    out = analyze_spec({"command": "codex", "args": ["mcp-server"]}, 60.0,
                       resolved="/a/codex", candidates=["/a/codex", "/b/codex"])
    assert any("多份同名" in t for t in _levels(out, WARN))
    # 只有一份就不该报
    out2 = analyze_spec({"command": "codex", "args": ["mcp-server"]}, 60.0,
                        resolved="/a/codex", candidates=["/a/codex"])
    assert not any("多份同名" in t for t in _levels(out2, WARN))


def test_agent_server_pitfalls_are_warned():
    """agent 型 server 的三个必踩点：没参数、没工作目录、超时跟随全局。"""
    out = analyze_spec({"command": "codex", "args": []}, 60.0, resolved="/a/codex")
    warns = " ".join(_levels(out, WARN))
    assert "stdin is not a terminal" in warns      # 缺子命令的后果，直接写出来
    assert "工作目录" in warns
    assert "超时" in warns and "60" in warns


def test_missing_cwd_directory_is_an_error_not_a_warning():
    out = analyze_spec({"command": "codex", "args": ["mcp-server"], "cwd": "D:/nope"},
                       60.0, resolved="/a/codex", cwd_exists=False)
    assert any("工作目录不存在" in t for t in _levels(out, BAD))


def test_permission_flags_are_surfaced_both_ways():
    """trust=开 与 always_confirm=关 都要提醒——后者正是"点过全部允许就全放开"那个坑。"""
    trusted = analyze_spec({"command": "c", "args": ["x"], "trust": True}, 60.0, resolved="/c")
    assert any("免确认" in t for t in _levels(trusted, WARN))
    loose = analyze_spec({"command": "c", "args": ["x"]}, 60.0, resolved="/c")
    assert any("每次都问" in t for t in _levels(loose, WARN))
    tight = analyze_spec({"command": "c", "args": ["x"], "always_confirm": True,
                          "cwd": "/w", "call_timeout": 900}, 60.0,
                         resolved="/c", cwd_exists=True)
    assert _levels(tight, WARN) == [] and _levels(tight, BAD) == []
    assert any("解析到" in t for t in _levels(tight, OK))


def test_probe_never_hangs_on_a_silent_process():
    """`readline()` 在子进程一个字都不输出时会**永久阻塞**，deadline 循环根本跑不到——
    真机上表现为按钮卡在「体检中…」再也不动。硬看门狗到点杀进程，读端自然 EOF。"""
    import subprocess
    import sys as _sys
    import time

    from agentcore.mcp_client.diag import probe_connect

    t0 = time.time()
    r = probe_connect({"command": _sys.executable, "args": ["-c", "import time;time.sleep(60)"]},
                      timeout=3)
    took = time.time() - t0
    assert took < 15, f"哑进程把体检卡了 {took:.0f}s"
    assert r["ok"] is False and r["error"]


def test_probe_reports_launch_failure_instead_of_raising():
    from agentcore.mcp_client.diag import probe_connect
    r = probe_connect({"command": "这个命令肯定不存在_xyz", "args": []}, timeout=3)
    assert r["ok"] is False and "FileNotFoundError" in r["error"] or r["error"]


def test_named_workspaces_are_legit_only_scratch_and_install_dir_warn():
    """**具名会话工作区是正当的**（`data/workspaces/1`），一刀切会误报——真机误报过。
    该警告的只有草稿区（没打开项目时的默认）和安装目录里的其它位置。"""
    from agentcore.config import APP_DIR
    from agentcore.mcp_client.diag import inside_hermes_dir

    assert inside_hermes_dir(str(APP_DIR / "data" / "workspaces" / "1")) is False
    assert inside_hermes_dir(str(APP_DIR / "data" / "workspaces" / "_scratch")) is True
    assert inside_hermes_dir(str(APP_DIR / "src")) is True
    assert inside_hermes_dir(str(APP_DIR)) is True
    assert inside_hermes_dir("/tmp/my-project") is False
    assert inside_hermes_dir("") is False
    # 自定义 workspaces_root 也要认
    assert inside_hermes_dir(str(APP_DIR / "ws" / "proj"),
                             workspaces_root=str(APP_DIR / "ws")) is False


def test_mcp_subprocess_inherits_parent_env():
    """SDK 默认只给白名单环境，代理变量（HTTPS_PROXY…）会被滤掉——真机表现是 codex 在子进程里
    反复 Reconnecting/超时，而同一条命令在用户终端里好好的。"""
    import inspect

    from agentcore.mcp_client.manager import McpManager
    src = inspect.getsource(McpManager._serve)
    assert "_os.environ" in src and "sc.env or {}" in src, "必须继承父进程环境再叠加 server 配置"


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
