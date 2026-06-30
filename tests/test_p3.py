"""P3 纯逻辑自测：工具 / gate / agent 循环（无 GUI、无网络）。

运行：python -m pytest tests/ -q   或   python tests/test_p3.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.gate import ALLOW, ALLOW_ALL, DENY, PermissionGate  # noqa: E402
from agentcore.agent.loop import AgentLoop  # noqa: E402
from agentcore.providers.base import Message, StreamEvent, ToolCall  # noqa: E402
from agentcore.tools import ToolError, build_registry  # noqa: E402


# ---- 工具：路径越界拒绝 + edit_file 串替换 ------------------------------
def test_path_escape_rejected(tmp: Path):
    reg = build_registry(tmp, shell="bash")
    read = reg.get("read_file")
    try:
        read.run({"path": "../secret.txt"})
    except ToolError:
        return
    raise AssertionError("越界路径未被拒绝")


def test_write_read_edit(tmp: Path):
    reg = build_registry(tmp, shell="bash")
    reg.get("write_file").run({"path": "a.txt", "content": "hello world"})
    assert (tmp / "a.txt").read_text() == "hello world"
    assert "hello" in reg.get("read_file").run({"path": "a.txt"})

    reg.get("edit_file").run({"path": "a.txt", "old_string": "world", "new_string": "P3"})
    assert (tmp / "a.txt").read_text() == "hello P3"

    # old_string 不唯一 -> 报错
    reg.get("write_file").run({"path": "b.txt", "content": "x x"})
    try:
        reg.get("edit_file").run({"path": "b.txt", "old_string": "x", "new_string": "y"})
    except ToolError:
        pass
    else:
        raise AssertionError("非唯一 old_string 未报错")


def test_registry_schema(tmp: Path):
    reg = build_registry(tmp, shell="bash")
    schemas = reg.to_schemas()
    names = {s["name"] for s in schemas}
    assert {"read_file", "write_file", "edit_file", "list_dir",
            "grep_search", "glob_search", "run_bash"} <= names
    for s in schemas:
        assert "input_schema" in s and "description" in s
    assert reg.is_dangerous("write_file") and not reg.is_dangerous("read_file")


# ---- gate：全允许短路 + resolve ----------------------------------------
def test_gate_allow_all():
    gate = PermissionGate(lambda req: None)
    # 异步 resolve 第一个请求为 allow_all
    def answer():
        time.sleep(0.05)
        gate.resolve(1, ALLOW_ALL)
    threading.Thread(target=answer).start()
    assert gate.confirm("write_file", {}) is True
    # 之后直接短路放行，无需再 resolve
    assert gate.confirm("run_bash", {}) is True


def test_gate_deny():
    gate = PermissionGate(lambda req: None)
    threading.Thread(target=lambda: (time.sleep(0.05), gate.resolve(1, DENY))).start()
    assert gate.confirm("write_file", {}) is False


# ---- agent 循环：tool_use -> tool_result 往返 --------------------------
class FakeProvider:
    """第一轮要求调用 list_dir，第二轮纯文本结束。"""
    def __init__(self):
        self.round = 0

    def stream_chat(self, messages, system=None, tools=None):
        self.round += 1
        if self.round == 1:
            yield StreamEvent("text", "我先看看目录。")
            yield StreamEvent("tool_use", meta={"call": ToolCall("c1", "list_dir", {"path": "."})})
            yield StreamEvent("done", meta={"stop_reason": "tool_use",
                                            "usage": {"input": 100, "output": 20, "cache_read": 0}})
        else:
            yield StreamEvent("text", "目录里有 a.txt。")
            yield StreamEvent("done", meta={"stop_reason": "end_turn",
                                            "usage": {"input": 130, "output": 8, "cache_read": 50}})


def test_agent_loop_roundtrip(tmp: Path):
    (tmp / "a.txt").write_text("hi")
    reg = build_registry(tmp, shell="bash")
    gate = PermissionGate(lambda req: None)  # list_dir 非危险，不会触发
    loop = AgentLoop(FakeProvider(), reg, gate, max_steps=5)

    events = []
    msgs = loop.run([Message("user", "看下目录")], None, lambda e, d: events.append((e, d)))

    kinds = [e for e, _ in events]
    assert "tool_use" in kinds and "tool_result" in kinds
    # 历史含 assistant(tool_use blocks) + user(tool_result) + assistant(最终文本)
    roles = [m.role for m in msgs]
    assert roles[-1] == "assistant" and isinstance(msgs[-1].content, str)
    assert any(isinstance(m.content, list) for m in msgs)  # 有 content blocks
    # FR-11.8：回合末发 usage，跨两步累加 token + 步数
    usage = next(d for e, d in events if e == "usage")
    assert usage["input"] == 230 and usage["output"] == 28 and usage["cache_read"] == 50
    assert usage["steps"] == 2 and usage["max_steps"] == 5


def test_agent_loop_no_usage_event_when_endpoint_silent(tmp: Path):
    """端点不回传 usage（done 无 usage）时不发 usage 事件，避免噪音。"""
    class SilentProvider:
        def stream_chat(self, messages, system=None, tools=None):
            yield StreamEvent("text", "好的。")
            yield StreamEvent("done", meta={"stop_reason": "end_turn"})
    reg = build_registry(tmp, shell="bash")
    loop = AgentLoop(SilentProvider(), reg, PermissionGate(lambda req: None), max_steps=5)
    events = []
    loop.run([Message("user", "hi")], None, lambda e, d: events.append((e, d)))
    assert not any(e == "usage" for e, _ in events)


def test_agent_loop_step_warning(tmp: Path):
    """步数接近上限（≥80%）发一次 step_warning。"""
    class LoopyProvider:
        def stream_chat(self, messages, system=None, tools=None):
            yield StreamEvent("tool_use", meta={"call": ToolCall("c", "list_dir", {"path": "."})})
            yield StreamEvent("done", meta={"stop_reason": "tool_use",
                                            "usage": {"input": 1, "output": 1}})
    reg = build_registry(tmp, shell="bash")
    loop = AgentLoop(LoopyProvider(), reg, PermissionGate(lambda req: None), max_steps=5)
    events = []
    loop.run([Message("user", "go")], None, lambda e, d: events.append((e, d)))
    warns = [d for e, d in events if e == "step_warning"]
    assert len(warns) == 1 and warns[0]["max_steps"] == 5   # 5*0.8=4，第4步预警一次


def test_agent_loop_denied(tmp: Path):
    """危险工具被拒 -> 回灌拒绝文本，不执行。"""
    class WriteProvider:
        def __init__(self): self.round = 0
        def stream_chat(self, messages, system=None, tools=None):
            self.round += 1
            if self.round == 1:
                yield StreamEvent("tool_use", meta={"call": ToolCall("c1", "write_file",
                    {"path": "x.txt", "content": "boom"})})
                yield StreamEvent("done", meta={"stop_reason": "tool_use"})
            else:
                yield StreamEvent("text", "好的，已取消。")
                yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    reg = build_registry(tmp, shell="bash")
    gate = PermissionGate(lambda req: None)
    threading.Thread(target=lambda: (time.sleep(0.05), gate.resolve(1, DENY))).start()
    loop = AgentLoop(WriteProvider(), reg, gate, max_steps=5)

    results = []
    loop.run([Message("user", "写文件")], None,
             lambda e, d: results.append((e, d)) if e == "tool_result" else None)
    assert not (tmp / "x.txt").exists()  # 文件未被创建
    assert any(not d["ok"] for _, d in results)  # 结果标记失败/拒绝


def test_agent_loop_thinking_emitted_not_in_answer(tmp: Path):
    """thinking 事件应被转发给前端，但不计入最终答案、不进历史。"""
    class ThinkProvider:
        def stream_chat(self, messages, system=None, tools=None):
            yield StreamEvent("thinking", "我先想想该怎么做…")
            yield StreamEvent("text", "答案是 42。")
            yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    reg = build_registry(tmp, shell="bash")
    gate = PermissionGate(lambda req: None)
    loop = AgentLoop(ThinkProvider(), reg, gate, max_steps=3)
    events = []
    msgs = loop.run([Message("user", "算一下")], None, lambda e, d: events.append((e, d)))

    kinds = [e for e, _ in events]
    assert "thinking" in kinds and "chunk" in kinds          # 都转发了
    assert any(d == "我先想想该怎么做…" for e, d in events if e == "thinking")
    # 最终 assistant 消息只含答案，不含思考内容
    final = msgs[-1]
    assert final.role == "assistant" and final.content == "答案是 42。"
    assert "想想" not in str(final.content)


def test_agent_loop_truncated_tool_not_executed(tmp: Path):
    """输出被 max_tokens 截断时：不执行残缺的 tool_use、报错、停止（不死循环）。"""
    class TruncProvider:
        def __init__(self): self.round = 0
        def stream_chat(self, messages, system=None, tools=None):
            self.round += 1
            # 每轮都返回「截断的 write_file（content 缺失）」+ stop_reason=max_tokens
            yield StreamEvent("text", "<!DOCTYPE html><html>...")
            yield StreamEvent("tool_use", meta={"call": ToolCall("c1", "write_file",
                {"path": "mockup.html"})})  # content 被截断掉了
            yield StreamEvent("done", meta={"stop_reason": "max_tokens"})

    reg = build_registry(tmp, shell="bash")
    gate = PermissionGate(lambda req: None)
    prov = TruncProvider()
    loop = AgentLoop(prov, reg, gate, max_steps=5)

    events = []
    loop.run([Message("user", "写个原型")], None, lambda e, d: events.append((e, d)))

    assert not (tmp / "mockup.html").exists()        # 没写出空文件
    assert prov.round == 1                            # 只跑一轮，没死循环
    assert any(e == "error" and "max_tokens" in str(d) for e, d in events)
    assert not any(e == "tool_result" for e, _ in events)  # 残缺工具未执行


def test_agent_loop_steering_inject(tmp: Path):
    """执行中追加（steering）：take_injects 的补充附进 tool_result 的**同一条** user 消息，
    模型下一轮即看到，且不产生连续 user / 不破坏交替。"""
    (tmp / "a.txt").write_text("hi")
    reg = build_registry(tmp, shell="bash")
    gate = PermissionGate(lambda req: None)  # list_dir 非危险
    loop = AgentLoop(FakeProvider(), reg, gate, max_steps=5)

    pending = [["也看下 a.txt 内容"]]  # 第一次工具回灌拉到一条补充，之后空
    take = lambda: pending.pop(0) if pending else []

    msgs = loop.run([Message("user", "看下目录")], None, lambda e, d: None, take_injects=take)

    tr_msg = next(m for m in msgs if m.role == "user" and isinstance(m.content, list)
                  and any(b.get("type") == "tool_result" for b in m.content))
    texts = [b["text"] for b in tr_msg.content if b.get("type") == "text"]
    assert any("[用户追加] 也看下 a.txt 内容" in t for t in texts), "补充未注入 tool_result 消息"
    assert tr_msg.content[0]["type"] == "tool_result"  # 仍是一条合法 user 消息（tool_result 在前）


def test_agent_loop_no_inject_keeps_old_behavior(tmp: Path):
    """take_injects=None（不传）时行为与原来完全一致：tool_result 消息只含工具结果，无追加块。"""
    (tmp / "a.txt").write_text("hi")
    reg = build_registry(tmp, shell="bash")
    loop = AgentLoop(FakeProvider(), reg, PermissionGate(lambda req: None), max_steps=5)
    msgs = loop.run([Message("user", "看下目录")], None, lambda e, d: None)
    tr_msg = next(m for m in msgs if m.role == "user" and isinstance(m.content, list)
                  and any(b.get("type") == "tool_result" for b in m.content))
    assert all(b.get("type") != "text" for b in tr_msg.content)  # 没有任何注入文本块


def test_write_edit_tools_emit_diff_block(tmp: Path):
    """write/edit 返回 ToolOutput 带 diff 块（供前端内联展示）。"""
    from agentcore.tools.base import ToolOutput
    reg = build_registry(tmp, shell="bash")
    (tmp / "a.txt").write_text("hello world\n")
    out = reg.get("edit_file").run({"path": "a.txt", "old_string": "world", "new_string": "P3"})
    assert isinstance(out, ToolOutput)
    diff = next(b for b in out.blocks if b.get("type") == "diff")
    assert "-hello world" in diff["diff"] and "+hello P3" in diff["diff"]
    out2 = reg.get("write_file").run({"path": "b.txt", "content": "line1\nline2\n"})
    assert isinstance(out2, ToolOutput) and any(b.get("type") == "diff" for b in out2.blocks)


def test_inline_diff_goes_to_frontend_not_model(tmp: Path):
    """diff 进 tool_result 事件（前端拿到）但不回灌模型（回灌的 user 消息里无 diff 块）。"""
    (tmp / "a.txt").write_text("hello world\n")

    class EditProvider:
        def __init__(self): self.round = 0
        def stream_chat(self, messages, system=None, tools=None):
            self.round += 1
            if self.round == 1:
                yield StreamEvent("tool_use", meta={"call": ToolCall("c1", "edit_file",
                    {"path": "a.txt", "old_string": "world", "new_string": "P3"})})
                yield StreamEvent("done", meta={"stop_reason": "tool_use"})
            else:
                yield StreamEvent("text", "好了")
                yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    reg = build_registry(tmp, shell="bash")
    gate = PermissionGate(lambda r: None)
    gate._allow_all = True   # 全允许，避免 edit_file（dangerous）卡在权限确认上
    loop = AgentLoop(EditProvider(), reg, gate, max_steps=5)
    events = []
    msgs = loop.run([Message("user", "改")], None, lambda e, d: events.append((e, d)))
    tr = next(d for e, d in events if e == "tool_result")
    assert tr.get("diff") and "+hello P3" in tr["diff"]["text"]   # 前端拿到 diff
    for m in msgs:                                                 # 模型回灌里无 diff 块
        if m.role == "user" and isinstance(m.content, list):
            assert all(b.get("type") != "diff" for b in m.content)


def test_readonly_tools_parallel_safe(tmp: Path):
    """只读工具标记 parallel_safe；写工具不标记；同轮多个只读工具并发执行、结果都正确。"""
    reg = build_registry(tmp, shell="bash")
    for n in ("read_file", "list_dir", "grep_search", "glob_search", "code_outline", "find_symbol"):
        assert getattr(reg.get(n), "parallel_safe", False), f"{n} 应 parallel_safe"
    for n in ("write_file", "edit_file", "multi_edit"):
        assert not getattr(reg.get(n), "parallel_safe", False), f"{n} 不应 parallel_safe"

    (tmp / "a.txt").write_text("AAA")
    (tmp / "b.txt").write_text("BBB")
    (tmp / "c.txt").write_text("CCC")

    class MultiReadProvider:
        def __init__(self): self.round = 0
        def stream_chat(self, messages, system=None, tools=None):
            self.round += 1
            if self.round == 1:
                for cid, path in [("r1", "a.txt"), ("r2", "b.txt"), ("r3", "c.txt")]:
                    yield StreamEvent("tool_use", meta={"call": ToolCall(cid, "read_file", {"path": path})})
                yield StreamEvent("done", meta={"stop_reason": "tool_use"})
            else:
                yield StreamEvent("text", "done")
                yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    loop = AgentLoop(MultiReadProvider(), reg, PermissionGate(lambda r: None), max_steps=5)
    outs = []
    loop.run([Message("user", "读三个")], None,
             lambda e, d: outs.append(d["output"]) if e == "tool_result" else None)
    assert any("AAA" in o for o in outs) and any("BBB" in o for o in outs) and any("CCC" in o for o in outs)


def test_extra_dir_grants_access(tmp: Path):
    """add-dir：授权目录后工具可读工作区外该目录的文件；未授权目录仍拒。"""
    ws = tmp / "ws"; ws.mkdir()
    ext = tmp / "external"; ext.mkdir(); (ext / "data.txt").write_text("EXTERNAL")
    shared: list = []
    reg = build_registry(ws, shell="bash", extra_dirs=shared)
    try:
        reg.get("read_file").run({"path": str(ext / "data.txt")})
        raise AssertionError("未授权却能读外部目录")
    except ToolError:
        pass
    shared.append(ext.resolve())                       # add-dir
    assert "EXTERNAL" in reg.get("read_file").run({"path": str(ext / "data.txt")})
    other = tmp / "other"; other.mkdir(); (other / "x.txt").write_text("X")
    try:
        reg.get("read_file").run({"path": str(other / "x.txt")})
        raise AssertionError("未授权的其它目录却能读")
    except ToolError:
        pass


def test_ask_user_binding(tmp: Path):
    """ask_user：ask() emit 问题并阻塞，resolve 后返回用户选择；空参报错。"""
    from agentcore.tools.ask import AskUserBinding, AskUserTool
    events = []
    b = AskUserBinding(lambda req: events.append(req))
    tool = AskUserTool(b)
    result = {}

    def asker():
        result["ans"] = tool.run({"question": "选哪个方案?", "options": ["A", "B"]})

    th = threading.Thread(target=asker); th.start()
    time.sleep(0.05)
    assert events and events[0]["question"] == "选哪个方案?" and events[0]["options"] == ["A", "B"]
    b.resolve(events[0]["id"], "B")          # 用户选了 B
    th.join(2.0)
    assert result["ans"] == "B"
    try:
        tool.run({"question": "", "options": ["A"]})   # 空 question 报错
    except ToolError:
        pass
    else:
        raise AssertionError("空 question 未报错")


# ---- 极简 runner（不依赖 pytest） --------------------------------------
def _run_all():
    import inspect
    import tempfile
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                if "tmp" in inspect.signature(fn).parameters:
                    fn(Path(d))
                else:
                    fn()
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
