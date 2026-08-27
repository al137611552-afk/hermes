"""Provider 工厂：根据 ModelConfig 构造对应实现。"""
from __future__ import annotations

from ..config import AppConfig, ModelConfig
from .anthropic_p import AnthropicProvider
from .base import BaseProvider, Message, StreamEvent, ToolCall, account_problem
from .cassette import (CassetteMiss, cassette_mode, cassette_store, make_replay,
                       wrap_recording)
from .openai_p import OpenAIProvider

__all__ = ["BaseProvider", "CassetteMiss", "Message", "StreamEvent", "ToolCall",
           "account_problem", "build_provider"]

_REGISTRY = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def build_provider(config: AppConfig, model_name: str | None = None) -> BaseProvider:
    mc: ModelConfig = config.get_model(model_name)
    # 录制/回放（ADR 0027 决策 4，块 V3）：环境变量驱动，默认不设 = 完全关闭、零行为改动。
    # **replay 在取 key 之前就返回**——回放不连网、不需要凭据，CI 里没有 key 也能跑。
    mode, store = cassette_mode(), cassette_store()
    if mode == "replay" and store is not None:
        return make_replay(mc.model, store)
    api_key = config.resolve_api_key(mc)
    cls = _REGISTRY[mc.provider]
    # 「限额与预算」里的输出上限覆盖：主模型走 model_max_tokens、子模型走 subagent_max_tokens（0=跟随档）
    max_tokens = mc.max_tokens
    resolved = model_name or config.active_model
    ag = config.agent
    if resolved == config.active_model and getattr(ag, "model_max_tokens", 0):
        max_tokens = ag.model_max_tokens
    elif ag.subagent_model and resolved == ag.subagent_model and getattr(ag, "subagent_max_tokens", 0):
        max_tokens = ag.subagent_max_tokens
    provider = cls(
        model=mc.model,
        api_key=api_key,
        max_tokens=max_tokens,
        base_url=mc.base_url,
        temperature=mc.temperature,
        prompt_cache=mc.prompt_cache,
    )
    if mode == "record" and store is not None:
        return wrap_recording(provider, store)
    return provider
