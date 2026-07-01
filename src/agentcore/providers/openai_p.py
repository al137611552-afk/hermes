"""OpenAI 兼容适配（OpenAI / DeepSeek / 各类中转，靠 base_url 区分）。

内部规范格式统一用 Anthropic 风格的 content blocks（见 base.Message）：
- assistant 的 tool_use block、user 的 tool_result block。
本实现负责把它们与工具 schema 翻译成 OpenAI 的 function-calling 形状。
"""
from __future__ import annotations

import json
from typing import Iterator

from openai import OpenAI

from .base import BaseProvider, Message, StreamEvent, ToolCall, retry_stream


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Anthropic 原生 {name, description, input_schema} -> OpenAI function 格式。"""
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def _messages_to_openai(messages: list[Message]) -> list[dict]:
    """内部 Message（含 Anthropic 风格 content blocks）-> OpenAI messages。"""
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
            continue

        # content 是 blocks 列表
        if m.role == "assistant":
            text_parts, tool_calls = [], []
            for b in m.content:
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                    })
            msg: dict = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        else:  # user：tool_result 各拆成 role=tool 消息；text/image 合成一条 user
            parts = []
            for b in m.content:
                if b.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": b["tool_use_id"],
                        "content": b.get("content", ""),
                    })
                elif b.get("type") == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "image":
                    src = b.get("source", {})
                    url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            if parts:
                # 全是纯文本时退化成字符串，否则用 OpenAI vision 的数组格式
                if all(p["type"] == "text" for p in parts):
                    out.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
                else:
                    out.append({"role": "user", "content": parts})
    return out


class OpenAIProvider(BaseProvider):
    def __init__(self, model, api_key, *, max_tokens=4096, base_url=None, temperature=None,
                 prompt_cache=True):  # openai 端点自动前缀缓存，本开关无需使用（FR-10.4b）
        super().__init__(model, api_key, max_tokens=max_tokens, base_url=base_url,
                         temperature=temperature, prompt_cache=prompt_cache)
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def stream_chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[StreamEvent]:
        api_messages = []
        if system:
            api_messages.append({"role": "system", "content": system})
        api_messages += _messages_to_openai(messages)

        kwargs = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if tools:
            kwargs["tools"] = _tools_to_openai(tools)

        # 瞬时错误（网络抖动/429/5xx）在吐内容前自动退避重试（FR-12.1）
        try:
            yield from retry_stream(lambda: self._stream(kwargs), label=self.model)
        except Exception as e:  # noqa: BLE001 — 重试用尽/非瞬时错误：转 error 事件
            yield StreamEvent("error", f"{type(e).__name__}: {e}")

    def _stream(self, kwargs: dict) -> Iterator[StreamEvent]:
        """单次流式调用；出错直接抛（由 stream_chat 决定重试或转 error）。"""
        stream = self.client.chat.completions.create(**kwargs)
        acc: dict[int, dict] = {}   # 累积流式 tool_calls 分片：index -> {id, name, args(str)}
        finish_reason = None
        usage_obj = None
        for chunk in stream:
            # 用量（FR-11.8，尽力而为）：部分端点会在末尾 chunk 自带 usage；
            # 不强加 stream_options 以免打挂不支持的端点，自然带就取。
            if getattr(chunk, "usage", None):
                usage_obj = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                yield StreamEvent("text", delta.content)
            # 部分模型（如 deepseek-reasoner）单独流出推理内容；有就转发供前端展示
            reasoning = getattr(delta, "reasoning_content", None) if delta else None
            if reasoning:
                yield StreamEvent("thinking", reasoning)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        for slot in acc.values():
            try:
                parsed = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                parsed = {}
            yield StreamEvent(
                "tool_use",
                meta={"call": ToolCall(id=slot["id"], name=slot["name"], input=parsed)},
            )
        # 归一到 Anthropic 语义。注意：finish_reason=="length" 表示输出被 max_tokens
        # 截断（哪怕同时有部分 tool_call），必须如实上报，否则 agent 循环会执行残缺的
        # 工具调用并陷入重试死循环；故截断优先于 tool_use。
        if finish_reason == "length":
            stop_reason = "length"
        elif acc:
            stop_reason = "tool_use"
        else:
            stop_reason = finish_reason or "end_turn"
        usage = None
        if usage_obj is not None:
            usage = {
                "input": getattr(usage_obj, "prompt_tokens", 0) or 0,
                "output": getattr(usage_obj, "completion_tokens", 0) or 0,
                "cache_read": 0,
            }
        yield StreamEvent("done", meta={"stop_reason": stop_reason, "usage": usage})
