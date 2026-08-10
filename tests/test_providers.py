"""Provider 中心：预设 → 模型档案展开 纯逻辑自检（产品化③第一步）。
运行：python tests/test_providers.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile  # noqa: E402

from agentcore.config import (  # noqa: E402
    DEFAULT_PROVIDERS, NO_MODEL_HINT, PROVIDER_PRESETS, AppConfig,
    expand_provider_profiles, load_user_providers, model_list_urls, save_user_providers,
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


def test_no_provider_enabled_out_of_the_box():
    """开箱不预设服务商：预置一家等于替用户做主——他没这家 key 时下拉挂着个用不了的模型，
    首轮报的是认证错而不是"你还没配模型"。"""
    assert DEFAULT_PROVIDERS == {}
    assert expand_provider_profiles(PROVIDER_PRESETS, DEFAULT_PROVIDERS) == {}


def test_no_model_configured_says_where_to_go():
    """没配模型时给的是人话指路，不是 KeyError('未找到模型档案 \'\'')。"""
    cfg = AppConfig(active_model="", models={})
    try:
        cfg.get_model()
        raise AssertionError("该抛异常")
    except KeyError as e:
        assert "Provider" in str(e) and "API Key" in str(e)
        assert str(e).strip("'\"") == NO_MODEL_HINT
    # 有模型但没选中：另一句话（指顶部下拉，不是让他去配 key）
    cfg2 = AppConfig(active_model="", models={"a": {"provider": "openai", "model": "x", "api_key_env": "K"}})
    try:
        cfg2.get_model()
        raise AssertionError("该抛异常")
    except KeyError as e:
        assert "还没有选中模型" in str(e)


def test_deepseek_uses_official_anthropic_endpoint():
    """DeepSeek 走 Anthropic 兼容协议：base_url 是 /anthropic（SDK 会在其后接 /v1/messages）。"""
    ds = PROVIDER_PRESETS["deepseek"]
    assert ds["provider"] == "anthropic"
    assert ds["base_url"] == "https://api.deepseek.com/anthropic"
    out = expand_provider_profiles(PROVIDER_PRESETS, {"deepseek": {"enabled": True}})
    assert out["deepseek/deepseek-chat"]["base_url"] == "https://api.deepseek.com/anthropic"


def test_model_list_falls_back_to_same_host_openai_endpoint():
    """Anthropic 兼容端点常常只实现 /v1/messages——DeepSeek 就列不出模型，但同一家的
    OpenAI 兼容端点可以。列模型只是配置面板的辅助功能，多试一个同源地址，别让用户以为 key 填错。"""
    assert model_list_urls("anthropic", "https://api.deepseek.com/anthropic") == [
        "https://api.deepseek.com/anthropic/v1/models",     # 先按协议本来的地址试
        "https://api.deepseek.com/v1/models",               # 再退到同一家的 OpenAI 兼容端点
    ]
    # 不带 /anthropic 后缀的兼容端点（如火山方舟 coding）没有可推导的同源地址 → 只试一个
    assert model_list_urls("anthropic", "https://ark.cn-beijing.volces.com/api/coding") == [
        "https://ark.cn-beijing.volces.com/api/coding/v1/models"]
    assert model_list_urls("anthropic", "") == ["https://api.anthropic.com/v1/models"]   # 官方默认
    assert model_list_urls("anthropic", "https://x.com/v1") == ["https://x.com/v1/models"]  # 已带 /v1 不再叠
    assert model_list_urls("openai", "https://api.deepseek.com/v1") == ["https://api.deepseek.com/v1/models"]
    assert model_list_urls("openai", "") == []              # OpenAI 协议没 base_url 就没得试


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
