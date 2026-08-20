"""Evaluator 协议 + 调度 + Score 投影（docs/adr/0014 事实层）。

设计：每个 Evaluator 实现 `applies()`（这条工具结果归不归我管）与 `evaluate()`
（解析成事实）。`evaluate()` 调度器按注册顺序选第一个 applies 的——内容特征强的
（如测试输出）排在工具名匹配的（如 shell）前面，让"用 shell 跑 pytest"也归 Coding。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..contract import Evaluation


@runtime_checkable
class Evaluator(Protocol):
    """一个 Skill 的事实评估器。纯逻辑、无 IO——只解析已经拿到的工具输出。"""

    def applies(self, tool_name: str, output: str) -> bool:
        """这条工具结果该不该由我评估。"""
        ...

    def evaluate(self, tool_name: str, output: str, tool_input: "dict | None" = None) -> Evaluation:
        """解析成结构化事实。仅在 applies() 为真时调用。"""
        ...


# 观察类工具：输出描述的是**世界的状态**，不是"我做了一件事"的结果。
# 读到一个含 assert 的测试文件、grep 命中一行 AssertionError——只说明世界里有这么一段文本，
# **不构成一次失败**。CodingEvaluator 的 applies() 只看输出特征词、不看工具，于是把它们
# 一并接管并判成 blocker「测试未全过」，后果三重（块 V4 收割语料时照出来的）：
#   ①失败语料被污染：13 次 logic 里 7 次来自 read_file；
#   ②`deadend_hint` 在**纯只读任务**里误报（l3_parallel_audit 基线实测触发 1 次）；
#   ③候选策略「先定位别盲改」的证据被稀释。
# 判据见 ADR 0027 决策 11：写进失败语料的必须**是一次动作**且**它失败了**。
# 注：Search/Shell/Research 三个评估器本来就按**工具名**接管，不受影响；
# 这里只挡 Coding 这一个"按文本特征词认领"的。
OBSERVATION_TOOLS = frozenset({
    "read_file", "list_dir", "code_outline", "find_symbol", "search_code",
    "grep_search", "glob_search",                 # 检索：命中的是别人的文本
    "git_status", "git_diff", "git_log",          # 读仓库状态
    "web_fetch",                                  # 读一个网页
    "take_screenshot",                            # 看一眼屏幕
})


# 注册顺序 = 优先级。Coding 在前：shell 跑出来的测试输出应归 Coding 而非 Shell。
# Research 早于 Search：联网搜索（web_search）走质量评估（判约束满足），代码检索仍归 Search。
def _registry() -> "list[Evaluator]":
    from .coding import CodingEvaluator
    from .research import ResearchEvaluator
    from .search import SearchEvaluator
    from .shell import ShellEvaluator
    return [CodingEvaluator(), ResearchEvaluator(), SearchEvaluator(), ShellEvaluator()]


_EVALUATORS: "list[Evaluator] | None" = None


def evaluate(tool_name: str, output: str, tool_input: "dict | None" = None) -> "Evaluation | None":
    """调度到第一个适配的 Evaluator；没有则返回 None（多数工具无需评估）。"""
    global _EVALUATORS
    if _EVALUATORS is None:
        _EVALUATORS = _registry()
    text = output or ""
    for ev in _EVALUATORS:
        if ev.applies(tool_name or "", text):
            return ev.evaluate(tool_name or "", text, tool_input)
    return None


def score(evaluation: Evaluation) -> float:
    """把事实投影成一个 0–1 展示分——**仅 UI 排序/展示用，绝不回喂决策**。

    投影规则刻意简单、单向：有 blocker 级 issue → 低分；signals 越多（噪声/异常越多）
    略降；confidence 直接加权。这只是给人看的一个粗略"好坏感"，不参与任何分支。
    """
    base = 1.0
    if evaluation.issues:
        base = 0.2                          # 有默认策略判定的问题 → 直接判低
    base -= min(0.3, 0.05 * len(evaluation.signals))   # 异常信号多 → 略降，封顶 -0.3
    base = max(0.0, min(1.0, base))
    return round(base * evaluation.confidence, 3)
