"""情境自启：反复改同一文件失败 → 提示 trace_run 的检测逻辑自测（纯逻辑）。

运行：python tests/test_stuck.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.loop import (  # noqa: E402
    detect_browse_nudge, detect_login_wall, detect_stuck_edit, looks_failing,
)


def test_login_wall_fires_and_forbids_search_engine():
    state = {}
    calls = [type("C", (), {"id": "n1", "name": "browser__browser_navigate"})()]
    out = {"n1": "知乎 - 请先登录后查看回答内容，扫码登录"}
    msg = detect_login_wall(calls, out, state)
    assert msg and "ask_user" in msg and ("google" in msg or "搜索引擎" in msg)
    # 每轮只提一次
    assert detect_login_wall(calls, out, state) is None


def test_login_wall_no_false_positive_on_normal_page():
    state = {}
    calls = [type("C", (), {"id": "n1", "name": "browser__browser_snapshot"})()]
    out = {"n1": "618 音响推荐：惠威 H6、JBL 305P，正文很长……页头有个登录按钮"}
    assert detect_login_wall(calls, out, state) is None      # "登录按钮"不算登录墙强信号


def test_login_wall_ignores_non_browser_tools():
    state = {}
    calls = [type("C", (), {"id": "r1", "name": "read_file"})()]
    assert detect_login_wall(calls, {"r1": "请先登录"}, state) is None


@dataclass
class Call:  # ToolCall 替身
    id: str
    name: str
    input: dict = field(default_factory=dict)


def _edit(i, path):
    return Call(i, "edit_file", {"path": path})


def test_looks_failing():
    assert looks_failing("Traceback (most recent call last)")
    assert looks_failing("🧪 受影响测试未通过")
    assert looks_failing("AssertionError: x")
    assert not looks_failing("已编辑 a.py（30 字符）")
    assert not looks_failing("")


def test_nudge_fires_on_repeated_failing_edits():
    counts, nudged = {}, set()
    # 第 1、2 次：未到阈值 -> 不提示
    assert detect_stuck_edit([_edit("1", "a.py")], {"1": "🧪 未通过"}, counts, nudged, 3, True) is None
    assert detect_stuck_edit([_edit("2", "a.py")], {"2": "🧪 未通过"}, counts, nudged, 3, True) is None
    # 第 3 次且失败 -> 提示，且含文件名与 trace_run
    msg = detect_stuck_edit([_edit("3", "a.py")], {"3": "AssertionError"}, counts, nudged, 3, True)
    assert msg and "a.py" in msg and "trace_run" in msg
    # 同文件不再重复提示
    assert detect_stuck_edit([_edit("4", "a.py")], {"4": "Traceback"}, counts, nudged, 3, True) is None


def test_no_nudge_when_not_failing():
    counts, nudged = {}, set()
    for i in range(5):  # 改了 5 次但每次都成功（无失败信号）-> 不提示
        out = detect_stuck_edit([_edit(str(i), "a.py")], {str(i): "已编辑 a.py"}, counts, nudged, 3, True)
        assert out is None


def test_no_nudge_without_trace_tool():
    counts, nudged = {}, set()
    for i in range(4):
        out = detect_stuck_edit([_edit(str(i), "a.py")], {str(i): "🧪 未通过"}, counts, nudged, 3, False)
        assert out is None  # 环境没 trace_run -> 不提示无意义建议


def test_threshold_zero_disables():
    counts, nudged = {}, set()
    for i in range(6):
        assert detect_stuck_edit([_edit(str(i), "a.py")], {str(i): "Traceback"}, counts, nudged, 0, True) is None


def test_counts_per_file_independently():
    counts, nudged = {}, set()
    # 交替改 a、b：各自计数，单独到阈值才提示
    seq = ["a.py", "b.py", "a.py", "b.py", "a.py"]  # a 到第 3 次时提示
    msgs = [detect_stuck_edit([_edit(str(i), p)], {str(i): "未通过"}, counts, nudged, 3, True)
            for i, p in enumerate(seq)]
    fired = [m for m in msgs if m]
    assert len(fired) == 1 and "a.py" in fired[0]
    assert counts["a.py"] == 3 and counts["b.py"] == 2


def _read(i):
    return Call(i, "read_file", {"path": f"f{i}.py"})


# ---- detect_browse_nudge（大库浏览太多 → 提示 search_code）------------------

def test_browse_nudge_fires_after_many_browses():
    state = {}
    # 累计浏览 6 次（_BROWSE_NUDGE_AT）才提示
    msgs = [detect_browse_nudge([_read(i)], state, True, True) for i in range(6)]
    fired = [m for m in msgs if m]
    assert len(fired) == 1 and "search_code" in fired[0]
    # 提示后不再重复
    assert detect_browse_nudge([_read(99)], state, True, True) is None


def test_browse_nudge_suppressed_if_search_used():
    state = {}
    calls = [_read("1"), _read("2"), Call("s", "search_code", {"query": "x"}),
             _read("3"), _read("4"), _read("5"), _read("6"), _read("7")]
    out = detect_browse_nudge(calls, state, True, True)
    assert out is None and state.get("used_search") is True  # 用了 search_code 就不提示


def test_browse_nudge_disabled_when_small_repo():
    state = {}
    for i in range(10):
        assert detect_browse_nudge([_read(i)], state, False, True) is None  # enabled=False（小库）


def test_browse_nudge_needs_search_tool():
    state = {}
    for i in range(10):
        assert detect_browse_nudge([_read(i)], state, True, False) is None  # 无 search_code 工具


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
