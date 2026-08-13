"""会话级工具预算（budget.py）+ 接进 AgentLoop 的闸。

运行：python tests/test_budget.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.budget import DEFAULT_LIMITS, ToolBudget, build_limits  # noqa: E402


def test_counts_and_stops_at_limit():
    b = ToolBudget({"web_search": 3})
    assert b.consume("web_search") is None
    assert b.consume("web_search") is None
    assert b.consume("web_search") is None
    over = b.consume("web_search")
    assert over is not None
    assert "预算用尽" in over and "web_search" in over and "3" in over
    # 撞上限那次不计入已用数：显示 3/3 而不是 4/3
    assert b.used("web_search") == 3
    # 再撞仍然是同样的事实，计数不继续涨
    assert b.consume("web_search") is not None
    assert b.used("web_search") == 3


def test_limits_are_per_tool():
    b = ToolBudget({"web_search": 1, "delegate": 2})
    assert b.consume("web_search") is None
    assert b.consume("web_search") is not None       # web_search 尽了
    assert b.consume("delegate") is None             # delegate 不受影响
    assert b.consume("delegate") is None
    assert b.consume("delegate") is not None
    assert b.snapshot() == {"web_search": 1, "delegate": 2}


def test_unlimited_tools_never_blocked():
    b = ToolBudget({"web_search": 0, "delegate": -1})
    for _ in range(500):
        assert b.consume("web_search") is None
        assert b.consume("delegate") is None
        assert b.consume("read_file") is None        # 未配置 = 不限
    assert b.limit_of("read_file") == 0


def test_concurrent_consume_never_exceeds_limit():
    """子 Agent 并发调用时，计数+判定必须在同一把锁内——否则会一起挤过上限。"""
    b = ToolBudget({"delegate": 50})
    granted = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            if b.consume("delegate") is None:
                with lock:
                    granted.append(1)

    ts = [threading.Thread(target=worker) for _ in range(10)]   # 10 线程 × 20 次 = 200 次尝试
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(granted) == 50, len(granted)       # 恰好放行 50 次，不多不少
    assert b.used("delegate") == 50


def test_build_limits_from_config():
    assert build_limits(200, 200) == {"web_search": 200, "delegate": 200}
    assert build_limits(0, 0) == {"web_search": 0, "delegate": 0}       # 全关
    assert build_limits(-5, None) == {"web_search": 0, "delegate": 0}   # 脏值归 0（不限）
    assert set(DEFAULT_LIMITS) == {"web_search", "delegate"}


# ---- 接进 AgentLoop --------------------------------------------------------

class _FakeTool:
    name = "web_search"
    dangerous = False
    parallel_safe = False

    def __init__(self) -> None:
        self.calls = 0

    def run(self, params, **kw):
        self.calls += 1
        return "搜索结果"


class _FakeRegistry:
    def __init__(self, tool) -> None:
        self._t = tool

    def get(self, name):
        return self._t

    def names(self):
        return [self._t.name]

    def to_schemas(self):
        return []


def _loop(tool, budget):
    from agentcore.agent.loop import AgentLoop
    return AgentLoop(provider=None, registry=_FakeRegistry(tool), gate=None,
                     tool_budget=budget)


def test_loop_blocks_tool_when_budget_exhausted():
    tool = _FakeTool()
    loop = _loop(tool, ToolBudget({"web_search": 2}))
    for _ in range(2):
        text, ok, _ = loop._exec_tool("web_search", {"query": "x"})
        assert ok and text == "搜索结果"
    text, ok, _ = loop._exec_tool("web_search", {"query": "x"})
    assert not ok
    assert "预算用尽" in text
    assert tool.calls == 2          # 第三次**真的没执行**（不是执行完再报）


def test_loop_without_budget_is_unchanged():
    """tool_budget=None（存量调用方/测试）→ 完全不计数，行为零变化。"""
    tool = _FakeTool()
    loop = _loop(tool, None)
    for _ in range(20):
        text, ok, _ = loop._exec_tool("web_search", {"query": "x"})
        assert ok and text == "搜索结果"
    assert tool.calls == 20


def test_main_and_subagent_share_one_budget():
    """要害：主 Agent 与子 Agent 共用同一实例，子 Agent 的搜索也计入总数。"""
    budget = ToolBudget({"web_search": 3})
    main_tool, sub_tool = _FakeTool(), _FakeTool()
    main, sub = _loop(main_tool, budget), _loop(sub_tool, budget)
    assert main._exec_tool("web_search", {})[1] is True
    assert sub._exec_tool("web_search", {})[1] is True
    assert sub._exec_tool("web_search", {})[1] is True
    # 预算已被两边合计用尽——主 Agent 这次也该被挡
    text, ok, _ = main._exec_tool("web_search", {})
    assert not ok and "预算用尽" in text
    assert main_tool.calls == 1 and sub_tool.calls == 2


def test_config_has_budget_keys():
    """config 字段 + 设置面板条目都在（防只加字段忘了露出来）。"""
    from agentcore.config import LIMITS_SPEC, AgentConfig
    a = AgentConfig()
    assert a.max_web_searches_per_session == 200
    assert a.max_delegates_per_session == 200
    keys = {k["key"] for k in LIMITS_SPEC}
    assert "agent.max_web_searches_per_session" in keys
    assert "agent.max_delegates_per_session" in keys


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
