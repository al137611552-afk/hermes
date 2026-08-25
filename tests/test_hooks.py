"""可编程 hooks 自测（纯逻辑 + 真子进程 hook + 经 AgentLoop._exec_tool 端到端）。

运行：python tests/test_hooks.py
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/_shellenv.py

from _shellenv import (  # noqa: E402
    hook_deny_if_stdin_has, hook_echo, hook_echo_exit, hook_exit, hook_stdin_to_file,
)
from agentcore.hooks import (  # noqa: E402
    HookRunner, make_hook_runner, match_hooks, parse_pre_result, parse_prompt_result,
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
             command=hook_deny_if_stdin_has("SECRET", "SECRET-FOUND"))
    r = HookRunner(tmp, [hook])
    allowed, msg = r.pre("write_file", {"path": "x.py", "content": "API_SECRET=abc"})
    assert allowed is False and "扫密钥" in msg and "SECRET-FOUND" in msg
    allowed2, _ = r.pre("write_file", {"path": "x.py", "content": "x=1"})
    assert allowed2 is True


def test_pre_warn_passes_with_message(tmp: Path):
    hook = H("PreToolUse", name="提醒", command=hook_echo_exit("branch-warning", 1))
    allowed, msg = HookRunner(tmp, [hook]).pre("run_bash", {"command": "git push"})
    assert allowed is True and "提醒" in msg and "branch-warning" in msg


def test_pre_no_match_zero_overhead(tmp: Path):
    r = HookRunner(tmp, [H("PreToolUse", matcher="write_file", command=hook_exit(2))])
    assert r.pre("read_file", {"path": "x"}) == (True, None)  # 不匹配 -> 直接放行


def test_post_appends_stdout(tmp: Path):
    hook = H("PostToolUse", matcher="edit_file", name="lint",
             command=hook_echo("E501 line too long"))
    out = HookRunner(tmp, [hook]).post("edit_file", {"path": "a.py"}, "已编辑 a.py")
    assert out and "lint" in out and "E501" in out


def test_hook_receives_payload_on_stdin(tmp: Path):
    # hook 把 stdin 的 JSON 落盘，验证 tool/params/event 都传到了
    hook = H("PostToolUse", command=hook_stdin_to_file(tmp / "got.json"))
    HookRunner(tmp, [hook]).post("write_file", {"path": "p.py"}, "result-text")
    import json
    data = json.loads((tmp / "got.json").read_text(encoding="utf-8"))
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


# ---- UserPromptSubmit / Stop 新事件 -------------------------------------------

def test_match_prompt_and_stop_empty_matcher():
    # 新事件无工具名可匹配：matcher 空视为匹配全部
    assert len(match_hooks([H("UserPromptSubmit")], "UserPromptSubmit", "")) == 1
    assert len(match_hooks([H("Stop")], "Stop", "")) == 1
    assert match_hooks([H("UserPromptSubmit", matcher="write_file")], "UserPromptSubmit", "") == []
    assert match_hooks([H("Stop")], "PreToolUse", "") == []   # 事件不对不触发


def test_parse_prompt_result():
    assert parse_prompt_result(2, "拒绝", "") == ("deny", "拒绝")
    assert parse_prompt_result(2, "", "")[0] == "deny"        # 无消息也有兜底
    assert parse_prompt_result(0, "ctx", "") == ("inject", "ctx")
    assert parse_prompt_result(0, "", "") == ("allow", "")   # 0 但无输出 -> 不注入
    assert parse_prompt_result(1, "x", "") == ("allow", "")  # 未定义码 -> 放行不注入
    assert parse_prompt_result(7, "x", "") == ("allow", "")  # 未知码 -> allow


def test_prompt_submit_deny_blocks(tmp: Path):
    hook = H("UserPromptSubmit", name="准入", command=hook_exit(2))
    ok, msg = HookRunner(tmp, [hook]).prompt_submit("帮我改 bug")
    assert ok is False and "准入" in msg


def test_prompt_submit_injects_stdout(tmp: Path):
    hook = H("UserPromptSubmit", name="注入", command=hook_echo("PROJECT-CTX"))
    ok, ctx = HookRunner(tmp, [hook]).prompt_submit("hello")
    assert ok is True and ctx and "PROJECT-CTX" in ctx and "注入" in ctx


def test_prompt_submit_payload_on_stdin(tmp: Path):
    hook = H("UserPromptSubmit", command=hook_stdin_to_file(tmp / "got.json"))
    HookRunner(tmp, [hook]).prompt_submit("给个计划")
    import json
    data = json.loads((tmp / "got.json").read_text(encoding="utf-8"))
    assert data["event"] == "UserPromptSubmit" and data["prompt"] == "给个计划"
    assert data["workspace"] == str(tmp.resolve())


def test_prompt_submit_no_hook_zero_overhead(tmp: Path):
    r = HookRunner(tmp, [H("PreToolUse", command=hook_exit(2))])
    assert r.prompt_submit("x") == (True, None)   # 无该事件 hook -> 直接放行


def test_stop_receives_payload_and_reason(tmp: Path):
    hook = H("Stop", command=hook_stdin_to_file(tmp / "stop.json"))
    HookRunner(tmp, [hook]).stop("done")
    import json
    data = json.loads((tmp / "stop.json").read_text(encoding="utf-8"))
    assert data["event"] == "Stop" and data["reason"] == "done"
    assert data["workspace"] == str(tmp.resolve())


def test_stop_stdout_not_returned(tmp: Path):
    # stop 无返回值：stdout 不回灌模型，能跑、不抛即通过
    HookRunner(tmp, [H("Stop", command=hook_echo("NOTIFY"))]).stop("error")


def test_stop_no_match_zero_overhead(tmp: Path):
    # 无 Stop 事件 hook -> 不跑任何命令、不抛
    HookRunner(tmp, [H("PreToolUse", command=hook_exit(2))]).stop("done")


# ---- 经 AgentLoop._exec_tool 端到端 ----------------------------------------

def test_loop_exec_tool_pre_deny_blocks_write(tmp: Path):
    from agentcore.agent.loop import AgentLoop
    from agentcore.agent.gate import PermissionGate
    from agentcore.tools.registry import build_registry
    gate = PermissionGate(emit=lambda d: None); gate._allow_all = True
    reg = build_registry(tmp)
    hook = H("PreToolUse", matcher="write_file", name="禁写密钥",
             command=hook_deny_if_stdin_has("SECRET", "SECRET-FOUND"))
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
    hook = H("PostToolUse", matcher="write_file", name="post检查", command=hook_echo("OK-SCANNED"))
    loop = AgentLoop(None, reg, gate, hook_runner=HookRunner(tmp, [hook]))
    text, ok, _ = loop._exec_tool("write_file", {"path": "a.py", "content": "x=1"})
    assert ok is True and "post检查" in text and "OK-SCANNED" in text


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
