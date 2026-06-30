"""可编程 hooks 自测（纯逻辑 + 真子进程 hook + 经 AgentLoop._exec_tool 端到端）。

运行：python tests/test_hooks.py
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.hooks import (  # noqa: E402
    HookRunner, make_hook_runner, match_hooks, parse_pre_result,
)


@dataclass
class H:  # 轻量 hook 配置替身（与 HookConfig 同字段）
    event: str
    command: str = ""
    matcher: str = ""
    name: str = ""
    timeout: int = 10


# ---- 纯逻辑 ----------------------------------------------------------------

def test_match_hooks_by_event_and_matcher():
    hooks = [H("PreToolUse", matcher="write_file|edit_file"),
             H("PostToolUse", matcher="edit_file"),
             H("PreToolUse", matcher="run_")]
    pre_write = match_hooks(hooks, "PreToolUse", "write_file")
    assert len(pre_write) == 1
    assert match_hooks(hooks, "PreToolUse", "run_bash")[0].matcher == "run_"
    assert match_hooks(hooks, "PostToolUse", "edit_file")[0].event == "PostToolUse"
    assert match_hooks(hooks, "PostToolUse", "write_file") == []  # 事件对、matcher 不中


def test_match_empty_matcher_matches_all():
    assert len(match_hooks([H("PreToolUse")], "PreToolUse", "anything")) == 1


def test_match_bad_regex_skipped():
    assert match_hooks([H("PreToolUse", matcher="[")], "PreToolUse", "x") == []


def test_parse_pre_result():
    assert parse_pre_result(2, "危险", "") == ("deny", "危险")
    assert parse_pre_result(1, "小心", "") == ("warn", "小心")
    assert parse_pre_result(0, "", "") == ("allow", "")
    assert parse_pre_result(2, "", "")[0] == "deny"   # 无消息也有兜底
    assert parse_pre_result(7, "x", "")[0] == "allow"  # 未知码 -> allow


# ---- HookRunner 真子进程 ----------------------------------------------------

def test_pre_deny_blocks(tmp: Path):
    # 写文件前：若内容含 SECRET 就拦截（退出码 2）
    hook = H("PreToolUse", matcher="write_file", name="扫密钥",
             command="grep -q SECRET <<<\"$(cat)\" && { echo '内容含密钥'; exit 2; } || exit 0")
    r = HookRunner(tmp, [hook])
    allowed, msg = r.pre("write_file", {"path": "x.py", "content": "API_SECRET=abc"})
    assert allowed is False and "扫密钥" in msg and "密钥" in msg
    allowed2, _ = r.pre("write_file", {"path": "x.py", "content": "x=1"})
    assert allowed2 is True


def test_pre_warn_passes_with_message(tmp: Path):
    hook = H("PreToolUse", name="提醒", command="echo '注意分支'; exit 1")
    allowed, msg = HookRunner(tmp, [hook]).pre("run_bash", {"command": "git push"})
    assert allowed is True and "提醒" in msg and "注意分支" in msg


def test_pre_no_match_zero_overhead(tmp: Path):
    r = HookRunner(tmp, [H("PreToolUse", matcher="write_file", command="exit 2")])
    assert r.pre("read_file", {"path": "x"}) == (True, None)  # 不匹配 -> 直接放行


def test_post_appends_stdout(tmp: Path):
    hook = H("PostToolUse", matcher="edit_file", name="lint",
             command="echo 'E501 line too long'")
    out = HookRunner(tmp, [hook]).post("edit_file", {"path": "a.py"}, "已编辑 a.py")
    assert out and "lint" in out and "E501" in out


def test_hook_receives_payload_on_stdin(tmp: Path):
    # hook 把 stdin 的 JSON 落盘，验证 tool/params/event 都传到了
    hook = H("PostToolUse", command=f"cat > {tmp/'got.json'}")
    HookRunner(tmp, [hook]).post("write_file", {"path": "p.py"}, "result-text")
    import json
    data = json.loads((tmp / "got.json").read_text())
    assert data["tool"] == "write_file" and data["params"]["path"] == "p.py"
    assert data["event"] == "PostToolUse" and data["result"] == "result-text"


def test_runner_bad_command_does_not_block(tmp: Path):
    # 命令跑不起来：pre 视为放行、不阻塞工具
    r = HookRunner(tmp, [H("PreToolUse", command="this_cmd_does_not_exist_xyz", timeout=5)])
    allowed, _ = r.pre("write_file", {"path": "x"})
    assert allowed is True


def test_make_hook_runner_none_when_empty(tmp: Path):
    assert make_hook_runner(tmp, []) is None
    assert make_hook_runner(tmp, [H("PreToolUse", command="x")]) is not None


# ---- 经 AgentLoop._exec_tool 端到端 ----------------------------------------

def test_loop_exec_tool_pre_deny_blocks_write(tmp: Path):
    from agentcore.agent.loop import AgentLoop
    from agentcore.agent.gate import PermissionGate
    from agentcore.tools.registry import build_registry
    gate = PermissionGate(emit=lambda d: None); gate._allow_all = True
    reg = build_registry(tmp)
    hook = H("PreToolUse", matcher="write_file", name="禁写密钥",
             command="grep -q SECRET <<<\"$(cat)\" && exit 2 || exit 0")
    loop = AgentLoop(None, reg, gate, hook_runner=HookRunner(tmp, [hook]))
    text, ok, _ = loop._exec_tool("write_file", {"path": "s.py", "content": "SECRET=1"})
    assert ok is False and "禁写密钥" in text
    assert not (tmp / "s.py").exists()  # 被拦，文件没落盘
    # 不含密钥的写入正常落盘
    text2, ok2, _ = loop._exec_tool("write_file", {"path": "ok.py", "content": "x=1"})
    assert ok2 is True and (tmp / "ok.py").exists()


def test_loop_exec_tool_post_appends(tmp: Path):
    from agentcore.agent.loop import AgentLoop
    from agentcore.agent.gate import PermissionGate
    from agentcore.tools.registry import build_registry
    gate = PermissionGate(emit=lambda d: None); gate._allow_all = True
    reg = build_registry(tmp)
    hook = H("PostToolUse", matcher="write_file", name="post检查", command="echo 'OK 已扫描'")
    loop = AgentLoop(None, reg, gate, hook_runner=HookRunner(tmp, [hook]))
    text, ok, _ = loop._exec_tool("write_file", {"path": "a.py", "content": "x=1"})
    assert ok is True and "post检查" in text and "已扫描" in text


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
