"""用量台账自测（ADR 0025 P1）：采集口径 + 落库聚合。

纯逻辑 + 临时库，不联网、不碰真模型。运行：python tests/test_usage.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.providers.anthropic_p import _usage as anthropic_usage   # noqa: E402
from agentcore.providers.openai_p import _usage as openai_usage         # noqa: E402
from agentcore.store.usage import (  # noqa: E402
    UsageStore, parse_usage_event, provider_kind,
)


class _Obj:
    """按关键字造一个"像 SDK usage 对象"的东西（只有给了的属性才存在）。"""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---- 采集口径 ---------------------------------------------------------------

def test_anthropic_records_cache_write():
    """写缓存必须单独记——它比普通输入贵，漏了就是系统性少算。"""
    u = anthropic_usage(_Obj(input_tokens=100, output_tokens=20,
                             cache_read_input_tokens=500, cache_creation_input_tokens=300))
    assert u == {"input": 100, "output": 20, "cache_read": 500, "cache_write": 300}


def test_anthropic_none_safe_and_missing_fields_default_zero():
    assert anthropic_usage(None) is None
    u = anthropic_usage(_Obj(input_tokens=7, output_tokens=3))   # 老端点无缓存字段
    assert u["cache_read"] == 0 and u["cache_write"] == 0


def test_openai_official_subtracts_cached_from_prompt():
    """**口径陷阱**：OpenAI 的 prompt_tokens 含缓存部分，不减就把命中的 token 按全价重算一遍。"""
    u = openai_usage(_Obj(prompt_tokens=1000, completion_tokens=50,
                          prompt_tokens_details=_Obj(cached_tokens=800)))
    assert u["input"] == 200, "未命中输入应为 1000-800"
    assert u["cache_read"] == 800
    assert u["output"] == 50


def test_openai_deepseek_dialect_hit_miss():
    """DeepSeek 方言：命中/未命中直接给，这正是此前恒记 0 丢掉的那部分。"""
    u = openai_usage(_Obj(prompt_tokens=1000, completion_tokens=50,
                          prompt_cache_hit_tokens=640, prompt_cache_miss_tokens=360))
    assert (u["input"], u["cache_read"]) == (360, 640)


def test_openai_dialects_never_double_count():
    """两种方言下 未命中 + 命中 都应还原成 prompt_tokens，不多不少。"""
    for u in (
        openai_usage(_Obj(prompt_tokens=900, completion_tokens=1,
                          prompt_tokens_details=_Obj(cached_tokens=100))),
        openai_usage(_Obj(prompt_tokens=900, completion_tokens=1,
                          prompt_cache_hit_tokens=100, prompt_cache_miss_tokens=800)),
    ):
        assert u["input"] + u["cache_read"] == 900


def test_openai_plain_endpoint_without_cache_fields():
    """普通端点（无任何缓存字段）：整段算未命中，不能凭空造出命中数。"""
    u = openai_usage(_Obj(prompt_tokens=42, completion_tokens=8))
    assert (u["input"], u["cache_read"], u["cache_write"]) == (42, 0, 0)


def test_openai_none_safe():
    assert openai_usage(None) is None


def test_provider_kind():
    class AnthropicProvider: pass
    class OpenAIProvider: pass
    assert provider_kind(AnthropicProvider()) == "anthropic"
    assert provider_kind(OpenAIProvider()) == "openai"


# ---- 事件识别（P1 最容易写错的一处）-----------------------------------------

def test_parse_main_usage_event():
    role, payload = parse_usage_event("usage", {"input": 5, "output": 1})
    assert role == "main" and payload["input"] == 5


def test_parse_subagent_usage_is_not_missed():
    """子 Agent 的用量被包了一层，外层事件名是 subagent_event——漏了就把委派的钱算丢。"""
    ev = {"id": "sub-7", "event": "usage", "data": {"input": 9, "output": 2, "model": "m2"}}
    role, payload = parse_usage_event("subagent_event", ev)
    assert role == "delegate:sub-7"
    assert payload["model"] == "m2" and payload["input"] == 9


def test_parse_ignores_other_events():
    """别把无关事件当用量记——工具事件同样从这条咽喉过。"""
    for event, data in (
        ("chunk", "hello"),
        ("tool_use", {"name": "read_file"}),
        ("subagent_event", {"id": "s1", "event": "tool_use", "data": {}}),
        ("subagent_done", {"id": "s1", "ok": True}),
        ("usage", None),                     # 形状不对也不能崩
        ("subagent_event", {"id": "s1", "event": "usage", "data": "坏数据"}),
    ):
        assert parse_usage_event(event, data) is None


# ---- 落库与聚合 -------------------------------------------------------------

def _store(tmp: str) -> UsageStore:
    return UsageStore(Path(tmp) / "usage.db")


def test_record_and_total():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1", input_uncached=10, input_cache_read=5,
                 input_cache_write=2, output=7)
        s.record(model_id="m1", input_uncached=1, output=1)
        t = s.totals()[0]
        assert t["input_uncached"] == 11 and t["input_cache_read"] == 5
        assert t["input_cache_write"] == 2 and t["output"] == 8
        assert t["rows"] == 2
        s.close()


def test_zero_row_is_still_recorded():
    """全零也要落一行——"这轮没花 token"和"这轮没记录"是两回事。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1")
        assert s.totals()[0]["rows"] == 1
        s.close()


def test_estimated_rows_are_flagged():
    """估算行必须可识别——它们不能用于对账（ADR 0025 决策 3）。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1", output=100, measured=True)
        s.record(model_id="m1", output=100, measured=False)
        t = s.totals()[0]
        assert t["rows"] == 2 and t["estimated_rows"] == 1
        s.close()


def test_group_by_model_and_role():
    """归因：钱花在哪个模型、哪个子 Agent 上——这是优化 harness 时最想知道的。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1", output=10, agent_role="main")
        s.record(model_id="m2", output=20, agent_role="delegate:sub-1")
        s.record(model_id="m2", output=5, agent_role="delegate:sub-1")

        by_model = {r["bucket"]: r["output"] for r in s.totals(group_by="model_id")}
        assert by_model == {"m1": 10, "m2": 25}

        by_role = {r["bucket"]: r["output"] for r in s.totals(group_by="agent_role")}
        assert by_role == {"main": 10, "delegate:sub-1": 25}
        s.close()


def test_group_by_rejects_unknown_dimension():
    """分组维度是白名单——它直接拼进 SQL，不能收外部任意串。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        raised = None
        try:
            s.totals(group_by="model_id; DROP TABLE usage_log")
        except ValueError as e:
            raised = str(e)
        assert raised is not None and "不支持的分组维度" in raised
        assert s.totals()[0]["rows"] == 0     # 表还在
        s.close()


def test_time_window_filter():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1", output=1, ts=1000.0)
        s.record(model_id="m1", output=2, ts=2000.0)
        s.record(model_id="m1", output=4, ts=3000.0)
        assert s.totals(since=1500.0, until=2500.0)[0]["output"] == 2
        s.close()


def test_survives_reopen():
    """独立库的意义就在于长期留存——重开进程账还在。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1", output=9)
        s.close()
        s2 = _store(tmp)
        assert s2.totals()[0]["output"] == 9
        s2.close()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ok  {name}")
    print(f"test_usage: {n}/{n} 通过")
