# ADR 0019 — Architecture Review Mode（规划模式下的多角色方案评审，草案）

状态：**草案 v2（pre-implementation，Consensus 已达成）**（2026-06-30）。本 ADR 先于实现立项——把设计基线定下来再动工，
正是用"先评审再动工"的方式来设计这个"评审再动工"功能本身。**v2 = 跑了一轮真实评审后的收敛版**：
GPT 出 proposal、我做 Execution+Architecture review、达成 Consensus（见下「评审收敛记录」），锁进五条设计约束。
MVP 切片（见下）待用户确认后实现，全回归（Python + 前端）+ Golden 扩充 + Windows 真机验后转"已实现/已验证"并定版。
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
- **评审单位 = Decision 对象，不是文档文本**（v2 锁定）：reviewer 永远针对**一个具体决策的"当前选择 vs 备选的 tradeoff"**
  发言，不评"第三段写得不好"。否则评审迅速烂成改措辞的口水会。见下「评审单位：Decision 对象」。
- **两个对冲角色起步：Execution ⟷ Architecture**（v2 调整）：把 GPT/Claude 张力**显式建模**为两个对冲 reviewer——
  **Execution Reviewer（压范围/可交付/Golden 怎么验）** ⟷ **Architecture Reviewer（拉天花板/防短视）**。
  "往下压"那一极用**可证伪的 Execution**（48h 能做吗？会改 100 个文件吗？有更小 MVP 吗？Golden 怎么验？）取代虚的"Simplicity"。
  Risk/Security 等更多角色是后续增强。
- **Consensus = 四态结构化文档，不是数值**（v2 调整）：评审产出 `Accepted / Rejected / Deferred / Need User Decision`
  四段——正是 ADR 体例，评审完 ADR 自动生成。用户不读两万字互评，只看四态与待决项。
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

## 评审单位：Decision 对象（①③④合一的核心洞见）

评审、四态共识、停止条件**不是三个功能，是同一个数据结构的三个切面**。只要把评审单位**物化成 Decision 对象**，
后两者是它的字段和对它的计数——这正是 0014/0016 一路的"**物化你要学习的，别建你不需要的引擎**"：建一个对象，不建三个引擎。

```
Decision
  id              # 稳定标识
  title           # 这个决策在定什么（如 "会话存储用 SQLite 还是 DuckDB"）
  current_choice  # 当前方案的选择
  alternatives    # 备选项 + 各自 tradeoff
  rationale       # 为什么当前这么选
  status          # Accepted | Rejected | Deferred | NeedUser  ← 就是四态共识
```

- **评审对象 = Decision（①）**：reviewer 针对某个 Decision 的 `current_choice vs alternatives` 发言，不评文档措辞。
- **四态共识 = Decision.status（③）**：`Accepted`（采纳）/ `Rejected`（否决，附理由）/ `Deferred`（后置，附触发条件）/
  `NeedUser`（升级给用户拍板）。把所有 Decision 按 status 分组打印，**就是一份 ADR**。
- **停止条件 = 数 Decision（④）**：见下「停止条件」——本质是数 `status==NeedUser` 或带未决 blocking 的 Decision 条数，可证伪、零分数。

## 停止条件（可验证，绝不用百分比）

Review 极易陷入"A 批 → 改 → B 批 → 改 → A 再批…"的无限循环——这是 [0018] 搜索 judge 无限重搜的翻版。
故停止条件**全部可证伪、可数**，满足任一即停：

1. **达到最大 Review 轮数**（MVP 取 2~3，同 `research_max_rounds` 纪律）。
2. **连续一轮零新增 blocking Decision**（没人再提出新的阻塞问题 → 收敛）。
3. **连续两轮只改措辞、零新增/变更架构 Decision**——边际收益归零信号（同搜索的 loop-until-dry）。

注意第 1、2、3 条都只数 Decision/blocking 的**条数变化**，没有任何"共识度"百分比。

## 复用，别重建

本功能 = 拼装已有积木，不新增基础设施：
`delegate ROLES`（reviewer 角色）+ judge 注入（裁判机制）+ `EnterPlanMode/ExitPlanMode` + 计划审批 gate
+ [0018] 的"轮数预算 / 收敛即停"纪律。

