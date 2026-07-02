# ADR 0019 — Architecture Review Mode（规划模式下的多角色方案评审，草案）

状态：**草案 v4（引擎+接线已上线，改造"可见分屏辩论 + 产品⟷技术双镜头 + 默认异构"进行中）**（2026-07-02）。本 ADR 先于实现立项——把设计基线定下来再动工，
正是用"先评审再动工"的方式来设计这个"评审再动工"功能本身。**v2 = 第一轮评审收敛**（GPT proposal → Execution+Architecture
review → Consensus，锁五条约束）；**v3 = 第二轮评审收敛**（针对"单模型 review 是否受限"，锁"Reviewer 是契约非模型 +
异构靠 `Role.model` 接线 mapping + 复杂度门控第二模型"，见「单模型 vs 异构模型」）；**v4 = 第三轮评审收敛**（针对"现产物像单模型提炼、看不到讨论过程"这一真机反馈，锁"对冲角色改 产品⟷技术、
手动评审默认异构 2 模型、辩论过程分屏可见、评审生命周期加终态"，见「v4 变更」）。纯逻辑引擎 + 接线已落地（见 MVP「进度」）。
v4 的分屏 UI / 角色改写 / 默认异构 / 生命周期终态待 Windows 真机验后转"已验证"并定版。
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

## 单模型 vs 异构模型：Reviewer 是契约，不是模型（v3 补充）

评审第二轮提出一个真问题：**单模型 review 受限于该模型自身的能力/偏见，对方案提升有限吗？**

**会受限——但解法不是"堆模型"，是"降低错误相关性"。** 第二个模型有价值**不因为更聪明，而因为错误相关性（error
correlation）更低**：同模型多角色共享权重→共享盲区，容易从"Event Sourcing"收敛成"那做个 Lite 版吧"，方向没被真正推翻；
异构模型的偏见不同（一个倾向抽象/扩展点，一个倾向"真的需要吗"），冲突才是真冲突。这正是 Code Review 有效的原因——
不是 reviewer 更强，是两人犯同一个错的概率更低。

**但 Hermes 不把架构绑死在"两个 LLM 对话"上。锁两条原则：**

1. **Reviewer 由"输出契约"定义，不由"是不是 LLM"定义**。引擎的 seam 是 `review_fn(name, prompt) -> str`——
   **引擎完全不认识"模型"概念**，只按 reviewer 名字喊。任何东西（同模型 / 异构模型 / 规则 / 静态分析器 / 成本检查器）
   只要吃 Decision、吐 `{id,status,add_blocking,resolve_blocking}` JSON，就是合法 reviewer。可插拔流水线是**自然延伸**，
   不是要现在建的引擎（YAGNI）：seam 已在，将来挂静态分析器零引擎改动。
2. **异构 = 接线层一个 mapping，不是新基础设施**。delegate 的 `Role` **已有 `model: str | None` 字段**
   （"该角色用的模型档案"）。接线层据 reviewer `name` 把某角色路由到不同模型档案即可——Planner 用主模型、
   Architecture reviewer 路由到另一档。这是**已有能力**，不是要新写的"External Reviewer 子系统"。

**复杂度门控第二模型**（投产比最高，同"不是每个任务都进 Review"）：90% 任务用**同模型双角色**（离线、零额外成本、零延迟）；
只有真正的大设计（新架构 / ADR / 核心 Loop / Memory / Evaluation）才把其中一个 reviewer 路由到**异构模型**。
默认同模型保证开箱即用；异构是高复杂度场景的可选增强，靠 config 指定。

**职责分离（采纳，且部分已结构性强制）**：Planner 只创造（禁自评）、Reviewer 只攻击（**禁重写 proposal**）、
Consensus 只总结（禁发明）。其中"Reviewer 禁重写"**引擎已物理强制**：`apply_review` 只允许 reviewer 改 `status`、
增删 `blocking`，**碰不到 `current_choice`**——reviewer 结构上无法重写方案，只能找问题。

## 复用，别重建

