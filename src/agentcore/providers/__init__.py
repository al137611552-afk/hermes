"""Provider 工厂：根据 ModelConfig 构造对应实现。"""
from __future__ import annotations

from ..config import AppConfig, ModelConfig
from .anthropic_p import AnthropicProvider
from .base import BaseProvider, Message, StreamEvent, ToolCall
from .openai_p import OpenAIProvider

__all__ = ["BaseProvider", "Message", "StreamEvent", "ToolCall", "build_provider"]

_REGISTRY = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def build_provider(config: AppConfig, model_name: str | None = None) -> BaseProvider:
    mc: ModelConfig = config.get_model(model_name)
    api_key = config.resolve_api_key(mc)
    cls = _REGISTRY[mc.provider]
    return cls(
        model=mc.model,
        api_key=api_key,
        max_tokens=mc.max_tokens,
        base_url=mc.base_url,
        temperature=mc.temperature,
        prompt_cache=mc.prompt_cache,
    )
