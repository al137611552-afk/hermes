"""模型档案管理纯逻辑自检：merge_models + user_models 读写往返 + 坏文件回退。
运行：python tests/test_model_profiles.py"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import load_user_models, merge_models, save_user_models  # noqa: E402


def test_merge_user_overrides_and_adds():
    base = {"ark-kimi": {"model": "kimi"}, "gpt": {"model": "gpt-4o"}}
    user = {"gpt": {"model": "gpt-5"}, "my-claude": {"model": "claude"}}
    out = merge_models(base, user)
    assert out["gpt"]["model"] == "gpt-5"          # 用户覆盖同名内置
    assert out["ark-kimi"]["model"] == "kimi"      # 内置保留
    assert out["my-claude"]["model"] == "claude"   # 用户新增
    assert merge_models(None, None) == {}
    assert merge_models(base, None) == base         # 无用户档案 = 原样


def test_user_models_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "user_models.yaml"
        assert load_user_models(p) == {}            # 不存在 → {}
        save_user_models({"x": {"provider": "openai", "model": "m", "api_key_env": "K"}}, p)
        got = load_user_models(p)
        assert got["x"]["model"] == "m" and got["x"]["provider"] == "openai"


def test_load_user_models_bad_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.yaml"
        p.write_text("{ broken: [", encoding="utf-8")
        assert load_user_models(p) == {}            # 坏文件回退 {}、不崩


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
