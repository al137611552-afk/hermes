# Hermes 开发路线图 — 评估/策略内核

> 配套 ADR：[`docs/adr/0014-evaluation-policy-architecture.md`](adr/0014-evaluation-policy-architecture.md)
> 适用范围：**整个 Hermes 执行内核**（Coding / Search / Vision / Research 全部 Skill），不止 crazy 模式。
> 节奏纪律（见 CLAUDE.md）：每块 = 实现 → 全回归全绿 → 用户 Windows 验 → 通过后定版 → 下一块。

## 一图看懂目标

最终要让每一次"行动"都走同一条闭环：

```text
Goal ─► Plan ─► Act ─► Observe ─► Evaluate ─► Update World State ─► Re-plan ─┐
  ▲                                                                          │
  └──────────────────────────────────────────────────────────────────────┘

每步落到契约：
Tool ─► Evaluation(事实) ─► Policy ─► Need(差距) ─► Planner ─► [Decision + 工具] ─► Tool
```

三个不变量贯穿所有块：
1. **事实/差距/做法分离**：Evaluation 只出事实；Need 只描述差距；Decision 才是做法。
2. **Need 小而稳**（~9 个枚举，多年不变），是 Learning 聚合的 key。
3. **物化你要学习的，别建你不需要的引擎**：Need 物化；Decision 多数只记标签。

---

## 块 A — 契约骨架（行为等价重构）✅ 已完成（2026-06-30）

**目标**：把"判断"抽成稳定契约，**不新增任何能力**，证明契约能承载现状。

- A1 ✅ 定义 `Need` 枚举（9 个）+ `Evaluation` dataclass（metrics/signals/issues/confidence）。`src/agentcore/agent/contract.py`（纯逻辑，单测覆盖）。
- A2 ✅ 把 crazy verdict（`[[DONE]]/[[CONTINUE]]/[[NEED_USER]]/[[PHASE_DONE]]`）经 `verdict_to_need()` 映射到 Need 并随轮上报（`crazy_need` 事件），**分支仍按 verdict 走，行为不变**。
- A3 ✅ 重构 `loop.py` 三个 nudge（login_wall/browse/stuck_edit）为"探测事实 → 归 Need → `_nudge_injection(need)` 选注入"，注入文案逐字不变、公开签名不变。
- A4 ✅ 全量回归绿：Python 45 文件（含 test_conversation 83、test_stuck 13、新增 test_contract 9）+ 前端 node:test 23。

**交付物**：`contract.py` + `tests/test_contract.py`；`conversation.py`/`loop.py` 走新契约但行为零变化。
**验收**：✅ 回归全绿；crazy 块2/3/4 行为与块A前逐字节一致（纯后端逻辑，本地自检即等价证明，无需 Windows）。

## 块 B — Evaluator 标准化（事实层）

**目标**：让每个 Skill 产出结构化 Evaluation，而非散落的字符串/退出码。

- B1 `Evaluator` 协议：输入工具结果，输出 `Evaluation{metrics,signals,issues,confidence}`。
- B2 先实现 3 个：`CodingEvaluator`（测试通过数/总、报错码）、`SearchEvaluator`（命中数、空结果信号）、`ShellEvaluator`（退出码、stderr 模式）。
- B3 Score 作为**事实投影**仅供 UI 展示，标注"不回喂决策"。

**交付物**：`agent/evaluators/`；每个 Evaluator 配单测（mock 工具结果）。
**验收**：回归全绿；UI 能显示三类任务的结构化事实。

## 块 C — Error Taxonomy（差距的可聚合分类）

**目标**：把 `signals/issues` 归并到稳定的错误分类，作为 Failure-Memory / Learning 的 key。

- C1 错误分类草案（与 Need 正交，更细）：`TRANSIENT_IO`（网络/端口/超时）、`AUTH`（鉴权/授权）、`NOT_FOUND`（路径/资源/0 结果）、`SYNTAX`（编译/解析）、`LOGIC`（断言/测试失败）、`RESOURCE`（OOM/磁盘/限额）、`AMBIGUOUS`（指令不清）、`EXTERNAL_BLOCKED`（第三方硬阻塞）、`UNKNOWN`。
- C2 `signal → taxonomy` 映射器（先规则 + 兜底 UNKNOWN），纯逻辑可测。
- C3 落 `docs/adr/0015-error-taxonomy.md` 固化分类语义。

