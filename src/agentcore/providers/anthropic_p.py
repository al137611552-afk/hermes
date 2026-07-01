"""Anthropic (Claude) 适配。

工具 schema 采用 Anthropic 原生格式：{name, description, input_schema}。
registry.to_schemas() 直接产出这种形状，本实现透传即可。

FR-10.4b prompt caching：默认给请求加三个 cache_control 断点（system 末块 / tools 末项 /
最后一条消息末块），前缀按最长匹配复用——长会话/多轮工具循环的输入大头逐轮命中缓存。
实测方舟 coding 端点支持（cache_read_input_tokens 真实命中）；请求小于端点缓存门槛时
安静跳过、无副作用。不支持 cache_control 的端点：请求未产出任何事件就失败时降级重试
一次（无缓存），错误含 cache 字样则记入模块级不支持名单、后续不再尝试。
"""
from __future__ import annotations

import sys
import time
from typing import Iterator

import anthropic

from .base import (
    MAX_RETRIES, BaseProvider, Message, StreamEvent, ToolCall,
    backoff_delay, is_transient_error,
)

# 实测不支持 cache_control 的端点（base_url, model）；进程级记账，避免每轮白付一次失败
_CACHE_UNSUPPORTED: set[tuple[str, str]] = set()

_EPHEMERAL = {"type": "ephemeral"}


def _usage(u) -> "dict | None":
    """规范化 Anthropic usage → {input, output, cache_read}（FR-11.8）；None 安全。"""
    if u is None:
        return None
    return {
        "input": getattr(u, "input_tokens", 0) or 0,
        "output": getattr(u, "output_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
    }


def apply_cache_breakpoints(kwargs: dict) -> dict:
    """返回打了缓存断点的请求 kwargs 拷贝（不改原对象/原 history）。

    断点：system 末块、tools 末项、最后一条消息的末块（断点逐轮后移，
    旧前缀仍按最长匹配命中）。
    """
    out = dict(kwargs)
    system = out.get("system")
    if isinstance(system, str) and system:
        out["system"] = [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]
    tools = out.get("tools")
    if tools:
        tools = list(tools)
        tools[-1] = {**tools[-1], "cache_control": _EPHEMERAL}
        out["tools"] = tools
    messages = out.get("messages")
    if messages:
        messages = list(messages)
        last = dict(messages[-1])
        content = last.get("content")
        if isinstance(content, str) and content:
            last["content"] = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
        elif isinstance(content, list) and content and isinstance(content[-1], dict):
            blocks = list(content)
            blocks[-1] = {**blocks[-1], "cache_control": _EPHEMERAL}
            last["content"] = blocks
        messages[-1] = last
        out["messages"] = messages
    return out


class AnthropicProvider(BaseProvider):
    def __init__(self, model, api_key, *, max_tokens=4096, base_url=None, temperature=None,
                 prompt_cache=True):
        super().__init__(model, api_key, max_tokens=max_tokens, base_url=base_url,
                         temperature=temperature, prompt_cache=prompt_cache)
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)

    def _stream(self, kwargs: dict) -> Iterator[StreamEvent]:
        """单次流式调用；出错直接抛（由 stream_chat 决定降级重试或转 error 事件）。"""
        with self.client.messages.stream(**kwargs) as stream:
            # 遍历原始事件而非 text_stream：除文本增量外，还能拿到 thinking_delta
            # （模型推理过程）。端点不产出 thinking 时行为与原来一致（只有 text）。
            for event in stream:
                if getattr(event, "type", "") != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta":
                    yield StreamEvent("text", getattr(delta, "text", ""))
                elif dtype == "thinking_delta":
                    yield StreamEvent("thinking", getattr(delta, "thinking", ""))
            final = stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                yield StreamEvent(
                    "tool_use",
                    meta={"call": ToolCall(id=block.id, name=block.name, input=dict(block.input))},
                )
        yield StreamEvent("done", meta={"stop_reason": final.stop_reason,
                                        "usage": _usage(getattr(final, "usage", None))})

    def stream_chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[StreamEvent]:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        cache_key = (self.base_url or "", self.model)
        use_cache = self.prompt_cache and cache_key not in _CACHE_UNSUPPORTED
        # 统一循环：cache_control 不被端点接受 → 摘掉缓存断点重试（不计入退避预算）；
        # 网络抖动/429/5xx → 指数退避重试；**仅在还没吐内容前**重试，否则照常报错（FR-12.1）。
        attempt = 0
        while True:
            variant = apply_cache_breakpoints(kwargs) if use_cache else kwargs
            yielded = False
            try:
                for ev in self._stream(variant):
                    yielded = True
                    yield ev
                return
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if use_cache and ("cache" in msg or not yielded):
                    # 端点不认 cache_control（或开局即挂）：摘缓存重试一次
                    if "cache" in msg:
                        _CACHE_UNSUPPORTED.add(cache_key)
                    use_cache = False
                    if not yielded:
                        continue
                if yielded or attempt >= MAX_RETRIES or not is_transient_error(e):
                    yield StreamEvent("error", f"{type(e).__name__}: {e}")
                    return
                delay = backoff_delay(attempt)
                print(f"[provider {self.model}] 瞬时错误，{delay:.1f}s 后重试"
                      f"（第 {attempt + 1}/{MAX_RETRIES} 次）：{type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                time.sleep(delay)
                attempt += 1
