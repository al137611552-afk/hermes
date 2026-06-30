# ADR 0019 — Architecture Review Mode（规划模式下的多角色方案评审，草案）

状态：**草案（pre-implementation）**（2026-06-30）。本 ADR 先于实现立项——把设计基线定下来再动工，
正是用"先评审再动工"的方式来设计这个"评审再动工"功能本身。MVP 切片（见下）待用户确认后实现，
全回归（Python + 前端）+ Golden 扩充 + Windows 真机验后转"已实现/已验证"并定版。
关联：[0014 评估/策略架构](0014-evaluation-policy-architecture.md)（**禁 score** 纪律的源头，本 ADR 的硬约束）、
[0005 工具与 Agent 循环](0005-tools-and-agent-loop.md)（delegate ROLES / judge 注入 / PlanMode 是本功能的复用积木）、
[0018 Research Evaluator](0018-research-evaluator.md)（"轮数预算 / 收敛停止"纪律直接搬用）。

## 背景

Hermes 已有规划模式（`EnterPlanMode/ExitPlanMode` + 计划审批 gate）：先出计划、用户批准、再动工。
但计划是**单视角一次成型**——模型写完即交，没有"被反复批评-修正-收敛"的环节。

真实体验给出了改进方向：用户在外部用两个模型（GPT/Claude）来回讨论同一个方案设计时，体验明显更好。
拆解这个体验，**价值不在"有两个模型"，而在同一份方案被对冲视角反复挑刺、修正、收敛**——
一个使劲压复杂度、一个使劲拉天花板，张力之下方案更稳。Chat arena（N 个模型各说各话、不收敛）是最低价值形态。

## 决策

在规划模式内新增 **Architecture Review Mode**（opt-in）：`Proposal → 多角色 Review → 修正 → Consensus → 用户 gate 开工`。
四条设计原则：

- **角色 > 模型（Hermes 原生）**：Reviewer 不是"另一个 provider"，而是**已有 `delegate` ROLES 体系下的新角色 system prompt**
  （复用 judge 注入式裁判同款机制）。单模型可轮流扮演多个 reviewer——**离线、无需多 key、无额外延迟**。
  这不是新引擎，是"新角色 + 一个编排循环"（遵循"别建你不需要的引擎"/YAGNI）。
- **两个对冲角色起步**：把用户观察到的 GPT/Claude 张力**显式建模**为两个对冲 reviewer——
  "压复杂度"（Simplicity / Conservative）与"拉天花板"（Architecture / Innovation）。Risk/Security 等更多角色是后续增强。
- **Consensus Builder 是产品杠杆**：评审产出**结构化共识文档**——`一致 / 分歧 / 待用户决策` 三段，正是 ADR 体例。
  用户不读两万字互评，只看共识与待决项。
- **开工 gate 卡在可数事实，不卡共识百分比**：见下「为什么禁共识百分比」。

## 为什么禁共识百分比（本 ADR 的硬刹车）

一个自然但**错误**的设计是"共识度 ≥ 80% 才能开工"。**坚决不做。**
那个百分比是**模型臆测的模糊分**——不可证伪、今天 80 明天 40，正是 [ADR 0014] 花数日从搜索循环里清掉的
`expected_gain` 同款陷阱（"禁 Evaluation 带 score"的同一条纪律）。

门要卡在**可数的确定事实**上：`未决阻塞性问题（blocking issues）== 0`。
- "还有 6 个未决问题，开工按钮灰着" —— **诚实、可证伪、可 Golden**。
- "共识度 40%" —— **编的**。

每条阻塞问题是一个具体、可勾掉的条目（由 reviewer 提出、在修正轮被解决或转为"待用户决策"），
不是一个连续分数。这与 0014 "喂事实而非打分"一脉相承。

## 复用，别重建

本功能 = 拼装已有积木，不新增基础设施：
`delegate ROLES`（reviewer 角色）+ judge 注入（裁判机制）+ `EnterPlanMode/ExitPlanMode` + 计划审批 gate
+ [0018] 的"轮数预算 / 收敛即停"纪律。

两条配套纪律直接搬搜索那套：
1. **每轮 Review 有轮数预算**——防无限互评（同 `research_max_rounds`）。
2. **不是每个任务都进 Review**——琐碎改动不强加；按复杂度触发或手动 opt-in。

## MVP 切片（单模型即可）

1. 两个对冲 reviewer 角色（压复杂度 / 拉天花板）的 system prompt。
2. 流程：`Proposal → 两角色 Review → 一轮 Revise`。
3. Consensus 合成器：产出 `一致 / 分歧 / 待决` 文档。
4. 开工 gate：`未决阻塞 == 0` **且** 用户签字（手动），二者皆满足才解锁"开始编码"。
5. 沉淀：本 ADR 转正 + 一条 DEVLOG。

**不进 MVP（后续增强）**：多 provider 真并行、多轮往返、Risk/Security 等更多角色、复杂度自动触发器。

## 影响

- 纯增量、opt-in：默认不改变现有规划模式行为；不开 Review 时与 3.49.0 等价。
- Golden：新增 kind（如 `design_review` / `consensus_gate`）冻结"有未决阻塞 → gate 锁死"与"零未决 → 可开工"两条基线，
  确保"禁共识百分比、只认可数阻塞"的纪律不被回归破坏。
- 无新密钥、无新网络依赖、无新引擎；单模型离线可跑。

## 备选与否决

- **共识百分比 gate**：否决（见上，违反 0014 禁 score）。
- **真·多模型 chat arena**：否决为 MVP 形态——价值低、成本高（多 key/多延迟）、不收敛；作为后续可选增强。
- **每个任务强制 Review**：否决——琐碎改动强加评审反伤体验（同 0018 "不是每次搜索都判质量"）。
