"""SearchEvaluator：把检索类工具输出解析成事实（命中数、空结果信号）。

吃得下的真实格式：
- grep_search：命中行用换行拼接，空 → `无命中。`
- glob_search：匹配文件用换行拼接，空 → `无匹配文件。`
- search_code：定义/片段列表，空 → 文案含"未找到/没有/无"

注意：**空结果是事实，不是 blocker**——"该不该急"是上层 Policy 的判断，故这里
issues 留空（ADR 决策第 1 条：Evaluator 只给事实，严重度归 Policy）。
"""
from __future__ import annotations

from ..contract import Evaluation

_SEARCH_TOOLS = frozenset({"grep_search", "glob_search", "search_code",
                           "code_outline", "web_search"})
# 各工具的"空结果"文案标志
_EMPTY_MARKERS = ("无命中", "无匹配文件", "未找到", "没有找到", "没有匹配", "无结果")


class SearchEvaluator:
    def applies(self, tool_name: str, output: str) -> bool:
        return tool_name in _SEARCH_TOOLS

    def evaluate(self, tool_name: str, output: str, tool_input=None) -> Evaluation:
        text = (output or "").strip()
        empty = (not text) or any(m in text for m in _EMPTY_MARKERS)

        if empty:
            hits = 0
        else:
            # 命中数 = 非空行数（启发式：每条结果一行或一段，足够给 Learning 当趋势）
            hits = sum(1 for ln in text.splitlines() if ln.strip())

        metrics = {"hits": float(hits)}
        signals = ["返回 0 条" if empty else f"命中 {hits} 条"]
        # 空结果不判 blocker——它是事实，要不要换检索策略由 Policy 定
        return Evaluation(metrics=metrics, signals=signals, issues=[], confidence=1.0)
