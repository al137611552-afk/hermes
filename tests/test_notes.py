"""FR-11.3a 工作笔记 + 11.3b 可重读引用：存取/级联/工具/注入块/截短标注（无网络）。

运行：python tests/test_notes.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.context import _slim_old_tool_results  # noqa: E402
from agentcore.providers import Message  # noqa: E402
from agentcore.store import Store  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.notes import (  # noqa: E402
    NotesBinding, UpdateNotesTool, build_notes_block,
)


def test_build_notes_block():
    assert build_notes_block("") is None and build_notes_block("   ") is None
    b = build_notes_block("- 用 ark-kimi\n- DB 在 data/")
    assert "[工作笔记]" in b and "ark-kimi" in b


def test_store_notes_roundtrip_and_cascade(tmp: Path):
    st = Store(tmp / "h.db")
    sid = st.create_session("t", "m")
    assert st.get_notes(sid) == ""                 # 默认空
    st.set_notes(sid, "事实A\n决定B")
    assert st.get_notes(sid) == "事实A\n决定B"
    st.set_notes(sid, "覆盖")                       # 整份替换
    assert st.get_notes(sid) == "覆盖"
    st.delete_session(sid)                          # 级联删
    assert st.get_notes(sid) == ""
    st.close()


def test_update_notes_tool(tmp: Path):
    st = Store(tmp / "h.db")
    sid = st.create_session("t", "m")
    events = []
    tool = UpdateNotesTool(NotesBinding(st, lambda: sid, lambda e, d: events.append((e, d))))
    out = tool.run({"notes": "# 计划\n- 步骤1完成"})
    assert "已更新" in out and st.get_notes(sid) == "# 计划\n- 步骤1完成"
    assert events[-1][0] == "notes_updated"
    assert "已清空" in tool.run({"notes": ""})      # 空字符串=清空
    # 缺 notes / 过长 / 无会话 报错
    try:
        tool.run({})
        assert False
    except ToolError as e:
        assert "notes" in str(e)
    try:
        tool.run({"notes": "x" * 9000})
        assert False
    except ToolError as e:
        assert "过长" in str(e)
    tool2 = UpdateNotesTool(NotesBinding(st, lambda: None, lambda e, d: None))
    try:
        tool2.run({"notes": "x"})
        assert False
    except ToolError as e:
        assert "尚未保存" in str(e)
    st.close()


def test_notes_tool_registered_when_store(tmp: Path):
    from agentcore.tools import build_registry
    from agentcore.tools.notes import NotesBinding as NB
    st = Store(tmp / "h.db")
    sid = st.create_session("t", "m")
    reg = build_registry(tmp, notes_binding=NB(st, lambda: sid, lambda e, d: None))
    assert "update_notes" in reg.names() and not reg.is_dangerous("update_notes")
    assert "update_notes" not in build_registry(tmp).names()   # 无 binding 不注册
    st.close()


def test_reread_hint_on_slimmed_read_result():
    """11.3b：被截短的旧 read_file 结果带'可用 read_file 重读 <路径>'标注。"""
    big = "x" * 1000
    msgs = [
        Message("user", "读下 a.py"),
        Message("assistant", [{"type": "tool_use", "id": "t1",
                               "name": "read_file", "input": {"path": "src/a.py"}}]),
        Message("user", [{"type": "tool_result", "tool_use_id": "t1", "content": big}]),
        # 另一个非 read_file 的大结果：不带重读提示
        Message("assistant", [{"type": "tool_use", "id": "t2",
                               "name": "run_bash", "input": {"command": "ls"}}]),
        Message("user", [{"type": "tool_result", "tool_use_id": "t2", "content": big}]),
        Message("user", "继续"),
    ]
    out, slimmed = _slim_old_tool_results(msgs, keep_from=len(msgs), max_chars=200)
    assert slimmed == 2
    read_tr = out[2].content[0]["content"]
    assert "可用 read_file 重读 src/a.py" in read_tr
    bash_tr = out[4].content[0]["content"]
    assert "已截短" in bash_tr and "重读" not in bash_tr
    # 原消息对象不被改动
    assert msgs[2].content[0]["content"] == big


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
