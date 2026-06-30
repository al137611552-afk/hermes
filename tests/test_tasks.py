"""FR-9.1 任务规划与拆解：store 任务表 + update_tasks 工具 + 纯逻辑（无网络）。

运行：python tests/test_tasks.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.store.db import Store  # noqa: E402
from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.tasks import (  # noqa: E402
    TaskBinding,
    UpdateTasksTool,
    build_task_block,
    normalize_tasks,
    summarize_tasks,
)


# ---- 纯逻辑 ----------------------------------------------------------------

def test_normalize_valid():
    out = normalize_tasks([
        {"content": "  写代码 ", "status": "in_progress"},
        {"content": "测试"},                       # 缺 status -> pending
        {"content": "发布", "status": "怪值"},     # 非法 status -> pending
    ])
    assert out == [
        {"content": "写代码", "status": "in_progress"},
        {"content": "测试", "status": "pending"},
        {"content": "发布", "status": "pending"},
    ]


def test_normalize_rejects_bad():
    for bad in (None, "x", 3):
        try:
            normalize_tasks(bad); assert False, "应拒绝非数组"
        except ToolError:
            pass
    try:
        normalize_tasks([{"content": "  "}]); assert False, "应拒绝空 content"
    except ToolError:
        pass
    try:
        normalize_tasks([{"content": "x"}] * 51); assert False, "应拒绝超量"
    except ToolError:
        pass


def test_summarize():
    assert "清空" in summarize_tasks([])
    s = summarize_tasks([
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "pending"},
    ])
    assert "3 项" in s and "1/3" in s and "进行中：b" in s


def test_build_block():
    assert build_task_block([]) is None
    blk = build_task_block([
        {"content": "做A", "status": "completed"},
        {"content": "做B", "status": "in_progress"},
    ])
    assert blk is not None and "[当前任务清单]" in blk
    assert "✅ 做A" in blk and "🔄 做B" in blk
    # 全完成仍展示（让模型知道已收尾），但无未完成项时不强制
    assert build_task_block([{"content": "x", "status": "completed"}]) is not None


# ---- Store 任务表 ----------------------------------------------------------

def test_store_set_get_roundtrip(tmp: Path):
    st = Store(tmp / "h.db")
    sid = st.create_session("会1", "m")
    assert st.get_tasks(sid) == []                  # 初始为空
    tasks = [{"content": "a", "status": "pending"}]
    st.set_tasks(sid, tasks)
    assert st.get_tasks(sid) == tasks
    st.set_tasks(sid, [{"content": "b", "status": "completed"}])  # 整份替换
    assert st.get_tasks(sid) == [{"content": "b", "status": "completed"}]
    st.close()


def test_store_delete_session_cascades_tasks(tmp: Path):
    st = Store(tmp / "h.db")
    sid = st.create_session("会1", "m")
    st.set_tasks(sid, [{"content": "a", "status": "pending"}])
    st.delete_session(sid)
    assert st.get_tasks(sid) == []                  # 随会话删除而清
    st.close()


# ---- 工具 ------------------------------------------------------------------

def test_tool_persists_and_emits(tmp: Path):
    st = Store(tmp / "h.db")
    sid = st.create_session("会1", "m")
    events = []
    tool = UpdateTasksTool(TaskBinding(st, lambda: sid, lambda e, d: events.append((e, d))))
    msg = tool.run({"tasks": [
        {"content": "第一步", "status": "in_progress"},
        {"content": "第二步"},
    ]})
    assert "2 项" in msg
    assert st.get_tasks(sid) == [
        {"content": "第一步", "status": "in_progress"},
        {"content": "第二步", "status": "pending"},
    ]
    assert events and events[0][0] == "tasks_updated"
    assert len(events[0][1]["tasks"]) == 2
    st.close()


def test_tool_requires_session(tmp: Path):
    st = Store(tmp / "h.db")
    tool = UpdateTasksTool(TaskBinding(st, lambda: None, lambda e, d: None))
    try:
        tool.run({"tasks": [{"content": "x"}]}); assert False, "无会话应报错"
    except ToolError:
        pass
    st.close()


def test_registry_registers_update_tasks(tmp: Path):
    st = Store(tmp / "h.db")
    reg = build_registry(tmp, task_binding=TaskBinding(st, lambda: 1, lambda e, d: None))
    assert "update_tasks" in reg.names()
    assert reg.is_dangerous("update_tasks") is False    # 非危险，不过 gate
    # 不给 binding 则不注册
    reg2 = build_registry(tmp)
    assert "update_tasks" not in reg2.names()
    st.close()


def test_delegated_status():
    """FR-10.5：delegated（已委派）状态合法、入块带 🤖、回执单列。"""
    from agentcore.tools.tasks import build_task_block, normalize_tasks, summarize_tasks
    tasks = normalize_tasks([
        {"content": "调研结构", "status": "delegated"},
        {"content": "写实现", "status": "in_progress"},
        {"content": "未知态", "status": "瞎写"},
    ])
    assert tasks[0]["status"] == "delegated" and tasks[2]["status"] == "pending"
    block = build_task_block(tasks)
    assert "🤖 调研结构" in block and "delegated" in block
    s = summarize_tasks(tasks)
    assert "已委派：调研结构" in s and "进行中：写实现" in s


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
