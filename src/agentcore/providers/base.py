"""模型适配层的统一接口。

设计要点：UI 和 agent 内核只依赖 BaseProvider，
切换 Claude / OpenAI 兼容模型只是换一个实现 + 换配置。
P3 在这里扩展了 tool-use；P4 将扩展 vision content block。
"""
from __future__ import annotations

import random
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

# ---- provider 韧性（FR-12.1）：瞬时错误自动退避重试 -------------------------
MAX_RETRIES = 3                                  # 瞬时错误最多重试次数
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_TRANSIENT_NAMES = (
    "APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError",
    "ServiceUnavailable", "Timeout", "ConnectionError", "ConnectionResetError",
    "ReadTimeout", "RemoteProtocolError", "Overloaded",
)
_TRANSIENT_MSGS = (
    "timeout", "timed out", "connection reset", "connection aborted", "temporarily",
    "overloaded", "rate limit", "too many requests", "try again", "503", "502", "529",
)


def is_transient_error(exc: Exception) -> bool:
    """是否为值得重试的瞬时错误（网络抖动 / 429 限流 / 5xx 服务端）。"""
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(code, int) and code in _TRANSIENT_STATUS:
        return True
    if any(n in type(exc).__name__ for n in _TRANSIENT_NAMES):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_MSGS)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 20.0) -> float:
    """指数退避 + 抖动（attempt 从 0 起）：base*2^attempt，封顶 cap，乘 [0.5,1) 抖动。"""
    return min(cap, base * (2 ** attempt)) * (0.5 + random.random() * 0.5)


# **只有答案内容才封锁重试**。thinking 是过程不是答案：重来一遍最坏是思考段重复一次，
# 而不重试＝整轮作废。2026-08-20 真机踩到的正是这条——推理模型（DeepSeek V4-FLASH）
# 长考期间对端断开（RemoteProtocolError: incomplete chunked read），旧逻辑因为
# "已 yield 过事件"（那是 thinking）直接放弃重试，用户看到的就是一轮白跑。
_ANSWER_EVENTS = ("text", "tool_use", "done")


def blocks_retry(ev) -> bool:
    """这个事件是否让"重来一遍"变得不可接受（会重复输出给用户的东西）。"""
    return getattr(ev, "type", "") in _ANSWER_EVENTS


def explain_stream_failure(e, attempt: int = 0, after_answer: bool = False) -> str:
    """把流式失败翻成**能照着做点什么**的一句话（纯函数）。

    原样抛 `RemoteProtocolError: peer closed connection without sending complete message body`
    对用户等于没说——它既不说明谁断的，也不说明还能怎么办。
    """
    name = type(e).__name__
    base = f"{name}: {e}"
    tail = ""
    if "incompletechunked" in str(e).replace(" ", "").replace("_", "").lower() \
            or name == "RemoteProtocolError":
        tail = ("——**对端在流式过程中断开**（不是本地网络断）。常见于长思考期间中间代理的"
                "空闲超时：可换个模型档、或把上下文压小些再试")
    elif is_transient_error(e):
        tail = "——瞬时故障"
    if tail:
        if after_answer:
            tail += "。已吐出部分答案，故未自动重试（重试会重复输出）"
        elif attempt:
            tail += f"。已自动重试 {attempt} 次仍失败"
    return base + tail


def retry_stream(make_stream, *, max_retries: int = MAX_RETRIES, label: str = ""):
    """重试生成器：**在还没吐出答案内容时**对瞬时错误退避重试。

    thinking 增量不算答案内容（见 `blocks_retry`）——推理模型长考中途断线是最常见的一种
    失败，若把它算进去，这层保护对推理模型等于不存在。

    make_stream() 每次调用返回一个全新的流（StreamEvent 迭代器）；流中途失败、或非瞬时
    错误、或重试用尽 → 原样抛出，由调用方转成 error 事件。
    """
    attempt = 0
    while True:
        yielded = False
        try:
            for ev in make_stream():
                yielded = yielded or blocks_retry(ev)
                yield ev
            return
        except Exception as e:  # noqa: BLE001
            if yielded or attempt >= max_retries or not is_transient_error(e):
                raise
            delay = backoff_delay(attempt)
            print(f"[provider{(' '+label) if label else ''}] 瞬时错误，"
                  f"{delay:.1f}s 后重试（第 {attempt + 1}/{max_retries} 次）："
                  f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(delay)
            attempt += 1


@dataclass
class Message:
    """统一消息格式。

    content 可以是：
    - 纯文本 str（普通对话）；
    - content blocks list[dict]（tool-use 往返：assistant 的 tool_use、
      user 的 tool_result；P4 多模态的图片块也走这里）。
    两个 provider 各自把它翻译成自家 API 需要的形状。
    """
    role: Literal["user", "assistant"]
    content: Any


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""
    id: str
    name: str
    input: dict


@dataclass
class StreamEvent:
    """流式事件。

    - text：文本增量（meta 空）。
    - thinking：模型推理过程增量（仅展示，不计入答案、不持久化）。部分模型/端点才有。
    - tool_use：模型要求调用工具，meta={"call": ToolCall}。
    - done：本轮（一次 API 调用）结束，meta={"stop_reason": str}；
      agent 循环据此判断是否还要继续（stop_reason=="tool_use" 时继续）。
    - error：出错，text 为可读错误信息。
    """
    type: Literal["text", "thinking", "tool_use", "done", "error"]
    text: str = ""
    meta: dict = field(default_factory=dict)


class BaseProvider(ABC):
    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        max_tokens: int = 4096,
        base_url: str | None = None,
        temperature: float | None = None,
        prompt_cache: bool = True,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.base_url = base_url
        self.temperature = temperature
        # FR-10.4b：anthropic 协议加 cache_control 前缀缓存；openai 端点自动缓存、忽略本开关
        self.prompt_cache = prompt_cache

    @abstractmethod
    def stream_chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[StreamEvent]:
        """流式返回一轮模型输出。

        tools 为统一的工具 schema 列表（registry.to_schemas() 产出），
        None 或空表示本轮不带工具（P1 纯对话路径不受影响）。

        实现约定：
        - 文本增量逐段 yield StreamEvent("text", ...)；
        - 每个工具调用 yield StreamEvent("tool_use", meta={"call": ToolCall});
        - 结束时 yield StreamEvent("done", meta={"stop_reason": ...})；
        - 出错时 yield StreamEvent("error", <可读信息>) 并返回。
        """
        raise NotImplementedError
