"""块H1：ResearchEvaluator 自检（搜索/调研结果质量事实，尤其预算约束满足）。

独立 runner：`python tests/test_research_evaluator.py`。纯逻辑、无 IO、无模型。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.evaluators import evaluate                              # noqa: E402
from agentcore.agent.evaluators.research import (                            # noqa: E402
    ResearchEvaluator, parse_budget_ceiling, split_items,
)

R = ResearchEvaluator()


def _ev(output, query=None):
    return R.evaluate("web_search", output, {"query": query} if query else None)


# ---- 预算上限解析 ----
def test_parse_budget_ceiling_variants():
    assert parse_budget_ceiling("618推荐的女士睡衣，500元以内") == 500
    assert parse_budget_ceiling("不超过300块的耳机") == 300
    assert parse_budget_ceiling("睡衣 ≤ 200 元") == 200
    assert parse_budget_ceiling("低于1000的手机") == 1000
    assert parse_budget_ceiling("预算800元以下") == 800
    # 多个上限取最严（最小）
    assert parse_budget_ceiling("500元以内，最好300以下") == 300
    # 无预算诉求
    assert parse_budget_ceiling("好看的女士睡衣推荐") is None
    assert parse_budget_ceiling("") is None


# ---- 结果分块 ----
def test_split_items_counts_results():
    out = ("[搜索结果·bing] 女士睡衣\n"
           "1. A 睡衣\n   http://a\n   ¥299 舒适\n"
           "2. B 睡衣\n   http://b\n   很好\n"
           "3. C 睡衣\n   http://c")
    items = split_items(out)
    assert len(items) == 3
    assert "A 睡衣" in items[0]


# ---- 调度：web_search 归 ResearchEvaluator（不是 SearchEvaluator）----
def test_dispatch_routes_web_search_to_research():
    out = "[搜索结果·bing] x\n1. t\n   http://u\n   ¥99 元好物"
    ev = evaluate("web_search", out, {"query": "x 100元以内"})
    # ResearchEvaluator 产 budget_ceiling 这个 metric，SearchEvaluator 不会
    assert ev is not None and "budget_ceiling" in ev.metrics


# ---- ★小红书验收用例：返回了但无一在预算内 → blocker issue ----
def test_xiaohongshu_budget_miss_flags_issue():
    # 模拟"小红书搜 618 女士睡衣 500 元以内"返回一堆都超预算的结果
    out = ("[搜索结果·bing] 小红书 618 女士睡衣 推荐\n"
           "1. 真丝睡衣套装 高端\n   http://a\n   ¥899 618大促\n"
           "2. 设计师款睡裙\n   http://b\n   1280元 限量\n"
           "3. 进口长袖睡衣\n   http://c\n   ￥699")
    ev = _ev(out, "在小红书搜索618推荐的女士睡衣，500元以内")
    assert ev.metrics["hits"] == 3
    assert ev.metrics["budget_ceiling"] == 500
    assert ev.metrics["priced"] == 3
    assert ev.metrics["within_budget"] == 0
    assert ev.issues, "无一在预算内应触发 blocker issue"
    assert "无一在预算内" in ev.issues[0]


def test_budget_satisfied_no_issue():
    # 有在预算内的结果 → 不该误报
    out = ("[搜索结果·bing] 女士睡衣 500元以内\n"
           "1. 纯棉睡衣\n   http://a\n   ¥199\n"
           "2. 真丝款\n   http://b\n   899元\n"
           "3. 冰丝睡裙\n   http://c\n   ¥359")
    ev = _ev(out, "女士睡衣 500元以内")
    assert ev.metrics["within_budget"] == 2     # 199、359 在内
    assert ev.issues == []


def test_priced_zero_is_signal_not_issue():
    # 有预算诉求但结果都没标价 → 只给 signal，不武断判 blocker（喂事实纪律）
    out = ("[搜索结果·bing] 睡衣\n"
           "1. 某款睡衣\n   http://a\n   点进店铺看\n"
           "2. 另一款\n   http://b\n   详情页有价")
    ev = _ev(out, "女士睡衣 500元以内")
    assert ev.issues == []
    assert any("无标价" in s for s in ev.signals)


def test_no_budget_constraint_only_counts_hits():
    out = ("[搜索结果·bing] 睡衣\n1. a\n   http://a\n2. b\n   http://b")
    ev = _ev(out, "好看的女士睡衣推荐")
    assert ev.metrics["hits"] == 2
    assert "budget_ceiling" not in ev.metrics
    assert ev.issues == []


def test_empty_result_no_issue():
    ev = _ev("搜索失败：bing: 无结果", "睡衣 500元以内")
    assert ev.metrics["hits"] == 0
    assert ev.issues == []
    assert any("0 条" in s for s in ev.signals)


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    fns.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
