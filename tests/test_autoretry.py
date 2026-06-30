"""块D 工具调用级自动重试自测（docs/adr/0014 块D）——policy 规则 + loop 接线。

（与 test_retry.py 不同：那是 provider 级流式重试；这是工具调用级瞬时 IO 重试。）
运行：python tests/test_autoretry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.policy import RetryDecision, decide_retry  # noqa: E402
from agentcore.agent.taxonomy import ErrorClass  # noqa: E402


# ---- decide_retry：第一条 Need→Decision 硬规则 ------------------------------

def test_retry_only_for_transient():
    for cls in (ErrorClass.LOGIC, ErrorClass.SYNTAX, ErrorClass.AUTH, ErrorClass.NOT_FOUND):
        assert decide_retry([cls], 1, max_attempts=2, backoff_base=0.5) is None


def test_retry_transient_within_budget():
    d = decide_retry([ErrorClass.TRANSIENT_IO], 1, max_attempts=2, backoff_base=0.5)
    assert isinstance(d, RetryDecision) and d.attempt == 1 and d.label == "RETRY_WITH_BACKOFF"
    assert d.delay == 0.5          # base * 2^0


def test_retry_exponential_backoff():
    d2 = decide_retry([ErrorClass.TRANSIENT_IO], 2, max_attempts=3, backoff_base=0.5)
    assert d2.delay == 1.0         # base * 2^1


def test_retry_stops_at_max():
    assert decide_retry([ErrorClass.TRANSIENT_IO], 3, max_attempts=2, backoff_base=0.5) is None


def test_retry_disabled_when_max_zero():
    assert decide_retry([ErrorClass.TRANSIENT_IO], 1, max_attempts=0, backoff_base=0.5) is None


def test_retry_transient_among_multiple_classes():
    d = decide_retry([ErrorClass.LOGIC, ErrorClass.TRANSIENT_IO], 1, max_attempts=2, backoff_base=0.1)
    assert d is not None


# ---- loop 接线：_exec_tool_with_retry --------------------------------------

def _make_loop(auto_retry=True, max_attempts=2):
    from agentcore.agent.loop import AgentLoop
    loop = AgentLoop.__new__(AgentLoop)   # 跳过 __init__ 的 provider 依赖
    loop.auto_retry = auto_retry
    loop.retry_max_attempts = max_attempts
    loop.retry_backoff_base = 0.0         # 测试不真 sleep
    loop._sleep = lambda s: None
    return loop


def test_loop_retries_transient_then_succeeds():
    loop = _make_loop()
    n = {"i": 0}

    def fake_exec(name, params):
        n["i"] += 1
        if n["i"] < 3:
            return ("[exit code] 1\n[stderr]\nConnection refused", True, [])
        return ("[exit code] 0\n[stdout]\nok", True, [])

    loop._exec_tool = fake_exec
    events = []
    out = loop._exec_tool_with_retry("run_shell", {}, emit=lambda e, d: events.append((e, d)),
                                     call=type("C", (), {"id": "x"})())
    assert n["i"] == 3                              # 首次 + 2 次重试
    assert "[exit code] 0" in out[0]               # 最终成功
    assert [e for e, _ in events].count("tool_retry") == 2


def test_loop_gives_up_after_max_and_returns_last_failure():
    loop = _make_loop(max_attempts=2)
    n = {"i": 0}

    def fake_exec(name, params):
        n["i"] += 1
        return ("[exit code] 1\n[stderr]\ntimed out", True, [])

    loop._exec_tool = fake_exec
    out = loop._exec_tool_with_retry("run_shell", {}, emit=None, call=None)
    assert n["i"] == 3                              # 1 + max_attempts(2)，不无限
    assert "[exit code] 1" in out[0]               # 撞上限 → 返回最后失败，交上层


def test_loop_does_not_retry_logic_failure():
    loop = _make_loop()
    n = {"i": 0}

    def fake_exec(name, params):
        n["i"] += 1
        return ("==== 1 failed, 2 passed ====\nAssertionError", True, [])

    loop._exec_tool = fake_exec
    loop._exec_tool_with_retry("run_shell", {}, emit=None, call=None)
    assert n["i"] == 1                             # 逻辑失败不重试


def test_loop_auto_retry_off_executes_once():
    loop = _make_loop(auto_retry=False)
    n = {"i": 0}

    def fake_exec(name, params):
        n["i"] += 1
        return ("[exit code] 1\n[stderr]\nConnection refused", True, [])

    loop._exec_tool = fake_exec
    loop._exec_tool_with_retry("run_shell", {}, emit=None, call=None)
    assert n["i"] == 1                             # 关掉 → 不重试


def test_loop_does_not_retry_success():
    loop = _make_loop()
    n = {"i": 0}

    def fake_exec(name, params):
        n["i"] += 1
        return ("[exit code] 0\n[stdout]\nfine", True, [])

    loop._exec_tool = fake_exec
    loop._exec_tool_with_retry("run_shell", {}, emit=None, call=None)
    assert n["i"] == 1                             # 成功不重试


def test_loop_retries_hard_toolerror_transient():
    # ok=False 的硬错误（无 Evaluator）也走裸分类兜底
    loop = _make_loop()
    n = {"i": 0}

    def fake_exec(name, params):
        n["i"] += 1
        return ("连接超时", False, []) if n["i"] == 1 else ("done", True, [])

    loop._exec_tool = fake_exec
    loop._exec_tool_with_retry("web_fetch", {}, emit=None, call=None)
    assert n["i"] == 2                             # 硬瞬时错误重试一次后成功


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