本功能 = 拼装已有积木，不新增基础设施：
`delegate ROLES`（reviewer 角色）+ judge 注入（裁判机制）+ `EnterPlanMode/ExitPlanMode` + 计划审批 gate
+ [0018] 的"轮数预算 / 收敛即停"纪律。

两条配套纪律直接搬搜索那套：
1. **每轮 Review 有轮数预算**——防无限互评（同 `research_max_rounds`）。
2. **不是每个任务都进 Review**——琐碎改动不强加；按复杂度触发或手动 opt-in。

## MVP 切片（单模型即可）

1. 两个对冲 reviewer 角色 **Execution ⟷ Architecture** 的 directive（引擎已含 `REVIEWERS`）。
2. 评审单位 = **Decision 对象**（上述字段）；reviewer 针对 Decision 发言。
3. 注入式 seam `review_fn(name, prompt) -> str`：引擎按 name 喊 reviewer，**默认同模型双角色**（离线零成本）；
   接线层可据 name 把某角色路由到异构模型档案（利用 `Role.model`），复杂度高时启用。
4. 流程：`Proposal（抽出 Decision 列表） → 两角色 Review（针对 Decision 提 blocking / 改 status） → 一轮 Revise`。
5. Consensus 合成器：把 Decision 按 `status` 四态分组 → 产出 `Accepted / Rejected / Deferred / NeedUser` 文档。
6. 开工 gate：`未决阻塞（status==NeedUser 或带 open blocking 的 Decision）== 0` **且** 用户签字（手动），二者皆满足才解锁"开始编码"。
7. 停止条件按上节三条（轮数 / 零新增 blocking / 连两轮只改措辞）任一触发。
8. 沉淀：本 ADR 转正 + 一条 DEVLOG。

**进度**：第 1 刀（纯逻辑引擎 `agent/design_review.py` + 15 单测 + Golden `consensus_gate`/`review_stop`）已完成、全回归绿。
第 2 刀（conversation/api/前端接线 + 复杂度门控异构路由）待 Windows 验。

**不进 MVP（后续增强）**：异构模型的复杂度**自动**触发器（MVP 先手动/config）、多轮往返、Risk/UX/Security 等更多 reviewer、
可插拔非模型 reviewer（静态分析/成本检查，seam 已就绪）、完整工程闭环（见「北极星」）。

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

## 评审收敛记录（Consensus，按轮）

本 ADR 自身在反复跑这个评审流程——proposal → Execution+Architecture review → Consensus，逐轮演进。

**第一轮 Accepted（采纳）**：
- 评审单位 = Decision 对象，不评文档文本。
- Consensus 四态 `Accepted/Rejected/Deferred/NeedUser`，且**就是 Decision.status**（与"Decision 对象"合一）。
- 停止条件全部可证伪、可数（轮数 / 零新增 blocking / 连两轮只改措辞），不用百分比。

**第一轮 Rejected（否决，附理由）**：
- "再加第三个 Execution Reviewer"——否决。理由：违反"MVP 要小"。**改为用 Execution 取代虚的 Simplicity**，
  保留两极张力（Execution 压 ⟷ Architecture 拉），且 Execution 比 Simplicity 可证伪。

**第二轮 Accepted（采纳，针对"单模型 review 是否受限"）**：
- 第二模型的价值 = **降低错误相关性**（非"更聪明"）；单模型多角色有真天花板（共享盲区），承认。
- **Reviewer 由输出契约定义、非由"是不是 LLM"定义**：seam `review_fn(name, prompt)`，引擎不认识"模型"。
- **异构 = 接线层据 name 路由的一个 mapping**，复用已有 `Role.model` 字段，非新子系统。
- **复杂度门控第二模型**：默认同模型双角色（离线零成本），仅大设计才路由异构。
- **职责分离**：Planner 只创造 / Reviewer 只攻击（禁重写，`apply_review` 已物理强制）/ Consensus 只总结。