两条配套纪律直接搬搜索那套：
1. **每轮 Review 有轮数预算**——防无限互评（同 `research_max_rounds`）。
2. **不是每个任务都进 Review**——琐碎改动不强加；按复杂度触发或手动 opt-in。

## MVP 切片（单模型即可）

1. 两个对冲 reviewer 角色 **Execution ⟷ Architecture** 的 system prompt（delegate 新增两个 Role）。
2. 评审单位 = **Decision 对象**（上述字段）；reviewer 针对 Decision 发言。
3. 流程：`Proposal（抽出 Decision 列表） → 两角色 Review（针对 Decision 提 blocking / 改 status） → 一轮 Revise`。
4. Consensus 合成器：把 Decision 按 `status` 四态分组 → 产出 `Accepted / Rejected / Deferred / NeedUser` 文档。
5. 开工 gate：`未决阻塞（status==NeedUser 或带 open blocking 的 Decision）== 0` **且** 用户签字（手动），二者皆满足才解锁"开始编码"。
6. 停止条件按上节三条（轮数 / 零新增 blocking / 连两轮只改措辞）任一触发。
7. 沉淀：本 ADR 转正 + 一条 DEVLOG。

**不进 MVP（后续增强）**：多 provider 真并行、多轮往返、Risk/Security 等更多角色、复杂度自动触发器、
完整工程闭环（见「北极星」）。

## 北极星（不进 MVP，记录方向）

更远处，本功能不止是"评审"，而是一条从想法到经验沉淀的工程闭环：
`Proposal → Review → Decision → ADR → Implementation → Evaluation → Lessons Learned`——
把规划、评审、ADR、实现、[0017] 学习引擎串成一条线（GPT 称之为 "Architecture Decision Studio"）。
**但品牌名与全闭环等一个 MVP 切片在 Windows 上真跑出一份四态共识文档之后再谈**——
零行代码时给功能起宏大品牌名，正是本项目一路在防的"建你还不需要的东西"。

## 影响

- 纯增量、opt-in：默认不改变现有规划模式行为；不开 Review 时与 3.49.0 等价。
- Golden：新增 kind（如 `design_review` / `consensus_gate`）冻结"有未决阻塞 → gate 锁死"与"零未决 → 可开工"两条基线，
  确保"禁共识百分比、只认可数阻塞"的纪律不被回归破坏。
- 无新密钥、无新网络依赖、无新引擎；单模型离线可跑。

## 评审收敛记录（v2 Consensus）

本 ADR 自身跑了一轮评审：proposal（GPT）→ Execution+Architecture review（Claude）→ 达成以下 Consensus。

**Accepted（采纳）**：
- 评审单位 = Decision 对象，不评文档文本。
- Consensus 四态 `Accepted/Rejected/Deferred/NeedUser`，且**就是 Decision.status**（与"Decision 对象"合一）。
- 停止条件全部可证伪、可数（轮数 / 零新增 blocking / 连两轮只改措辞），不用百分比。

**Rejected（否决，附理由）**：
- "再加第三个 Execution Reviewer"——否决。理由：违反"MVP 要小"。**改为用 Execution 取代虚的 Simplicity**，
  保留两极张力（Execution 压 ⟷ Architecture 拉），且 Execution 比 Simplicity 可证伪。

**Deferred（后置，附触发条件）**：
- "Architecture Decision Studio" 全闭环与品牌名——后置到 MVP 跑通一份共识文档之后（见「北极星」）。

**NeedUser（已由用户拍板）**：
- 是否现在动工 MVP —— 用户已绿灯（2026-06-30）。

## 备选与否决

- **共识百分比 gate**：否决（见上，违反 0014 禁 score）。
- **真·多模型 chat arena**：否决为 MVP 形态——价值低、成本高（多 key/多延迟）、不收敛；作为后续可选增强。
- **每个任务强制 Review**：否决——琐碎改动强加评审反伤体验（同 0018 "不是每次搜索都判质量"）。
- **第三个 Execution Reviewer（在两角色外再加）**：否决——违反 MVP 小切片；改为 Execution 取代 Simplicity 极。
