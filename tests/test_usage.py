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
from agentcore.store.pricing import (  # noqa: E402
    Price, cost_of, is_stale, match_price, resolve_price, summarize_costs,
)
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


# ---- P2 价目与成本 ----------------------------------------------------------

_USD = Price(currency="USD", input=10.0, output=30.0, cache_read=1.0, cache_write=12.5)


def test_cost_splits_cache_three_ways():
    """缓存三态分别按各自单价算——合并成一个系数就是现有实现出错的地方。"""
    c = cost_of({"input_uncached": 1_000_000, "input_cache_write": 1_000_000,
                 "input_cache_read": 1_000_000, "output": 1_000_000}, _USD)
    assert c["currency"] == "USD"
    assert round(c["amount"], 6) == round(10.0 + 12.5 + 1.0 + 30.0, 6)
    assert c["inferred"] is False


def test_cost_flags_inferred_cache_price():
    """价目没单列缓存价时回落输入价，但**必须标记**——别让推断值冒充精确值。"""
    p = Price(currency="USD", input=10.0, output=30.0)      # 没填缓存价
    c = cost_of({"input_cache_read": 1_000_000}, p)
    assert c["amount"] == 10.0 and c["inferred"] is True
    # 没用到缓存就不该标 inferred（否则满屏都是警告，等于没有警告）
    assert cost_of({"input_uncached": 1_000_000}, p)["inferred"] is False


def test_no_price_means_no_amount():
    """没有可信价格就不给金额（决策 3）。"""
    assert cost_of({"output": 999}, None) is None


def test_prefix_match_not_substring():
    """按 model_id 前缀匹配：`opus` 不能再命中任意含该词的名字（决策 4）。"""
    table = {"gpt-4o": Price(currency="CNY", input=2.5, output=10.0, source="user"),
             "gpt-4o-mini": Price(currency="CNY", input=0.15, output=0.6, source="user"),
             "claude-opus": Price(currency="CNY", input=15.0, output=75.0, source="user")}
    assert match_price("claude-opus-5-20260101", table) is not None
    assert match_price("my-opus-tuned", table) is None      # 旧的子串写法会误伤
    # 最长前缀优先：gpt-4o-mini 必须命中自己那条，而不是更短的 gpt-4o
    assert match_price("gpt-4o-mini", table).input == 0.15
    assert match_price("gpt-4o-2026", table).input == 2.5


def test_price_comes_only_from_user():
    """**不随包带内置牌价**：没填过就是没有价格，只显 token。

    曾平移过一份公开牌价当兜底，但它全是美元、又未核实，而用户按人民币结算——
    那等于安静地给出一个币种和数值都不对的金额。
    """
    assert resolve_price("gpt-4o") is None
    assert resolve_price("claude-opus-5") is None
    mine = {"deepseek-v4-flash": Price(currency="CNY", input=1.0, output=2.0, source="user")}
    p = resolve_price("deepseek-v4-flash", mine)
    assert p.currency == "CNY" and p.source == "user"
    assert resolve_price("gpt-4o", mine) is None, "别人的价目不该外溢到没填过的模型"


def test_currencies_never_summed_together():
    """多币种分开汇总，绝不相加（决策 4）。"""
    table = {"a-": Price(currency="USD", input=1.0, output=1.0),
             "b-": Price(currency="CNY", input=7.0, output=7.0)}
    rows = [{"model_id": "a-1", "input_uncached": 1_000_000, "output": 0},
            {"model_id": "b-1", "input_uncached": 1_000_000, "output": 0},
            {"model_id": "zzz", "input_uncached": 1_000_000, "output": 0}]
    s = summarize_costs(rows, resolve=lambda mid: match_price(mid, table))
    assert set(s["by_currency"]) == {"USD", "CNY"}
    assert s["by_currency"]["USD"]["amount"] == 1.0
    assert s["by_currency"]["CNY"]["amount"] == 7.0
    assert s["unpriced_rows"] == 1, "没价格的那条只能算 token，不能混进金额"


def test_stale_price_detection():
    """没有 as_of 一律当过期——不知道是哪天的价格，就不能当它新鲜。"""
    assert is_stale(Price(currency="USD", input=1, output=1), "2026-08-14") is True
    assert is_stale(Price(currency="USD", input=1, output=1, as_of="2026-08-01"),
                    "2026-08-14") is False
    assert is_stale(Price(currency="USD", input=1, output=1, as_of="2025-01-01"),
                    "2026-08-14") is True
    assert is_stale(Price(currency="USD", input=1, output=1, as_of="不是日期"),
                    "2026-08-14") is True


