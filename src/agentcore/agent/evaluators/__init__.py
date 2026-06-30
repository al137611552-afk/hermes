"""事实层：把工具原始输出解析成结构化 `Evaluation`（见 docs/adr/0014、ROADMAP 块B）。

每个 Skill 一个 Evaluator，只产出**事实**（metrics/signals/issues/confidence），
不产出 Score、不产出 Need、不做决策。上层 Policy 读这些事实判 Need。

对外只暴露两个入口：
- `evaluate(tool_name, output, tool_input=None) -> Evaluation | None`：调度到合适的
  Evaluator；没有适配的 Evaluator 时返回 None（多数工具不需要评估）。
- `score(evaluation) -> float`：把事实投影成一个 0–1 的展示分（**仅 UI 排序/展示用，
  绝不回喂决策**，ADR 决策第 1 条）。
"""
from .base import Evaluator, evaluate, score

__all__ = ["Evaluator", "evaluate", "score"]
