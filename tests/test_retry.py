"""FR-12.1 provider 韧性：瞬时错误判定 / 退避 / retry_stream / anthropic 重试+缓存降级（无网络）。

运行：python tests/test_retry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.providers import Message, StreamEvent  # noqa: E402
from agentcore.providers import anthropic_p  # noqa: E402
from agentcore.providers.anthropic_p import AnthropicProvider  # noqa: E402
from agentcore.providers.base import (  # noqa: E402
    account_problem, backoff_delay, blocks_retry, explain_stream_failure, is_transient_error,
    retry_stream,
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


# ---- 长考中途断线：thinking 不该封锁重试（2026-08-20 真机） ---------------------
# DeepSeek V4-FLASH 打开已有项目接着开发时"陷入长考"，然后
# `RemoteProtocolError: peer closed connection without sending complete message body`。
# 该错本来就在瞬时清单里，但旧口径是"yield 过任何事件就不重试"——推理模型先吐 thinking，
# 那道门当场被踩掉，于是**这层保护对推理模型形同虚设**，一断就整轮作废。

class _RemoteProtocolError(Exception):
    """httpx 那个类的替身（不引 httpx 依赖）。"""


def test_thinking_does_not_block_retry():
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        yield StreamEvent("thinking", "让我想想…")     # 长考中
        if calls["n"] < 2:
            raise _RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)")
        yield StreamEvent("text", "答案")
        yield StreamEvent("done")

    import agentcore.providers.base as base
    base.time.sleep = lambda s: None
    out = list(retry_stream(make, max_retries=3))
    assert calls["n"] == 2, "thinking 之后断线仍应重试"
    assert [e.type for e in out] == ["thinking", "thinking", "text", "done"], [e.type for e in out]


def test_answer_content_still_blocks_retry():
    """吐过正文/工具调用再断：**不重试**——重来一遍会把答案重复输出给用户。"""
    for blocking in ("text", "tool_use", "done"):
        calls = {"n": 0}

        def make(_b=blocking):
            calls["n"] += 1
            yield StreamEvent(_b, "x")
            raise _RemoteProtocolError("peer closed connection")

        import agentcore.providers.base as base
        base.time.sleep = lambda s: None
        try:
            list(retry_stream(make, max_retries=3))
        except _RemoteProtocolError:
            pass
        else:
            raise AssertionError(f"{blocking} 之后不该重试却没抛")
        assert calls["n"] == 1, (blocking, calls["n"])


def test_blocks_retry_classifies_events():
    assert not blocks_retry(StreamEvent("thinking", "…"))
    for t in ("text", "tool_use", "done"):
        assert blocks_retry(StreamEvent(t, "x")), t


def test_error_text_tells_the_user_what_to_do():
    """原样抛 RemoteProtocolError 对用户等于没说：谁断的、还能怎么办，都得写出来。"""
    msg = explain_stream_failure(
        _RemoteProtocolError("peer closed connection without sending complete message body "
                             "(incomplete chunked read)"), attempt=3)
    assert "对端" in msg and "重试 3 次" in msg
    assert "上下文" in msg or "模型档" in msg
    # 吐过答案那条要说清"为什么没重试"，否则看着像漏了重试
    assert "重复输出" in explain_stream_failure(_RemoteProtocolError("peer closed"),
                                                after_answer=True)
    # 非瞬时错误不该被加戏
    assert explain_stream_failure(ValueError("invalid api key")) == "ValueError: invalid api key"


# ---- 账户级硬错误（2026-08-26 真机：端点 402 被当成 Firecrawl 搜索配额）------------

_402 = ("Error code: 402 - {'error': {'code': 'insufficient_credits', 'message': "
        "'Billing account is not in good standing for this request.', 'type': 'billing_error'}}")


def test_account_problem_classifies():
    class E402(Exception):
        status_code = 402
    class E401(Exception):
        status_code = 401
    class E429(Exception):
        status_code = 429
    assert account_problem(E402(_402)) == "计费"
    assert account_problem(E401("invalid x-api-key")) == "鉴权"
    assert account_problem(_402) == "计费"           # 也吃已成文的错误串
    # 429 是限流不是账户问题：状态码优先，别因为文案里有 quota 就把付费链路判死
    assert account_problem(E429("rate limit quota exceeded")) == ""
    assert is_transient_error(E429("rate limit quota exceeded"))
    # 账户级永不重试
    assert not is_transient_error(E402(_402))
    # 普通异常不该被误吃
    assert account_problem(ValueError("invalid api key")) == ""


def test_account_error_text_names_model_and_rules_out_search_quota():
    """402 只打 `APIStatusError: Error code: 402 - {...}` 会把人往搜索配额上带偏。
    文案必须说清：这是模型 API 的钱/权限问题、哪个模型档、以及委派可能用的是另一个档。"""
    class E402(Exception):
        status_code = 402
    msg = explain_stream_failure(E402(_402), endpoint="kimi-k2.6 @ relay.example.com")
    assert "模型 API" in msg and "计费" in msg
    assert "kimi-k2.6 @ relay.example.com" in msg      # 不指名就查错账户
    assert "Firecrawl" in msg                          # 明确排除搜索配额
    assert "subagent_model" in msg                     # 子 Agent 可能是另一个账户
    assert "不会自动重试" in msg


def test_anthropic_non_transient_yields_explained_error_not_nameerror():
    """回归：`explain_stream_failure` 一度没在 anthropic_p 里导入，于是**非瞬时错误那条路
    一走就 NameError**——用户拿到的不是"账户计费问题"，是一句不知所云的 NameError，
    委派侧还会因此白重试一次。单测都用假 provider，是真跑（3 路并发子 Agent）才把它翻出来的。"""
    class E402(Exception):
        status_code = 402
    p = AnthropicProvider.__new__(AnthropicProvider)      # 不建真 client
    p.model, p.base_url, p.max_tokens = "m", "https://relay.example.com", 16
    p.temperature, p.prompt_cache, p.api_key = None, False, "k"
    def boom(_kwargs):
        raise E402(_402)
        yield  # pragma: no cover — 让它是生成器
    p._stream = boom
    evs = list(p.stream_chat([Message("user", "hi")]))
    assert len(evs) == 1 and evs[0].type == "error"
    assert "模型 API" in evs[0].text and "计费" in evs[0].text
    assert "relay.example.com" in evs[0].text


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