**第二轮 Rejected（否决，附理由）**：
- "把 Reviewer 拆成 Complexity/Risk/Maintainability/UserValue 四个"——否决进 MVP。违反"MVP 要小"；
  seam 已可插拔，更多 reviewer 留作后续增强。
- "现在就建通用 Review Pipeline 框架"——否决。YAGNI；`review_fn` 注入点已是 seam，不需要框架。

**Deferred（后置，附触发条件）**：
- "Architecture Decision Studio" 全闭环与品牌名——后置到 MVP 跑通一份共识文档之后（见「北极星」）。
- 异构模型的**自动**复杂度触发——MVP 先手动/config，后置。

**NeedUser（已由用户拍板）**：
- 是否现在动工 MVP —— 用户已绿灯（2026-06-30）。

## 备选与否决

- **共识百分比 gate**：否决（见上，违反 0014 禁 score）。
- **真·多模型 chat arena**：否决为 MVP 形态——价值低、成本高（多 key/多延迟）、不收敛；作为后续可选增强。
- **每个任务强制 Review**：否决——琐碎改动强加评审反伤体验（同 0018 "不是每次搜索都判质量"）。
- **第三个 Execution Reviewer（在两角色外再加）**：否决——违反 MVP 小切片；改为 Execution 取代 Simplicity 极。
- **四个领域 Reviewer / 通用 Pipeline 框架（现在就建）**：否决——YAGNI；`review_fn(name,prompt)` seam 已可插拔，
  非模型 reviewer（静态分析/成本）将来挂同一 seam，零引擎改动。
- **把架构绑死"两个 LLM 对话"**：否决——抽象的是 *Review 能力*（输出契约），不是模型数量；异构靠 `Role.model` 接线即可。

## v4 变更（真机反馈驱动：让评审"像两个模型讨论"）

**真机反馈**：现产物"像是把主模型会话内容提炼拆解成几个确认项"，用户看不到模型之间的探讨过程；理想形态是"点评审→对话中部分屏出现 2 个模型→基于对方回复讨论 2~3 轮→收敛成共识的下一步行动方案→排规划开工"。

**诊断（关键洞见）**：引擎其实**已在跑**多轮对冲评审（`run_review`：2~3 轮 × 2 角色并行 + 停止条件 + 四态共识），差距不在能力，在两处：① **过程不可见**——前端只渲染最终决策卡/确认项，把每轮 reviewer 的发言全折叠没了；② **默认非异构**——`design_review_models` 缺省为空 → 两角色其实是同一主模型轮流戴帽（共享盲区），即便露出来也不像"两个真的不同的脑子"。故 v4 是"把已有的结构化辩论**变可见 + 变真异构**"，**不是重写引擎**。

**锁定四条（第三轮 Accepted）**：

1. **对冲角色改为 产品 ⟷ 技术**（替换原 Execution ⟷ Architecture，仍是 2 个、不叠加）：
   - **Product Reviewer（产品/市场镜头）**：市场与竞品现状、产品路线图契合度、目标用户/场景、优先级/是否过早优化。**必须可证伪**——"谁在什么场景用、服务哪个路线图目标、竞品是否已有、做出来怎么验证有人用"，**禁**"感觉这方向不错"式空话（同当年用可证伪 Execution 取代虚 Simplicity 的纪律）。
   - **Technical Reviewer（技术镜头）**：技术选型/架构/可行性/工程风险/维护成本；吸收原 Execution 的可证伪（48h/改多少文件/有无更小 MVP/Golden 怎么验）+ 原 Architecture 的拉天花板/防短视。
   - **主模型 = Consensus 合成者**（禁发明、只综合）：读两方意见 → 四态共识 + **下一步行动方案** → gate。"2 reviewer + 1 主模型收敛" = 拿到 3 方视角、不付第 3 个 reviewer 的成本。
   - **为何仍是好评审**：产品价值 ⟷ 技术代价天然拉扯（产品想要的技术上贵/有风险；技术想做的产品上不紧急），张力真实、错误相关性低（商业脑 vs 工程脑 + 异构模型进一步降相关）。ADR 硬骨架全不动：评审单位=Decision、Reviewer 禁重写 `current_choice`（`apply_review` 物理强制）、四态=Decision.status、gate 卡可数阻塞（禁百分比）、停止条件可数。
