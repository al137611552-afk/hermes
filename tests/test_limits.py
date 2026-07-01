"""统一「限额与预算」持久化 + 校验 + 合并自测（GUI 设置面板后端）。

运行：python tests/test_limits.py
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import (  # noqa: E402
    LIMITS_SPEC, _LIMITS_BY_KEY, load_config, merge_limits, read_limits, set_limits,
)


def test_set_and_read_roundtrip(tmp: Path):
    p = tmp / "limits.json"
    out = set_limits({"agent.max_steps": 80, "web.timeout": 40}, path=p)
    assert out == {"agent.max_steps": 80, "web.timeout": 40}
    assert read_limits(p) == {"agent.max_steps": 80, "web.timeout": 40}


def test_set_merges_not_overwrites(tmp: Path):
    p = tmp / "limits.json"
    set_limits({"agent.max_steps": 80}, path=p)
    set_limits({"web.timeout": 40}, path=p)
    assert read_limits(p) == {"agent.max_steps": 80, "web.timeout": 40}


def test_rejects_unknown_keys(tmp: Path):
    p = tmp / "limits.json"
    set_limits({"agent.max_steps": 80, "bogus.key": 5, "agent.system_prompt": "x"}, path=p)
    d = read_limits(p)
    assert "bogus.key" not in d and "agent.system_prompt" not in d


def test_coerce_and_clamp_to_range(tmp: Path):
    p = tmp / "limits.json"
    # 字符串数字被转 int；越界被夹到 min/max
    out = set_limits({"agent.model_max_tokens": "30000",
                      "agent.design_review_verdict_max_tokens": 999999,  # > max 32000
                      "agent.max_steps": 0}, path=p)                       # < min 1
    assert out["agent.model_max_tokens"] == 30000
    assert out["agent.design_review_verdict_max_tokens"] == 32000
    assert out["agent.max_steps"] == 1


def test_non_numeric_dropped(tmp: Path):
    p = tmp / "limits.json"
    out = set_limits({"agent.max_steps": "abc", "web.timeout": 30}, path=p)
    assert "agent.max_steps" not in out and out["web.timeout"] == 30


def test_merge_overrides_correct_section(tmp: Path):
    p = tmp / "limits.json"
    set_limits({"agent.model_max_tokens": 30000, "web.timeout": 45,
                "mcp.call_timeout": 120}, path=p)
    data = {"agent": {"model_max_tokens": 0}, "web": {"timeout": 20}, "mcp": {"call_timeout": 60}}
    m = merge_limits(data, path=p)
    assert m["agent"]["model_max_tokens"] == 30000
    assert m["web"]["timeout"] == 45
    assert m["mcp"]["call_timeout"] == 120


def test_merge_empty_is_noop(tmp: Path):
    p = tmp / "limits.json"          # 不存在
    data = {"agent": {"max_steps": 25}}
    assert merge_limits(data, path=p) == data


def test_spec_keys_map_to_real_config_fields():
    """每个 spec key 的 section.field 都要在真实 config 上存在，否则 UI 写了合并不进去。"""
    c = load_config()
    for s in LIMITS_SPEC:
        section, _, field = s["key"].partition(".")
        sec = getattr(c, section, None)
        assert sec is not None, f"缺 config 段：{section}（{s['key']}）"
        assert hasattr(sec, field), f"config.{section} 无字段 {field}（{s['key']}）"
        assert s["type"] in ("int", "float")


def test_spec_index_covers_all():
    assert set(_LIMITS_BY_KEY) == {s["key"] for s in LIMITS_SPEC}
    assert len(_LIMITS_BY_KEY) == len(LIMITS_SPEC)


def test_persisted_is_valid_json(tmp: Path):
    p = tmp / "limits.json"
    set_limits({"agent.max_steps": 40}, path=p)
    json.loads(p.read_text(encoding="utf-8"))


def _run_all():
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
