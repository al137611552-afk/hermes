"""块G：Learning Engine（见 docs/adr/0017-learning-engine.md）。

在稳定的 Need/taxonomy 之上，**离线**把 Failure Memory 的失败证据聚合成
"哪条路、以哪种分类、反复不通" → 产出**候选策略**（带语料证据）。

纪律（ADR 0014 不变量③ + 块F 语料门）：
- 不自动改运行时。决策层仍是确定性硬规则 + 模型；本模块只产**建议**。
- 候选策略 status=proposed，须**人审 + Golden 验证**后才 active，且可退役/回滚。
- 每条策略带"语料证据"（命中的指纹、次数、样例 detail），可解释。
"""
from .engine import (
    Aggregate,
    Candidate,
    Strategy,
    StrategyStore,
    aggregate,
    propose,
)

__all__ = [
    "Aggregate", "Candidate", "Strategy", "StrategyStore",
    "aggregate", "propose",
]