2. **手动评审默认异构 2 模型**（仅"用户主动点评审"时触发，**不**绑每条消息、**不**给琐碎方案强加）：既然要真·两个脑子，同模型双角色达不到效果，故评审这个动作默认走异构。成本只在用户主动深度评审时发生。异构 = 接线层据 reviewer 名（`product`/`technical`）路由到不同模型档，**设置面板下拉已支持按角色选模型**（火山单 key 即支持 kimi/deepseek/minimax 等多档，异构零额外 key 成本）。默认建议对：产品镜头→偏发散/商业档、技术镜头→偏严谨/代码档、收敛→主模型；用户可在面板改。
3. **辩论过程分屏可见（渐进披露）**：点评审 → 对话区中部分屏两列（产品 / 技术），**逐轮显示**两 reviewer 针对各 Decision 的发言与相互回应，主模型收敛区在下方产出四态共识 + 下一步行动方案。**共识文档是主角、过程可折叠回看**——既满足"看得到讨论"，又不违反 v2"用户不读两万字互评"。仍**不做**自由 chat arena（无结构、不收敛），辩论始终绑在 Decision 对象上、受停止条件约束。
4. **评审生命周期加终态（修 bug）**：现 bug——点"开始编码"（签字+重排待办+已开工）后，切走再切回，评审栏重现且可再次点"开始编码"。根因＝签字/已开工状态未随会话持久化、重挂按 pending 重渲。v4 给评审会话加 `consumed/started` 终态并持久化到会话；重挂时按终态渲染**只读收条**（"本方案已开工"），不再重现可点 gate。

**默认档策略**：普通评审即异构（见 2），不再区分"快跑/深度"两档——因为评审本就是低频的主动动作。

**实现切片（MVP 小切口，逐刀过全回归）**：① 引擎 `REVIEWERS` execution/architecture → product/technical + 两 directive 改写；Golden 引用 reviewer 名的基线同步。② 配置层 `design_review_models` 键迁移 + 设置面板 label 改「产品镜头/技术镜头」+ 旧值兼容迁移。③ 会话生命周期终态 + 持久化（修 bug）。④ 前端分屏 UI（纯逻辑落 `web/pure.js` 配单测；注意 WebView2 滚动坑：分屏内滚用 padding 不用 margin、别给容器只设 overflow-x）。⑤ `run_design_review` 把逐轮逐角色发言结构化返回给前端（MVP 先"整轮跑完显示全过程"，逐 token 实时流式作为后续增强）。⑥ ADR 转正 + DEVLOG。⑦ 全回归（Python+前端+Golden）→ Windows 真机验 → 定版。

## 评审收敛记录补：第三轮（v4）

**第三轮 Accepted（采纳）**：对冲角色 产品⟷技术（替换 Execution⟷Architecture，仍 2 个）；手动评审默认异构 2 模型（仅主动触发）；辩论过程分屏可见（共识为主角、过程可折叠）；评审生命周期加终态修"切走切回可重复开工"bug。

**第三轮 Rejected（否决，附理由）**：
- **3 个 reviewer（产品/技术再加第三极）**——否决。主模型收敛已是第 3 方视角，3 个 reviewer 增 1.5x 延迟/成本、更多阅读量、更难收敛，违反"MVP 要小"。
- **产品镜头做成自由发挥的"市场感觉"**——否决。必须可证伪、针对 Decision，否则退化成空话（守 0014 禁虚分纪律）。
- **改成自由 chat arena 式两模型对话**——否决。无结构不收敛、逼用户读长文；保留 Decision 对象 + 停止条件的结构化辩论，只把过程"变可见"。

**Deferred（后置，附触发条件）**：逐 token 实时流式分屏——后置到 MVP"整轮显示全过程"跑通之后。异构模型的自动复杂度触发——仍手动/config。
