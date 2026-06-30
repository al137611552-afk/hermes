"""FR-10.4b prompt caching：缓存断点改写（纯函数）+ 降级重试与不支持名单（假 client，无网络）。

运行：python tests/test_cache.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.providers import Message  # noqa: E402
from agentcore.providers import anthropic_p  # noqa: E402
from agentcore.providers.anthropic_p import (  # noqa: E402
    AnthropicProvider, apply_cache_breakpoints,
)


# ---- 断点改写（纯函数） -------------------------------------------------------

def test_breakpoints_on_system_tools_last_message():
    kwargs = {
        "model": "m", "max_tokens": 10,
        "system": "你是助手",
        "tools": [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}],
        "messages": [{"role": "user", "content": "你好"},
                     {"role": "user", "content": "再见"}],
    }
    out = apply_cache_breakpoints(kwargs)
    cc = {"type": "ephemeral"}
    assert out["system"] == [{"type": "text", "text": "你是助手", "cache_control": cc}]
    assert "cache_control" not in out["tools"][0] and out["tools"][1]["cache_control"] == cc
    assert out["messages"][0] == {"role": "user", "content": "你好"}     # 只动最后一条
    assert out["messages"][1]["content"][-1]["cache_control"] == cc


def test_breakpoints_block_content_and_no_mutation():
    blocks = [{"type": "tool_result", "tool_use_id": "1", "content": "ok"},
              {"type": "text", "text": "继续"}]
    kwargs = {"messages": [{"role": "user", "content": blocks}],
              "tools": [{"name": "a"}], "system": "s"}
    out = apply_cache_breakpoints(kwargs)
    assert out["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # 原对象（history 引用的就是它们）必须原封不动
    assert "cache_control" not in blocks[-1]
    assert "cache_control" not in kwargs["tools"][0]
    assert kwargs["system"] == "s" and isinstance(kwargs["system"], str)


def test_breakpoints_edge_shapes():
    out = apply_cache_breakpoints({"messages": [], "system": ""})
    assert out["messages"] == [] and out["system"] == ""     # 空形态不崩、不乱加
    out2 = apply_cache_breakpoints({"messages": [{"role": "user", "content": []}]})
    assert out2["messages"][0]["content"] == []


# ---- 降级重试 / 不支持名单（假 client） ----------------------------------------

class _FakeStreamCM:
    """模拟 client.messages.stream(...) 的上下文管理器与事件流。"""

    def __init__(self, fail_with: "str | None" = None):
        self._fail = fail_with

    def __enter__(self):
        if self._fail:
            raise RuntimeError(self._fail)
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        class _Delta:
            type = "text_delta"
            text = "hi"
        class _Ev:
            type = "content_block_delta"
            delta = _Delta()
        return iter([_Ev()])

    def get_final_message(self):
        class _Final:
            content = []
            stop_reason = "end_turn"
        return _Final()


class _FakeMessages:
    def __init__(self, fail_first_with: "str | None"):
        self.calls: list[dict] = []
        self._fail_first = fail_first_with

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_first and len(self.calls) == 1:
            return _FakeStreamCM(fail_with=self._fail_first)
        return _FakeStreamCM()


def _provider(fail_first_with=None, *, prompt_cache=True, model="m1", base_url="http://x"):
    p = AnthropicProvider.__new__(AnthropicProvider)  # 跳过真实 SDK 构造
    p.model, p.api_key, p.max_tokens = model, "k", 16
    p.base_url, p.temperature, p.prompt_cache = base_url, None, prompt_cache
    fake = _FakeMessages(fail_first_with)
    p.client = type("C", (), {"messages": fake})()
    return p, fake


def test_cache_rejected_falls_back_and_remembers():
    anthropic_p._CACHE_UNSUPPORTED.clear()
    p, fake = _provider("invalid parameter: cache_control", model="mA")
    events = list(p.stream_chat([Message("user", "hi")], system="s"))
    assert [e.type for e in events] == ["text", "done"]      # 降级重试成功
    assert "cache_control" in str(fake.calls[0]) and "cache_control" not in str(fake.calls[1])
    assert ("http://x", "mA") in anthropic_p._CACHE_UNSUPPORTED
    # 第二轮：直接不带缓存，不再白付失败调用
    p2, fake2 = _provider(None, model="mA")
    list(p2.stream_chat([Message("user", "hi")], system="s"))
    assert len(fake2.calls) == 1 and "cache_control" not in str(fake2.calls[0])
    anthropic_p._CACHE_UNSUPPORTED.clear()


def test_transient_error_retries_but_not_remembered():
    anthropic_p._CACHE_UNSUPPORTED.clear()
    p, fake = _provider("connection reset", model="mB")
    events = list(p.stream_chat([Message("user", "hi")]))
    assert [e.type for e in events] == ["text", "done"]      # 开局失败也重试一次
    assert ("http://x", "mB") not in anthropic_p._CACHE_UNSUPPORTED  # 非 cache 错不记账
    assert len(fake.calls) == 2


def test_prompt_cache_off_no_breakpoints():
    p, fake = _provider(None, prompt_cache=False, model="mC")
    events = list(p.stream_chat([Message("user", "hi")], system="s"))
    assert [e.type for e in events] == ["text", "done"]
    assert len(fake.calls) == 1 and "cache_control" not in str(fake.calls[0])


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