**交付物**：`agent/taxonomy.py` + 单测 + ADR 0015。
**验收**：三类 Evaluator 的典型失败都能被分类（含 UNKNOWN 兜底）。

## 块 D — Auto-Retry（最便宜的 Need→Decision 硬规则）

**目标**：第一条确定性 `Need→Decision` 规则落地，验证决策层不必是大引擎。

- D1 仅对 `TRANSIENT_IO` + Need∈{NEED_EXECUTION,NEED_VALIDATION} 触发带退避重试。
- D2 重试上限/退避参数进 `config.yaml`；撞上限 → 升级为 `GOAL_BLOCKED`（交给上层/用户）。
- D3 Decision 记标签 `RETRY_WITH_BACKOFF`，进可观测日志。

**交付物**：`agent/policy.py` 的第一条规则 + 单测（注入 transient 信号验证重试与上限升级）。
**验收**：transient 失败自动重试成功的端到端自检；非 transient 不误重试。

## 块 E — World State + Failure Memory（跨步/跨会话记忆）

**目标**：让"差距"和"失败"被记住，不再每步从零判断。

- E1 `WorldState`：当前会话内累积的 Need 历史、已证伪路径（`APPROACH_INVALIDATED` 的具体路径）、未决阻塞。
- E2 `FailureMemory`：把 `(上下文指纹, taxonomy, 失败的 Decision)` 持久化（复用 `store/`）。
- E3 Planner 在出 Decision 前查 FailureMemory，避开已知死路（"此路两次不通就别再走"）。

**交付物**：`agent/world_state.py` + `store` 扩展 + 单测。
**验收**：构造"同一死路连撞"场景，验证第二次自动绕开。

## 块 F — Golden Dataset + 回归门（Learning 的安全网）

**目标**：在动 Planner 策略前，先有"语料验证"的能力，否则 Learning 无法安全上线。

- F1 收集 N 条真实/构造任务轨迹为 Golden Dataset（含期望 Need 序列、期望结果）。
- F2 回归跑：任一 `Need→Decision` 改动都在 Golden 上比对，回归即拦。
- F3 接入"全回归"流程（CLAUDE.md 的两条命令再加一条 golden 跑）。

**交付物**：`tests/golden/` + runner。
**验收**：故意劣化一条策略，golden 门能红。

## 块 G — Learning Engine（优化 Need→Decision 映射）

**目标**：最终能力——在稳定 Need 之上，自动/半自动改进 `Need→Decision` 映射，且每改必过 Golden。

- G1 离线聚合：按 `Need × taxonomy` 统计各 Decision 的成功率（来自 FailureMemory + 轨迹）。
- G2 候选策略生成（"Search 失败两次转 Browser"这类），**人审 + Golden 验证**后才生效。
- G3 策略可退役/回滚；每条策略带"语料证据"。

**交付物**：`agent/learning/` + 策略存储 + ADR 0016。
**验收**：用历史轨迹跑出一条可解释、Golden 验证通过的策略改进。

---

## 依赖关系

```text
A(契约) ─► B(事实) ─► C(分类) ─► D(硬规则重试)
                          └─► E(World/Failure记忆) ─► F(Golden门) ─► G(Learning)
```

A 是地基，必须先过。B/C 可并行起步但 C 依赖 B 的 signals。D 是"决策层不必是大引擎"的最小证明。F 必须早于 G——**没有语料门，不准上 Learning**。

## 当前状态

- crazy 块 1–4：已实现并 Windows 验收通过（块4 于 2026-06-29）。
- 浏览器直通：已切 Google Chrome，功能页文案 + 手动安装命令已加（待 Windows 视觉验）。
- **块 A：已完成**（2026-06-30，行为等价重构，全回归绿）。
- **块 B：下一步**——Evaluator 标准化。
