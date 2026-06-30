"""块H2：detect_low_quality_research 自检（搜索不达标 → 催重搜的决策接线）。

独立 runner：`python tests/test_research_refine.py`。纯逻辑、无模型、无 IO。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.loop import detect_low_quality_research  # noqa: E402


class _Call:
    def __init__(self, i, name, params):
        self.id, self.name, self.input = str(i), name, params


# 超预算搜索结果（无一在 500 内）
_MISS = ("[搜索结果·bing] 小红书 618 女士睡衣\n"
         "1. 真丝睡衣\n   http://a\n   ¥899\n"
         "2. 设计师款\n   http://b\n   1280元")
# 达标结果（有在预算内）
_OK = ("[搜索结果·bing] 女士睡衣\n"
       "1. 纯棉睡衣\n   http://a\n   ¥199\n"
       "2. 真丝款\n   http://b\n   899元")


def test_budget_miss_triggers_refine_nudge():
    calls = [_Call(1, "web_search", {"query": "618女士睡衣 500元以内"})]
    out = {"1": _MISS}
    msg = detect_low_quality_research(calls, out, {}, max_nudges=1)
    assert msg is not None
    assert "不达标" in msg and ("换" in msg)


def test_satisfied_search_no_nudge():
    calls = [_Call(1, "web_search", {"query": "女士睡衣 500元以内"})]
    msg = detect_low_quality_research(calls, {"1": _OK}, {}, max_nudges=1)
    assert msg is None


def test_no_budget_no_nudge():
    # 没给预算约束 → 不催重搜（H1 不产 blocker issue）
    calls = [_Call(1, "web_search", {"query": "好看的女士睡衣推荐"})]
    msg = detect_low_quality_research(calls, {"1": _MISS}, {}, max_nudges=1)
    assert msg is None


def test_nudge_capped_per_query():
    calls = [_Call(1, "web_search", {"query": "618女士睡衣 500元以内"})]
    out = {"1": _MISS}
    state = {}
    first = detect_low_quality_research(calls, out, state, max_nudges=1)
    second = detect_low_quality_research(calls, out, state, max_nudges=1)
    assert first is not None and second is None      # 同 query 封顶 1 次


def test_different_query_gets_own_nudge():
    state = {}
    c1 = [_Call(1, "web_search", {"query": "618女士睡衣 500元以内"})]
    c2 = [_Call(2, "web_search", {"query": "女士睡衣 推荐 不超过500"})]
    m1 = detect_low_quality_research(c1, {"1": _MISS}, state, max_nudges=1)
    m2 = detect_low_quality_research(c2, {"2": _MISS}, state, max_nudges=1)
    assert m1 is not None and m2 is not None         # 换了关键词=新 query=另起计数


def test_non_web_search_ignored():
    calls = [_Call(1, "grep_search", {"pattern": "x"})]
    msg = detect_low_quality_research(calls, {"1": "1.\n2.\n3."}, {}, max_nudges=1)
    assert msg is None


def test_loop_construct_default_off():
    # AgentLoop 默认 research_refine=False → 存量行为零变化（同 auto_retry/failure_memory 纪律）
    from agentcore.agent.loop import AgentLoop
    import inspect
    sig = inspect.signature(AgentLoop.__init__)
    assert sig.parameters["research_refine"].default is False


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    fns.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
