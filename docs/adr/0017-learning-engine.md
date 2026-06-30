# ADR 0017 — Learning Engine（块G）

状态：已实现（2026-06-30，待 Windows 验观察侧接线）
关联：[0014 评估/策略架构](0014-evaluation-policy-architecture.md)、[0015 错误分类](0015-error-taxonomy.md)、[0016 World State + Failure Memory](0016-world-state-failure-memory.md)；块F Golden 门是其上线前置。

## 背景

块 A–F 把执行内核铺成了稳定契约：事实（Evaluation）/差距（Need）/做法（Decision）分离，
错误归到 9 类稳定 taxonomy，失败被 Failure Memory 跨会话记住，Golden 门冻结了各决策点的行为基线。
路线图的收官块 G 要回答：**积累的失败证据，怎么变成对 `Need→Decision` 的改进，而又不失控？**

## 决策

**只做离线分析 + 候选建议 + 治理，不自动改运行时。** 三段：

1. **聚合 `aggregate(FailureMemory)`** —— 把失败行按错误分类归并成 `Aggregate`：
   总次数、涉及多少条不同的路（指纹）、失败时所记的 Decision、样例 detail。
   瞬时 IO 本就不进 Failure Memory（块E 已过滤），故聚合天然不含可重试噪声。

2. **候选 `propose(aggregates)`** —— 只对**系统性**失败升级为候选策略：
   同一分类**跨 ≥min_paths 条不同的路**累计 **≥min_count 次**。单条路偶发（块D 重试/块E 死路提示已管）
   不升级。每条候选 = 人话建议（来自 `_SUGGESTION` 分类骨架）+ 理由 + **语料证据**（命中指纹、次数、样例）。
   `transient_io` 双保险永不成策略。

3. **治理 `StrategyStore`** —— 候选/在用/退役策略持久存储（JSON，可读可审计）。生命周期：
   `proposed → (人审 approve + Golden 通过) → active → retire / rollback`。
   `approve()` **强制 `golden_passed=True`**，否则抛错——“没过语料门不准上”写进代码，不靠自觉。
   每次状态变迁留 `history` 审计；`rollback` 把 active 打回 proposed（撤销采纳，保留证据）。

## 为什么不自动应用策略（ADR 0014 不变量③：物化你要学习的，别建你不需要的引擎）

- 决策层仍是**确定性硬规则 + 模型**。G 不插一个会自动改路的"引擎"，只产**带证据的建议**。
  采纳与否、何时生效，由人 + Golden 门把关。这把"学习"的风险面压到最小：最坏情况是多产了几条没人采纳的建议。
- `active()` 暴露"已生效策略"只读视图，留作将来运行时消费的接口；**本块暂不接线** loop——
  先证明"能从历史轨迹跑出一条可解释、Golden 验证后生效的策略"，接线是后续增量。
- 这样块G 零运行时控制流改动 → 对存量行为零回归风险（同块A/F 的纪律）。

## 影响

- 新增 `src/agentcore/agent/learning/`（`aggregate` / `propose` / `StrategyStore`）+ `tests/test_learning.py`（14 测）。
- `FailureMemory.rows()` 导出全部失败行供聚合（只读，不改写入路径）。
- Golden 门加 `learn` 类 3 条语料：系统性升级 / 单路不升级 / 瞬时永不升级——把候选生成边界纳入回归门。
- 无新依赖（json + 标准库）。策略存储 `data/strategies.json`（与 `data/failures.db` 同目录，已 gitignore）。

## 验收

历史轨迹（如 `external_blocked` 在 3 条路上反复）→ `propose` 出一条可解释候选（建议+理由+证据）→
落库 → 人审 `approve(golden_passed=True)` → `active`。`test_end_to_end_one_explainable_strategy` 守此闭环；
`approve(golden_passed=False)` 被拒、`retire`/`rollback` 留审计均有测试。