def test_store_price_roundtrip_and_cost():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.set_price("deepseek-v4-flash",
                    Price(currency="CNY", input=2.0, output=8.0, cache_read=0.2,
                          as_of="2026-08-14"))
        got = s.user_prices()["deepseek-v4-flash"]
        assert got.currency == "CNY" and got.cache_read == 0.2
        assert got.source == "user" and got.verified is True, "人亲手填的就是权威来源"

        s.record(model_id="deepseek-v4-flash", input_uncached=1_000_000,
                 input_cache_read=1_000_000, output=1_000_000)
        out = s.totals_with_cost()
        assert round(out["by_currency"]["CNY"]["amount"], 6) == round(2.0 + 0.2 + 8.0, 6)
        assert out["unpriced_rows"] == 0
        s.close()


def test_store_unpriced_model_still_reports_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="某个没人填过价的模型", output=1234)
        out = s.totals_with_cost()
        assert out["unpriced_rows"] == 1 and not out["by_currency"]
        assert out["buckets"][0]["output"] == 1234, "没金额也要有 token"
        s.close()


def test_price_update_overwrites_not_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.set_price("m", Price(currency="USD", input=1.0, output=1.0))
        s.set_price("m", Price(currency="USD", input=2.0, output=2.0))
        prices = s.user_prices()
        assert len(prices) == 1 and prices["m"].input == 2.0
        s.delete_price("m")
        assert s.user_prices() == {}
        s.close()


# ---- P3 后端 API（面板的数据源）---------------------------------------------

class _FakeConv:
    def __init__(self, store): self._s = store
    def _get_usage_store(self): return self._s


def _api_with(store):
    """造一个只带 active 会话的 Api 壳子——不跑 __init__（不起对话/存储/模型）。"""
    from agentcore.bridge import api as apimod
    a = object.__new__(apimod.Api)
    a.active = _FakeConv(store)
    return a


def test_api_usage_summary_shapes():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.record(model_id="m1", input_uncached=100, input_cache_read=900, output=50,
                 agent_role="main")
        s.record(model_id="m2", output=20, agent_role="delegate:sub-1", measured=False)
        out = _api_with(s).usage_summary(days=30)
        assert out["ok"] is True
        assert out["total"]["rows"] == 2
        assert {r["bucket"] for r in out["by_model"]} == {"m1", "m2"}
        assert {r["bucket"] for r in out["by_role"]} == {"main", "delegate:sub-1"}
        assert out["by_day"], "按天切分要有数据"
        assert out["total"]["estimated_rows"] == 1
        assert out["unpriced_rows"] == 2, "没填价格的模型应被计出来，面板据此提示"
        s.close()


def test_api_usage_summary_degrades_without_store():
    """台账关掉时给明确错误，不是崩、也不是假装有数据。"""
    a = object.__new__(__import__("agentcore.bridge.api", fromlist=["x"]).Api)
    a.active = _FakeConv(None)
    assert a.usage_summary()["ok"] is False


def test_api_price_crud_and_validation():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        api = _api_with(s)
        assert api.set_model_price("m1", {"currency": "cny", "input": "2", "output": "8",
                                          "cache_read": "0.2", "as_of": "2026-08-14"})["ok"]
        row = api.get_model_prices()["user"][0]
        assert row["currency"] == "CNY", "币种归一成大写"
        assert row["source"] == "user" and row["verified"] is True
        assert row["cache_write"] is None, "没填的就是没填，不许替用户编一个"

        # 填错要拦住并说清楚，别把坏数据写进账里
        assert api.set_model_price("", {"input": 1, "output": 1})["ok"] is False
        assert api.set_model_price("m2", {"input": "", "output": 1})["ok"] is False
        assert api.set_model_price("m2", {"input": -1, "output": 1})["ok"] is False
        assert api.set_model_price("m2", {"input": "abc", "output": 1})["ok"] is False

        # 有价之后金额才出得来
        s.record(model_id="m1", input_uncached=1_000_000, output=1_000_000)
        assert round(_api_with(s).usage_summary()["by_currency"]["CNY"]["amount"], 6) == 10.0

        assert api.delete_model_price("m1")["ok"]
        assert api.get_model_prices()["user"] == []
        s.close()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ok  {name}")
    print(f"test_usage: {n}/{n} 通过")
