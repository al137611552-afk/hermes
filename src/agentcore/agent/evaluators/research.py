"""ResearchEvaluator：把**搜索/调研结果**解析成「质量事实」（块H1，见 ADR 0018）。

与 SearchEvaluator 的分工：
- SearchEvaluator 管**代码检索**（grep/glob/search_code）——只数命中、判空。
- ResearchEvaluator 管**联网搜索/调研**（web_search …）——除命中数外，还判**结果到底对不对题**：
  用户给的**可校验约束**有没有满足。当前抓最硬的一类：**预算上限**
  （query 说"500元以内"，结果标价却无一在内 → 这是"返回了但不达标"的质量差距）。

纪律（ADR 0014 决策①「事实/严重度分离」+ 块E「喂事实而非硬拦截，防误报」）：
- blocker 级 `issues` **只在可证伪时**触发（预算是数值铁证：有上限、有命中、却 0 条在内）。
- 模糊的相关性（"是不是好推荐"）**不在这里判**——留给 H3 模型裁判，避免正则误报把功能搞缺失。
- 品类关键词覆盖只当 `signals`（线索），不当 blocker。

吃得下的格式：`web_search` 输出 = `[搜索结果·引擎] query` + 若干 `N. 标题\n   url\n   摘要`。
"""
from __future__ import annotations

import re

from ..contract import Evaluation

# 归 ResearchEvaluator 的"联网搜索/调研"类工具（注册早于 SearchEvaluator，故 web_search 由本器接管）
_RESEARCH_TOOLS = frozenset({"web_search"})

# 价格：数字（可带千分位/小数）紧邻 元/块/¥/rmb。¥ 在数字前也认。
_PRICE_RE = re.compile(r"(?:¥|￥)\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:元|块|rmb)", re.I)
# 预算上限：上限词（以内/以下/不超过/低于/≤/<）配合一个金额。两种语序都认。
_CEIL_RE = re.compile(
    r"(?:(\d+(?:[.,]\d+)?)\s*(?:元|块|¥|￥|rmb)?\s*(?:以内|以下|之内|不超过|不到|封顶))"
    r"|(?:(?:不超过|低于|少于|≤|<=|<)\s*(\d+(?:[.,]\d+)?)\s*(?:元|块|¥|￥|rmb)?)",
    re.I)
# 结果分块：行首 "N. "
_ITEM_RE = re.compile(r"^\s*\d+\.\s", re.M)


def _to_float(s: str) -> "float | None":
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_budget_ceiling(query: str) -> "float | None":
    """从 query 抽预算上限（取最严的一个 = 最小上限）。无则 None。"""
    if not query:
        return None
    vals = []
    for m in _CEIL_RE.finditer(query):
        v = _to_float(m.group(1) or m.group(2))
        if v is not None:
            vals.append(v)
    return min(vals) if vals else None


def _prices_in(text: str) -> "list[float]":
    out = []
    for m in _PRICE_RE.finditer(text or ""):
        v = _to_float(m.group(1) or m.group(2))
        if v is not None:
            out.append(v)
    return out


def split_items(text: str) -> "list[str]":
    """把搜索结果正文切成每条结果一段（按行首 'N. '）。切不出就整体当一段。"""
    body = text or ""
    # 去掉首行 "[搜索结果·引擎] query"
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("[搜索结果"):
        body = "\n".join(lines[1:])
    parts = _ITEM_RE.split(body)
    items = [p.strip() for p in parts if p.strip()]
    return items


_EMPTY_MARKERS = ("无结果", "未找到", "没有找到", "搜索失败")


class ResearchEvaluator:
    def applies(self, tool_name: str, output: str) -> bool:
        return tool_name in _RESEARCH_TOOLS

    def evaluate(self, tool_name: str, output: str, tool_input=None) -> Evaluation:
        text = (output or "").strip()
        empty = (not text) or any(m in text for m in _EMPTY_MARKERS)
        items = [] if empty else split_items(text)
        hits = len(items)

        query = ""
        if isinstance(tool_input, dict):
            query = str(tool_input.get("query") or tool_input.get("q") or "")

        metrics = {"hits": float(hits)}
        signals = []
        issues = []

        if empty or hits == 0:
            signals.append("返回 0 条")
            return Evaluation(metrics=metrics, signals=signals, issues=issues, confidence=1.0)
        signals.append(f"命中 {hits} 条")

        # —— 预算约束满足（可证伪的硬事实）——
        ceil = parse_budget_ceiling(query)
        if ceil is not None:
            metrics["budget_ceiling"] = ceil
            priced = 0          # 有标价的结果数
            within = 0          # 标价 ≤ 上限的结果数
            for it in items:
                ps = _prices_in(it)
                if ps:
                    priced += 1
                    if min(ps) <= ceil:    # 取该结果最低价判是否在预算内
                        within += 1
            metrics["priced"] = float(priced)
            metrics["within_budget"] = float(within)
            if priced > 0 and within == 0:
                # 有命中、有标价、却无一在预算内 → 铁证：搜索没满足预算约束
                issues.append(
                    f"预算上限 {ceil:g} 元：{hits} 条结果中 {priced} 条有标价，"
                    f"但**无一在预算内**——结果不达标，建议换关键词/换数据源重搜")
            elif priced == 0:
                # 有预算诉求但结果全无价格 → 线索（可能要进店看价），不武断判 blocker
                signals.append(f"指定了预算（≤{ceil:g}元）但结果均无标价，难判是否达标")

        return Evaluation(metrics=metrics, signals=signals, issues=issues, confidence=0.9)
