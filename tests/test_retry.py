"""FR-12.1 provider 韧性：瞬时错误判定 / 退避 / retry_stream / anthropic 重试+缓存降级（无网络）。

运行：python tests/test_retry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.providers import StreamEvent  # noqa: E402
from agentcore.providers import anthropic_p  # noqa: E402
from agentcore.providers.anthropic_p import AnthropicProvider  # noqa: E402
from agentcore.providers.base import (  # noqa: E402
    backoff_delay, is_transient_error, retry_stream,
)


# ---- 瞬时错误判定 ------------------------------------------------------------

def test_is_transient():
    class E429(Exception):
        status_code = 429
    class E400(Exception):
        status_code = 400
    class APIConnectionError(Exception):
        pass
    assert is_transient_error(E429())
    assert not is_transient_error(E400())
    assert is_transient_error(APIConnectionError())
    assert is_transient_error(Exception("Request timed out"))
    assert is_transient_error(Exception("server overloaded, try again"))
    assert not is_transient_error(Exception("invalid api key"))
    assert not is_transient_error(ValueError("bad input"))


def test_backoff_monotonic_and_capped():
    # 抖动下仍应大致随 attempt 增长、且封顶
    d0 = [backoff_delay(0) for _ in range(20)]
    d2 = [backoff_delay(2) for _ in range(20)]
    assert max(d0) <= 1.0 and max(d2) <= 4.0           # base*2^n * [0.5,1)
    assert sum(d2) / 20 > sum(d0) / 20                 # 平均递增
    assert backoff_delay(10) <= 20.0                   # cap


# ---- retry_stream ------------------------------------------------------------

def test_retry_then_succeed():
    calls = {"n": 0}
    def make():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")   # 瞬时、还没 yield
        yield StreamEvent("text", "ok")
        yield StreamEvent("done")
    import agentcore.providers.base as base
    base.time.sleep = lambda s: None         # 别真睡
    out = list(retry_stream(make, max_retries=3))
    assert calls["n"] == 3 and [e.type for e in out] == ["text", "done"]


def test_no_retry_after_first_yield():
    """已经吐过内容再失败：不重试（避免重复输出），原样抛。"""
    calls = {"n": 0}
    def make():
        calls["n"] += 1
        yield StreamEvent("text", "部分")
        raise ConnectionError("mid-stream drop")
    try:
        list(retry_stream(make, max_retries=3))
        assert False
    except ConnectionError:
        pass
    assert calls["n"] == 1                    # 没重试


def test_no_retry_non_transient():
    calls = {"n": 0}
    def make():
        calls["n"] += 1
        raise ValueError("bad request")       # 非瞬时
        yield  # noqa
    try:
        list(retry_stream(make, max_retries=3))
        assert False
    except ValueError:
        pass
    assert calls["n"] == 1


def test_retry_exhausted_raises():
    def make():
        raise TimeoutError("nope")
        yield  # noqa
    import agentcore.providers.base as base
    base.time.sleep = lambda s: None
    try:
        list(retry_stream(make, max_retries=2))
        assert False
    except TimeoutError:
        pass


# ---- anthropic：重试 + 缓存降级共存（假 client） -----------------------------

class _CM:
    def __init__(self, events, raise_with=None):
        self._events, self._raise = events, raise_with
    def __enter__(self):
        if self._raise:
            raise self._raise
        return self
    def __exit__(self, *a):
        return False
    def __iter__(self):
        return iter([])
    def get_final_message(self):
        class F:
            content = []
            stop_reason = "end_turn"
            usage = None
        return F()


def _provider(seq):
    """seq: 每次 stream() 调用要做的事——异常实例 或 'ok'。"""
    p = AnthropicProvider.__new__(AnthropicProvider)
    p.model, p.api_key, p.max_tokens = "m", "k", 16
    p.base_url, p.temperature, p.prompt_cache = "http://x", None, True
    state = {"i": 0, "kwargs": []}
    def stream(**kwargs):
        state["kwargs"].append(kwargs)
        action = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return _CM([], raise_with=action if isinstance(action, Exception) else None)
    p.client = type("C", (), {"messages": type("M", (), {"stream": staticmethod(stream)})()})()
    return p, state


def test_anthropic_transient_retry():
    anthropic_p._CACHE_UNSUPPORTED.clear()
    import agentcore.providers.base as base
    base.time.sleep = lambda s: None
    class Conn(Exception):
        status_code = 503
    p, state = _provider([Conn(), Conn(), "ok"])   # 前两次 503，第三次成功
    events = list(p.stream_chat([Message := __import__("agentcore.providers", fromlist=["Message"]).Message("user", "hi")]))
    assert any(e.type == "done" for e in events) and state["i"] == 3


def test_anthropic_cache_degrade_then_retry():
    """先 cache 错（摘缓存重试，不计退避），再瞬时错（退避重试），最后成功。"""
    anthropic_p._CACHE_UNSUPPORTED.clear()
    import agentcore.providers.base as base
    base.time.sleep = lambda s: None
    from agentcore.providers import Message
    class CacheErr(Exception):
        pass
    class Conn(Exception):
        status_code = 429
    p, state = _provider([CacheErr("invalid cache_control"), Conn(), "ok"])
    events = list(p.stream_chat([Message("user", "hi")]))
    assert any(e.type == "done" for e in events)
    assert ("http://x", "m") in anthropic_p._CACHE_UNSUPPORTED   # 记下不支持缓存
    # 第一次带缓存断点、之后不带
    assert "cache_control" in str(state["kwargs"][0])
    assert "cache_control" not in str(state["kwargs"][-1])
    anthropic_p._CACHE_UNSUPPORTED.clear()


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
