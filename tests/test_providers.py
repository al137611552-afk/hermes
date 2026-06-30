"""Provider 中心：预设 → 模型档案展开 纯逻辑自检（产品化③第一步）。
运行：python tests/test_providers.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile  # noqa: E402

from agentcore.config import (  # noqa: E402
    PROVIDER_PRESETS, expand_provider_profiles, load_user_providers, save_user_providers,
)


def test_disabled_not_expanded():
    # 默认全禁用 → 空（没填 key/没启用就不生成档案）
    assert expand_provider_profiles(PROVIDER_PRESETS, {}) == {}
    assert expand_provider_profiles(PROVIDER_PRESETS, {"openai": {"enabled": False}}) == {}


def test_enable_all_models():
    out = expand_provider_profiles(PROVIDER_PRESETS, {"volcengine-ark": {"enabled": True}})
    assert len(out) == 5  # 火山方舟下 5 个模型全展开
    p = out["volcengine-ark/kimi-k2.6"]
    assert p["provider"] == "anthropic" and p["model"] == "kimi-k2.6"
    assert p["api_key_env"] == "ARK_API_KEY" and p["vision"] is True
    assert "ark.cn-beijing" in p["base_url"] and p["max_tokens"] == 16384
    assert out["volcengine-ark/deepseek-v4-pro"]["vision"] is False


def test_enable_subset():
    out = expand_provider_profiles(PROVIDER_PRESETS,
                                   {"volcengine-ark": {"enabled": True, "models": ["kimi-k2.6"]}})
    assert set(out) == {"volcengine-ark/kimi-k2.6"}


def test_anthropic_official_no_base_url():
    out = expand_provider_profiles(PROVIDER_PRESETS, {"anthropic": {"enabled": True}})
    assert "base_url" not in out["anthropic/claude-opus-4-8"]  # 官方默认不带 base_url


def test_base_url_override():
    out = expand_provider_profiles(PROVIDER_PRESETS, {"moonshot": {
        "enabled": True, "models": ["kimi-k2.6"], "base_url": "https://api.moonshot.ai/v1"}})
    assert out["moonshot/kimi-k2.6"]["base_url"] == "https://api.moonshot.ai/v1"


def test_custom_model_on_preset_provider():
    out = expand_provider_profiles(PROVIDER_PRESETS, {"openai": {
        "enabled": True, "models": ["gpt-4o", "o3"], "custom_models": ["o3"]}})
    assert "openai/o3" in out and out["openai/o3"]["model"] == "o3"


def test_custom_provider():
    out = expand_provider_profiles(PROVIDER_PRESETS, {"myllm": {
        "enabled": True, "provider": "openai", "api_key_env": "MY_KEY",
        "base_url": "https://my.api/v1", "models": ["m1"], "custom_models": ["m1"]}})
    assert out["myllm/m1"]["api_key_env"] == "MY_KEY"
    assert out["myllm/m1"]["provider"] == "openai" and out["myllm/m1"]["base_url"] == "https://my.api/v1"


def test_providers_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "providers.yaml"
        assert load_user_providers(p) == {}                 # 不存在 → {}
        save_user_providers({"openai": {"enabled": True, "models": ["gpt-4o"]}}, p)
        got = load_user_providers(p)
        assert got["openai"]["enabled"] is True and got["openai"]["models"] == ["gpt-4o"]


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
