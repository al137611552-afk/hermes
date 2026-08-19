# Hermes 开发路线图

> **现状：v3.70.1（2026-08-14，已定版推 main+tag）**。本文件分四部分：
> **第一阶段** 评估/策略内核 A–H（✅ 收官，定版 3.46.0–3.48.0）→
> **第二阶段** 能力面铺开 v3.49–v3.70（✅ 已交付，按线索归并）→
> **第三阶段** 喂饱评估内核 V0–V5（⏳ 提议中，ADR 0027）→
> **待办** 当前未做项（分三档）。
> 节奏纪律（见 CLAUDE.md）：每块 = 实现 → 全回归全绿 → 用户 Windows 验 → 通过后定版 → 下一块。

---

# 第一阶段 — 评估/策略内核（块 A–H）✅ 已收官

> 配套 ADR：[`docs/adr/0014-evaluation-policy-architecture.md`](adr/0014-evaluation-policy-architecture.md)
> 适用范围：**整个 Hermes 执行内核**（Coding / Search / Vision / Research 全部 Skill），不止 crazy 模式。

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

## 块 B — Evaluator 标准化（事实层）✅ 已完成（2026-06-30）

**目标**：让每个 Skill 产出结构化 Evaluation，而非散落的字符串/退出码。

- B1 ✅ `Evaluator` 协议 + `evaluate()` 调度器（`agent/evaluators/base.py`），输出 `Evaluation{metrics,signals,issues,confidence}`。
- B2 ✅ 三个 Evaluator：`CodingEvaluator`（pytest/runner/verify 测试输出→通过数/总）、`SearchEvaluator`（grep/glob/search_code→命中数、空结果信号）、`ShellEvaluator`（`[exit code]`/stderr/超时/缺程序）。调度优先级 Coding>Search>Shell。
- B3 ✅ `score()` 仅 UI 投影、`Evaluation` 不存 score、决策不读（测试守 `test_evaluation_has_no_score_field`）。
- 接线 ✅ `loop.py _emit_result` 附 `eval`（纯观测，try/except 包死，不参与控制流）；前端 `formatEval` + `.tr-eval` 摘要条。

**交付物**：`agent/evaluators/`（base+coding+search+shell）+ `tests/test_evaluators.py` 24 测；`web/pure.js formatEval` + `tests/web` 5 测。
**验收**：✅ 回归全绿（Python 46 + 前端 28）；后端事实层全自检过。**前端摘要条视觉待 Windows 真机看一眼**（DOM，Linux 看不了）。

## 块 C — Error Taxonomy（差距的可聚合分类）✅ 已完成（2026-06-30）

**目标**：把 `signals/issues` 归并到稳定的错误分类，作为 Failure-Memory / Learning 的 key。

- C1 ✅ `ErrorClass` 9 类（TRANSIENT_IO/AUTH/NOT_FOUND/SYNTAX/LOGIC/RESOURCE/AMBIGUOUS/EXTERNAL_BLOCKED/UNKNOWN），与 Need 正交。
- C2 ✅ `classify_text()` + `classify(evaluation,output)`（规则先行、按优先级、失败门控、UNKNOWN 兜底）。`agent/taxonomy.py`。
- C3 ✅ `docs/adr/0015-error-taxonomy.md` 固化语义。
- 接线 ✅ `loop.py` eval 附 `error_classes`（纯观测）；前端 `formatEval` 缀分类标签。

**交付物**：`agent/taxonomy.py` + `tests/test_taxonomy.py` 20 测 + ADR 0015。
**验收**：✅ 三类 Evaluator 典型失败均可分类（含 UNKNOWN 兜底）；优先级（TRANSIENT 最前、根因先于表象）有测试守。全回归绿（Python 47 + 前端 30）。

## 块 D — Auto-Retry（最便宜的 Need→Decision 硬规则）✅ 已完成（2026-06-30）

**目标**：第一条确定性 `Need→Decision` 规则落地，验证决策层不必是大引擎。

- D1 ✅ `decide_retry()` 仅对 `TRANSIENT_IO` 触发指数退避重试（工具调用级）。
- D2 ✅ `auto_retry`/`retry_max_attempts`/`retry_backoff_base` 进 `config.yaml`（默认开）；撞上限 → 返回最后失败交上层（不伪造 Need）。
- D3 ✅ Decision 记标签 `RETRY_WITH_BACKOFF`；`tool_retry` 事件可观测。
- 接线 ✅ `_exec_tool_with_retry` 包住串行+并行两路；判据是分类（非 ok 标志），硬错误走 classify_text 兜底。

**交付物**：`agent/policy.py` + `tests/test_autoretry.py` 12 测；config 三项。
**验收**：✅ transient 自动重试至成功 / 撞上限返回最后失败 / 非 transient 不误重试 均有测试。全回归绿（Python 48 + 前端 30）。**✅ Windows 真机已验**（真实 PowerShell 子进程端到端：执行 3 次、重试 2 次、恢复；定版 3.46.0）。

## 块 E — World State + Failure Memory（跨步/跨会话记忆）✅ 已完成并 Windows 验（2026-06-30，定版 3.47.0）

**目标**：让"差距"和"失败"被记住，不再每步从零判断。

- E1 ✅ `WorldState`（单会话纯内存）：Need 历史、按**指纹**聚合的失败计数、已证伪路径（`invalidated`）、未决阻塞（`blocked`）。
- E2 ✅ `FailureMemory`（跨会话 SQLite，`data/failures.db`）：key=`(指纹, 错误分类, 失败的 Decision)`，**一次失败=一行增量**（只记主分类，防多分类重复计数）；`known_deadend(指纹, 阈值)` 查已知死路。
- E3 ✅ `loop.py detect_repeated_failure`：每个非瞬时失败记入两者；本会话累计 ≥ 阈值**或**跨会话已知死路 → 注入"此路已 N 次不通，换思路"事实（每指纹每轮一次）。**瞬时 IO 不计**（归块D 重试）。喂事实而非硬拦截（防误报致功能缺失），与块A nudge / 块D 回灌一脉相承。
- 接线 ✅ config `failure_memory`(默认 true)/`deadend_threshold`(2)；conversation.py 主+子 Agent 两路传入懒建复用的 FailureMemory；构造器默认 `failure_memory=None` → 存量测试零行为变化。ADR 0016。

**交付物**：`agent/world_state.py` + `tests/test_world_state.py` 15 测 + config 两项 + ADR 0016。
**验收**：✅ "同一死路连撞第二次→提示换思路"、"瞬时失败不算死路"、"跨会话已知死路首撞即提示" 均有测试。全回归绿（Python 49 + 前端 30）。**✅ Windows 真机已验**（`scripts/diag_blockE.py` 11/11：SQLite 死路记忆跨会话落盘 + 真实 detect_repeated_failure 端到端 + 瞬时不误判；定版 3.47.0）。

## 块 F — Golden Dataset + 回归门（Learning 的安全网）✅ 已完成（2026-06-30）

**目标**：在动 Planner 策略前，先有"语料验证"的能力，否则 Learning 无法安全上线。

- F1 ✅ `tests/golden/cases.py`：23 条决策点语料，覆盖 A(verdict→Need) / B(evaluate 事实) / C(classify 主分类) / D(retry 决策) / E(deadend 第几次提示) 的确定性映射，每条 `输入→期望输出`。
- F2 ✅ `tests/golden/runner.py`：重放**真实**决策函数比对期望，回归即报（退出码非零）。可独立跑也可被测试调用。
- F3 ✅ 并入"全回归"——作 `tests/test_golden.py`（已在 `tests/test_*.py` 循环内，无需额外命令）；含**门活性自检**（注入错误期望必须报红，防门形同虚设）。

**交付物**：`tests/golden/`（cases + runner）+ `tests/test_golden.py`（3 测含活性自检）。
**验收**：✅ 故意劣化一条期望，golden 门报红（`test_golden_gate_catches_regression`）。23/23 语料过；全回归绿（Python 50 + 前端 30）。**纯测试工具、无运行时/GUI 行为，本地自检即等价证明，无需 Windows 验。**

## 块 G — Learning Engine（优化 Need→Decision 映射）✅ 已完成（2026-06-30）

**目标**：最终能力——在稳定 Need 之上，半自动改进 `Need→Decision` 映射，且每改必过 Golden。

- G1 ✅ 离线聚合 `aggregate(FailureMemory)`：按错误分类归并失败行 → `Aggregate`（总次数、涉及几条路、失败时的 Decision、样例 detail）。瞬时 IO 本就不进 Failure Memory，聚合天然无可重试噪声。
- G2 ✅ 候选生成 `propose()`：只对**系统性**失败升级（同分类跨 ≥min_paths 条路累计 ≥min_count 次）；单路偶发（块D/E 已管）不升级。每条候选带人话建议 + 理由 + **语料证据**。`transient_io` 双保险永不成策略。
- G3 ✅ `StrategyStore`（JSON 治理）：`proposed →(人审 approve + Golden 通过)→ active → retire/rollback`。**`approve()` 强制 `golden_passed=True`**——"没过语料门不准上"写进代码；状态变迁留 `history` 审计。
- 纪律 ✅ **不自动改运行时**：决策层仍是确定性硬规则 + 模型，G 只产**建议**；`active()` 留作将来运行时只读消费接口，本块暂不接线 loop → 零控制流改动、零回归风险（同块A/F）。ADR 0017。

**交付物**：`agent/learning/`（aggregate/propose/StrategyStore）+ `tests/test_learning.py` 14 测 + ADR 0017；`FailureMemory.rows()` 导出；Golden 门加 `learn` 类 3 条语料。
**验收**：✅ 历史轨迹（`external_blocked` 跨 3 条路反复）→ 跑出一条可解释候选 → 人审 + Golden 后 active（`test_end_to_end_one_explainable_strategy`）；approve 未过 Golden 被拒、retire/rollback 留审计均有测试。全回归绿（Python 51 + 前端 30，Golden 26 含 3 条块G）。**纯离线分析工具、无运行时/GUI 行为，本地自检即等价证明，无需 Windows 验。**

## 块 H — Research Evaluator（搜索/调研结果质量评估）✅ 已完成并 Windows 验（2026-06-30，定版 3.48.0）

> **全块 Windows 真机验证通过**（diag_blockH 22/22；"2026 最新显卡价格"实测：重搜达预算后强制停搜、模型诚实综合作答不编造价格）——下方各 Hx 内联的"待 Windows 验"以此为准已通过。

**目标**：把"结果返回了但不达标"这类**质量差距**也纳入 `Evaluation→Need→Decision` 闭环，让 Hermes 能**自判搜索好坏并重搜**。源起真实反馈：小红书搜"618 推荐女士睡衣 500 元以内"返回一堆超预算/不对题结果，Hermes 判不出、不会重搜。见 ADR 0018。

- H1 ✅ 事实层 `evaluators/research.py`（已实现 2026-06-30）：接管 `web_search`（注册早于 Search），抽**预算约束满足度**（query 解析上限 + 结果解析标价 → `within_budget`）。**blocker issue 只在可证伪时触发**（有上限/有命中/有标价却无一在内）；模糊项只当 signal。8 测含小红书验收。
- H2 ✅ 决策层（已实现 2026-06-30，待 Windows 验）：`loop.py detect_low_quality_research`——web_search 出 blocker issue → 注入"返回了但不达标，换词/换源重搜"事实促模型重搜。per-query 计数封顶（config `research_refine_max`）防无限重搜；换关键词=新 query=另起计数。**喂事实非硬拦截**（同块E）。config `research_refine`(默认 true)；构造器默认 `research_refine=False` → 存量行为零变化。
- H3a ✅ 模型裁判·文字层（已实现 2026-06-30，待 Windows 验）：`agent/judge.py` provider 注入式裁判（`judge_fn(prompt,images)`，**多模态就绪**），判语义相关性（"夏季"≠厚秋冬款、来源权威/时效）。`loop.py detect_offtarget_research` 挂 web_search 结果，H2 正则未拦时再过裁判，不对题→提示换词/换源重搜。**裁判故障/解析失败一律放行不拦**（不因模型出错误触发）。config `research_judge`(默认 true)；构造器默认 `research_judge=None` → 存量零变化。
- H3b ✅ 模型裁判·**多模态看图**（已实现 2026-06-30，待 Windows 验）：挂在带图答案**收尾呈现前**，连配图一起判（抓"配图是冬季"那一环）。`loop.py detect_offtarget_answer` + 终局 `if not calls` 钩子：本轮做过研究（web_search/browser_*）且累积了模型真"看过"的配图块（截图/浏览器图，`seen_images`）→ 连图喂裁判判图文相关性，不对题→注入提示并**再放一轮**让模型据图重选/重搜（`answer_refined` 每轮封顶一次，防无限）。`conversation.py _make_research_judge` 把 image 块合进多模态 user 消息真正喂像素（anthropic 直传 / openai 转 image_url）。**裁判故障一律放行不拦**。**已知边界**：仅判模型真看过的 image 块（浏览器截图/截屏/上传图）；配图若是模型没看过的 markdown 图 URL，需先抓取再判（后续增量）。
- H3c ✅ **萃取（三态）+ 接地/时效闸**（已实现 2026-06-30，待 Windows 验）：解决"污染结果整批丢弃→退回训练数据→过时且白搜"。① 裁判从二态升三态：`judge.py` Verdict 加 `use`（可萃取的相关少数）+ `salvageable`，prompt 要求"即使整体不对题也把相关项放进 use、绝不因掺垃圾就整批丢、绝不让人凭记忆替代"。`loop.py detect_offtarget_research` 改三态——部分污染→"挑出有效项采用并标注来源、别整批丢"（**杀掉旧的"请不要采用这些结果"措辞**，它正是诱因），基本是垃圾才重搜且禁止凭记忆顶替；H2 措辞同步加"别凭训练记忆编"。② `loop.py detect_ungrounded_answer` 接地/时效闸（纯正则零成本）挂终局：本轮做过搜索 + 问题时效敏感（价格/最新/榜单/年份…）+ 答案**既无引用又无声明** → 催"据搜到内容作答并标注来源，没有就明确声明可能过时"。**保守触发**：已引用来源或已声明过时都放行，不误杀稳定知识兜底。与 H3b 共用 `answer_refined` 每轮一次。
- H3c+ ✅ **全局重搜预算 + 止血出口**（`research_max_rounds` 默认3）：催重搜达上限→强制"停搜、综合现有+声明局限"，根治"换关键词无限重搜→1500s 交白卷"。**Novelty/Progress + 换源策略阶梯**：`extract_domains` 抽域名作 Novelty（确定性、非分数），有新域名→换词重搜、零新域名(NO_PROGRESS)→`switch_strategy_nudge` 逐级换源 `site:`→浏览器直通→ask_user。守 ADR 0014 禁 score。
- H4 ✅ Golden 语料扩到 **42 条**（+14：research_judge 三态 / grounding 闸 / 换源阶梯 / Novelty）+ Windows 真机验通过 + `scripts/diag_blockH.py` 22 项。**已定版 3.48.0**。
- ✅ **后续·上游检索已做完**：确定性重排/去重/控源多样（v3.49.0）→ FR-11.1c 宽召回 + 模型语义重排 + 读正文（v3.55.0，Windows 验）。详见下方「第二阶段 · 线索②」。
- ⏳ **后续·仍未做**：目标满足驱动的换源（触发从"零新域名"补成"目标数据点连续缺席"，价格/数字类先做）；研究墙·墙钟时间上限。Issue 生命周期 / 通用 Search Policy 抽象按需后置。见文末「待办 · 第三档」。

**交付物**：`evaluators/research.py`（H1）+ `loop.py detect_low_quality_research`（H2）+ `tests/test_research_evaluator.py` 8 测 + `tests/test_research_refine.py` 7 测 + config 两项 + ADR 0018 + Golden 2 条 + `scripts/diag_blockH.py`。
**验收**：H1 ✅ 小红书超预算结果 `within_budget=0`+blocker issue、有在预算内不误报、无标价只给 signal。H2 ✅ 不达标触发重搜提示、达标不触发、无预算不触发、per-query 封顶、换词另起计数均有测试。H3/H4 ✅（见本块开头：全块 Windows 真机验证通过 diag_blockH 22/22，定版 3.48.0）。

---

## 依赖关系

```text
A(契约) ─► B(事实) ─► C(分类) ─► D(硬规则重试)
                          └─► E(World/Failure记忆) ─► F(Golden门) ─► G(Learning)
```

A 是地基，必须先过。B/C 可并行起步但 C 依赖 B 的 signals。D 是"决策层不必是大引擎"的最小证明。F 必须早于 G——**没有语料门，不准上 Learning**。

## 第一阶段小结

- crazy 块 1–4：已实现并 Windows 验收通过（块 4 于 2026-06-29；**块 4 的阶段边界后于 v3.55.0 重做**，见线索②）。
- **块 A–D**：✅ 并 Windows 验（定版 3.46.0）。块 D 自动重试经真实 PowerShell 子进程端到端验证（执行 3 次、重试 2 次、恢复）。
- **块 E**：✅ 并 Windows 验（定版 3.47.0，World State + Failure Memory + 死路提示）。
- **块 F / G**：✅（定版 3.47.0；纯测试工具 / 纯离线分析，无需 Windows 验）。**块 G 的运行时接线后于 v3.60.0 补上**，见线索④。
- **块 H**：✅ 并 Windows 验（定版 3.48.0，Research Evaluator H1–H4 + 全局预算止血 + Novelty/换源阶梯；Golden 42 条）。

**A–H 全部实现并 Windows 验完毕**，此后主线转向能力面铺开，见下。

---

# 第二阶段 — 能力面铺开（v3.49 → v3.64）✅ 已交付

> 不再按字母块推进，改为按用户真机反馈驱动的**线索**。每条线索的权威细节在 `CHANGELOG.md` /
> `docs/PRD.md`（FR 编号）/ `docs/adr/`；此处只留"做了什么 + 停在哪"。

## 线索① 方案评审（ADR 0019）— v3.50.0 / v3.51.x / v3.56.0

- **v3.50.0** 评审模式：hub-and-spoke，多角色评审员（务实 ⟷ 严谨）只**进言**、决策权归主模型；围绕 `Decision` 结构四态共识（Accepted/Rejected/Deferred/NeedUser）；开工 gate 卡**可数事实**（未决阻塞==0 且已签字），**不出现百分比**（守 ADR 0014 禁 score）。
- **v3.51.0–3.51.2** UI 对齐 Figma（工作区标签化、lucide 图标唯一来源 `web/icons.js`、明暗切换）+ shell 执行健壮性四类挂死修复（杀进程树 / 刷屏 OOM / `&` 继承管道 / 非交互 `hardened_env()`）。
- **v3.56.0 可用性收口**：一口气修掉评审链上五个**"看着正常、其实静默降级"**——输入靠猜→改用户指定（气泡/划选）；"多模型讨论"其实一个模型演三角色→自动挑跨 provider 异构，同构如实叫「单模型自审」；没 key 的档被派成镜头；镜头调用失败静默吞成空串；评审员听不见任何人→第 2 轮起喂上一轮 hub 回复（**仍不喂对方镜头**，独立性是降错误相关性的核心）。第二个出口 `hand_review_to_main`；评审状态落库（`session_review` 表）。同版：**开箱不预设模型**（`DEFAULT_PROVIDERS={}`）。
- **贯穿教训**：固定预算 + 输出随规模线性增长 → 必被截断（`scale_review_budget`），**且预算与超时必须成对伸缩**（只涨预算不涨超时＝照样写一半被打断）。

## 线索② 检索与研究质量 — v3.49.0 / v3.53.x / v3.55.0

- **v3.49.0** 确定性重排/去重/控源多样 + CJK 2-gram 分词（本地、无 key、无模型、无延迟）。
- **v3.53.0 FR-11.1b 分工修正**：**搜索恒走 HTTP，浏览器只读页面**——推翻 v3.43 的 `_drop_web_when_browser`（挂上浏览器就摘掉 web 工具），实测证明搜索引擎对自动化浏览器返回**空壳结果页**，那等于砍掉唯一稳定搜索通道。现结构＝`web_fetch` 受阻**自动升级**到浏览器读同一 URL（代码保证，不靠 prompt）；多引擎并发 + RRF 融合 + readability 式正文抽取。同版：应用内更新提醒**默认关**（ADR 0020 能力保留）。
- **v3.53.1** 官方技能库 `anthropics/skills` 进内置精选（17 个技能）；超长 description 从"整个技能拒收"改"截断+标注"。
- **v3.55.0 FR-11.1c 上游检索**：宽召回 → 模型语义重排 → 自动读前 3 条正文按查询摘录，故障即降级到确定性重排。**实测纠正假前提：Bing 的 `count=30`/`first=11` 全无效**，真正能加宽的只有 DDG lite 的 POST 翻页。同版修 `web_fetch` 老 bug：不解 gzip + 不读 `<meta>` charset → 整段乱码；**crazy 块 4 改由任务清单状态差分判定阶段边界**（三次真跑发现模型几乎从不发 `[[PHASE_DONE]]`，块 4 原是死代码路径）。

## 线索③ 技能生态（ADR 0014-agent-skills / 0015-skill-marketplace）— v3.52.0 / v3.58–3.60

- **v3.52.0** FR-13.S 技能包（渐进披露三层）+ FR-13.S2 技能市场（对齐 Claude Code `marketplace.json`）+ FR-13.S3 检查更新（按内容哈希）。**安全立场刻意偏离规范**：`allowed-tools` 只展示不免确认，技能正文标注为「参考资料非用户指令」；扫描分级只决定确认强度、**永远不硬拦**。
- **v3.58.0 FR-13.C1 自定义斜杠命令**（一个命令＝一个 md，文件名即命令名、中文可用；**内置命令不可被同名文件覆盖**——`/crazy` 被顶掉＝伪装后门）+ FR-11.4b **权限规则持久化与可解释**（`gate.explain()`：三种免确认原因在 UI 上必须自解释）。
- **v3.59.0 FR-13.C2 一键技能化**（程序→技能）：核心判断是**契约不该用户手填、也不该模型猜，而应由刚写完这个程序的 agent 实测得出**——内置 `skill-creator` 七步流程（含**真跑一条只读命令取样**，字段表从实测反推）+ `check_skill.py` 成稿自检。
- **v3.60.0/3.60.1** 技能跨层搬家（项目级↔全局）+ diff 行内定向反馈 + **块 G Learning 运行时接线**（见线索④）。

## 线索④ Learning 闭环补完 — v3.60.0

- 失败记忆补「做法」标签：`record` 的 `decision` 参数**唯一调用点从没传过** → 块 G evidence 恒空、`propose()` 只能吐查表话术。现记 `工具名` 与 `工具名|after_nudge`。
- 块 G 接线三件事：①消费通路（active 策略→带出处的「历史教训」注入 ≤2 条 ≤400 字）②影子记录（`learning_shadow` 只记不改路）③让策略真生效。**只有③需要语料**——押后①②的代价是永远发现不了集成 bug（`decision` 字段那个洞正是这么找出来的）。**真正要守的是"别让策略在没人点头时改变行为"**（`approve()` 强制 `golden_passed`），与接不接通路无关。无 active 策略时彻底 no-op，有测试钉死。

## 线索⑤ 工具产物化（ADR 0021 / FR-14）— v3.54.0

- 真空白＝**工具输出不可寻址**（read/shell/procs 各 20 万字符截断后原始数据永久消失）。做法：大输出落盘产物、工具回**摘要（头 60 + 尾 40 行）+ 句柄**，处理复用现成 grep/read/shell，**不引常驻 IPython**（数据可寻址＝90% 价值）。
- 五项决议：跨会话按 id 读不限 / 后台环形缓冲**必须读线程 tee** / 判据是**「发生截断且原始量≥阈值」**而非「输出够大」 / `read_file` 移出首批（源文件本就可寻址＝套娃）/ `.hermes` 检索与 git 污染都修。
- 顺带治了老 bug：40 万字符 pytest 输出的**失败汇总正好在被截掉的尾部**。真跑验过头号风险：模型看到句柄→**自发 grep 产物**→答对，没重跑命令。

## 线索⑥ 交互式命令治理（ADR 0022）— v3.61.0 / v3.62.x

- **三条立场**：不做全局 auto-yes（确认框是防误删最后一道闸）/ 不做完整集成终端（ConPTY+xterm.js 与非交互硬化对着干）/ 前台不等人（crazy 无人值守不能卡在提示上）。
- **v3.61.0** 非交互环境补齐（会改语义的用 `setdefault`；**刻意不设 `TERM=dumb`**、**不注入 `$ConfirmPreference`**）+ 交互提示识别 `looks_waiting_input`（保守三判据）→ **180s 变 5.3s**。
- **v3.62.0** 后台进程 stdin 开 PIPE，**一条通道两个入口**：`write_process_input` 工具（模型侧，过 gate）+ 工具块行内输入行（人接管，不过 gate，渲染时**强制展开**——等你操作的东西不能藏着）；写入**回显进日志**（进程无 TTY 不自回显，否则事后分不清谁答的）。
- **v3.62.1** 修 diff 增删行重叠（inline-block + 换行写在 span 里被吞）+ 新增 `scripts/diag_diff_ui.py` 62 项几何自检。三条同源教训见 CLAUDE.md。

## 线索⑦ 人机换手 + 轨迹固化（ADR 0023）— v3.63.0

- **FR-15 换手** `request_handoff(reason,target,verify)` 三条结构约束：`verify` 必填 / 交回后 **binding 自动重读现场**回灌（不信"用户说做完了"）/ **无人值守绝不放行**（超时置 blocked → crazy 收成「阻塞：待人工换手」不记完成）。与 `ask_user` **刻意分家**（无人值守下行为相反，合并会让 crazy 产假成功）；面板常驻**真实目标 + 凭据边界声明**（换手是钓鱼位）。
- **FR-16 轨迹固化**：录工具序列 + 用户纠正 + 「记一步」打点 → 归并（**不跨段**）→ 参数化 `{{变量}}` → 复用 skill-creator 流水线写 **SOP 技能**（非回放脚本）。采集挂在 `Conversation.emit` 咽喉，不录零开销。
- **三轮真机验证抓到 7 个问题**（都已修+补自检），**贯穿教训：新 UI 接进老机制时配套规则要一起接**（浮层栈 / `[hidden]` 与 `display` / `composerState` 运行态 —— 三次栽在同一件事，已进 CLAUDE.md gotchas）。

## 线索⑧ 并发可观测性（FR-17）— v3.64.0

- **起点是一篇 Grok Bot 报道**：它的三个卖点里"把电脑递给你"＝FR-15、"看着你操作一遍记下来"＝FR-16
  都在同一天交付了；剩下的"多 Bot 各司其职"照出自家一个洞。
- **查证到的实况**（比"缺可视化"具体）：三种"等你"里**只有权限确认**会置 `awaiting`，`ask_user` 与
  `request_handoff` 只 emit 事件不改状态；而 `runningSessions()` 又只收 `running|queued`，
  **连已有的 `awaiting` 都排除在外**。后果：后台会话停在换手上等人时全局零信号——
  **这不是美化问题，是 FR-15 在并发下失效**。
- **T1** 三种等待统一成 `awaiting` + reason，chip 拆 `✋ N 等你 · M 运行中`（等待段在前 + 警告色），
  指挥中心等待行排最前、点击直达。**T2** `tool_use` 顺路记「在干什么」（保留原始工具名，不另造中文动词）。
  **T3** 系统标题角标 + **后台终态闪任务栏**（有焦点才走应用内 toast）。
- **刻意不抄"多 Bot 互相通信"**：Hermes 跨会话知识走**共享存储**（`recall_history` 跨会话检索 /
  `FailureMemory` 跨会话死路 / FR-14 产物按 id 读不限），实时协调走**委派**；
  对方上通信是"每 bot 一台独立 VM、没有共享存储"的架构约束所迫，**不是优点**。
- **自测期两条真机反馈都改了**：①「越忙的会话越点不动」——把整块 `innerHTML` 重建接到高频事件上，
  用户正按着的那一行在 mousedown/mouseup 之间被换掉，click 永不触发；改成结构没变就不重建。
  ②「toast 我没注意到」——戳破 T3 的自相矛盾（立意是"你没盯着窗口"，而应用内浮层那时等于不存在），
  改成没焦点就闪任务栏。**原先"刻意不做闪烁"的判断被推翻**：那是规避方式的问题（不碰 `window.native`
  即可），不是该不该做的问题。

---

# 第三阶段 — 喂饱评估内核（块 V0–V5）⏳ 提议中

> 配套 ADR：[`docs/adr/0027-eval-corpus-and-replay.md`](adr/0027-eval-corpus-and-replay.md)
> 承第一阶段：块 A–H 把**评估内核**建起来了，这一阶段解决它的**输入**。
> **命名**：用 V（Verification 验证闭环）不用 E——第一阶段「块 E」的子项已经叫 E1/E2/E3
> （E1=WorldState、E2=FailureMemory），再用 E 会指代不清。

## 为什么是现在

与 deepseek-harness 横向对比（2026-08-19）的结论：**hermes 的评估/策略内核比对方完整——
对方的 `guard/` 只有 repeat-tool-reminder 和 timeout-policy 两个包，A–H 那套它完全没有——
但我们这套在空转。**

读码查证到，问题不是"没有语料"，而是**语料在产生、却落不了盘，且指纹是脏的**：

- ✅ **遥测已经齐了**：`loop.py` 已 emit 八种 nudge 事件（`login_hint`/`stuck_hint`/`search_hint`/
  `deadend_hint`/`research_hint`/`truncation_hint`/`learning_shadow`/`learning_advice`），
  `_emit_result` 还附 `eval`（含 `error_classes`）。每一跑都在流过，**只是没人接**。
- ❌ **落不了盘**：`EvalResult` 只活在内存，`run_eval.py` 只打印 + 退出码 → 两次跑无法对比。
- ❌ **指纹脏**：`fingerprint()` 的 `_KEY_PARAMS` 含 `path`/`file_path`/`command`，评测在 `tempfile`
  建工作区 → 同一失败每跑生成不同指纹 → `propose(min_count=3, min_paths=2)` 的**双门两个方向同时错**
  （`paths` 虚高放假信号、每指纹 `total` 恒 1 漏真信号）。
- ❌ **量级不足**：`tasks.py` 只有 6 个任务（Golden 51 条 / 13 类是健康的，端到端层不是）。

**"6 个任务测不出几个百分点的变化"**——这意味着近几十版的每一次改动，其"提升"实际上
**从未被验证过**，只有"真机跑一次感觉可以"。这是本阶段真正要解决的问题。

## 三层语料，不可互相冒充（ADR 0027 决策 1）

| 层 | 是什么 | 成本 | 进 CI | 守什么 |
|---|---|---|---|---|
| **L-Golden**（V0 前 51 条 / 13 类） | 决策函数 `输入→期望`，离线确定性、不调模型 | 毫秒 | ✅ 每次 | 决策内核不回归 |
| **L-Cassette**（新） | 录好的模型输出重放，跑完整 loop | 秒级、离线 | ✅ 每次 | **端到端行为**不回归 |
| **L-Live**（扩量） | 真模型真网络跑任务集 | 分钟级、烧 key | ❌ 按需 | **解题率**绝对水平与 A/B |

## 块 V0 — 地基修正（指纹归一 + 语料分库）✅ 已实现并本地验收（2026-08-19）

**目标**：让攒下来的语料是干净、可分离、可重置的。**不新增任何能力**（同块 A 的定位）。

- V0.1 `world_state.py:fingerprint()` 加 `workspace` 参数：`path`/`file_path` 转工作区相对路径、
  `command` 内嵌绝对路径同样归一，再取 sha1。纯逻辑不变、无 IO。
- V0.2 `FailureMemory` 路径可注入（现 `conversation.py:2279` 硬编码 `ROOT/data/failures.db`）。
  真实使用 `failures.db` / 评测 `failures.eval.db`。**分库是为了能重置**——评测要能清空重跑，
  而真实死路记忆是跨会话资产、绝不能被评测误删（同 ADR 0025 决策 7 的理由）。
- V0.3 SQLite schema 加 `source` 列（真实/评测/自检），库内细分用；**隔离靠分库、细分靠列**。
- V0.4 `harness.py` 显式指定评测语料库（现在是默认继承 `agent.failure_memory=True`，闷声写进生产库）。

**实现中发现并修正的一处设计缺口**：最初把工作区绝对路径折成 `<ws>/a.py`，但模型对同一个文件
**绝对与相对两种写法都会用**（`read_file("/tmp/ws/a.py")` 与 `read_file("a.py")`），折成 `<ws>/` 仍是两个指纹。
改为折成**工作区相对路径**（裸根记 `<ws>`），两种写法才真正同指纹。是新加的 golden 语料把它逼出来的。

**交付物**：
- `world_state.py`：`fingerprint(tool, params, workspace=None)` + `_ws_prefixes` / `_fold_workspace`
  两个纯函数；`FailureMemory(db_path, *, source=)` + `_migrate()` 补 `source` 列（**不重建表、旧行不丢**）。
- `loop.py`：`AgentLoop(workspace=)` → `detect_repeated_failure(workspace=)` → `fingerprint` 全程透传。
- `config.py` `agent.failure_memory_db`（空=默认库）；`conversation.py` 主/子 Agent 两路同口径；
  `harness.py` 指向 `data/failures.eval.db` 且标 `source=eval`。
- 自检：`tests/test_world_state.py` +10 测（25/25）、Golden +6 条 `fingerprint` 语料（51→**57**）。

**验收**：✅ 两次不同 tempdir 跑同一失败 → `rows()` 是**一行 count=2**
（`test_same_failure_across_runs_aggregates_to_one_row`）；配一条**反证**测试钉住"不归一会分裂成两行"。
✅ 本地全回归绿（Python 73/73 文件、前端 133/133 测）。

**本地验收结果（2026-08-19，13 项过 / 0 挂 / 1 项 Windows 专属跳过）**：

| # | 项 | 结果 |
|---|---|---|
| 1 | 反斜杠 / 正斜杠两种写法 → 同指纹（含命令里混写） | ✅ 纯字符串逻辑，Linux 可证 |
| 2 | 盘符绝对路径 → 与工作区相对写法同指纹；不同盘符工作区下同一相对路径同指纹 | ✅ 同上；并验证 Linux 上对 Windows 路径 `resolve()` 产生的垃圾前缀**不误伤** |
| 3 | 工作区**双形态**（原样 / `resolve()`）都能折上 | ✅ **用软链把这条路径真正压出来了**——与 8.3 短名、macOS `/private/var` 是同一条机制 |
| 3b | 8.3 短名 `RUNNER~1` → 长名展开本身 | ⊘ **Windows 专属，Linux 无法复现**。机制已由 3 证明，只剩这个触发条件没压 |
| 4 | 旧库迁移不丢数据 | ✅ **在一份真实用过的 `data/failures.db` 上真实发生**：9 行 2026-08-10 的历史数据完整保留、全部落 `source='real'`，补列成功 |
| 5 | 评测分库、真实库不被触碰 | ✅ 走真实 `_get_failure_memory` 通路：评测库独立建成、`source=eval`；真实库**行数与 mtime 均未变** |

> **仍需 Windows 真机看一眼的只剩 3b 一条**（在 `%TEMP%` 下建工作区、确认 8.3 短名也折得上）。
> 其余各项要么是纯字符串逻辑、要么已在真实库上发生过，Linux 自检即等价证明。
> 另注：跑回归时真实 `failures.db` 的 mtime 会变一次——那是 `ALTER TABLE` 补列、**不是写入行**
> （已核对：近 6 小时零新增行）。

> **已知一次性影响**：指纹口径变了，旧 `failures.db` 里的历史指纹**不再与新指纹匹配**
> （旧行仍在、不删，只是不再被命中）。死路记忆是可再生资产、量也不大，故**不写迁移脚本**——
> 留着比删了安全，代价只是短期内跨会话死路提示会冷启动一段。
> **这是实现时按「不删数据」默认原则自行拍的，用户未就此表态**——若要写迁移脚本（按旧行反查工具名+入参重算指纹）随时可补，代价是得先给 `failures` 表补存原始入参。

> 吸收待办第二档原「失败语料无来源标记」一条：**来源标记解决"混"，指纹归一解决"碎"，缺一不可**。

## 块 V1 — Run Record：让每次评测留下痕迹 ✅ 已实现并本地验收（2026-08-19）

**目标**：一次评测跑 = 一份可复查、可对比的记录。**这是后面每一块"是否真的提升"的唯一验证手段。**

**交付物**：
- **`scripts/eval/record.py`（新）** —— 纯逻辑 `summarize_events` / `config_snapshot` /
  `model_identity` / `build_record`，受控 IO `git_sha` / `write_record` / `load_run`。
  指标全部从**现有事件流**直接算，`loop.py` 一行未改。
- **`scripts/eval/report.py`（新）** —— `aggregate` / `compare` / `render` 全纯函数；
  CLI 三态：无参列出全部跑、一个参数出汇总、两个参数出差异表。
- `run_eval.py` 加 `--repeat N` / `--tag` / `--out` / `--no-record`，**默认落盘**
  （ADR 说"必须落"，那就不该是可选项；`--no-record` 只留给临时试跑）。
- `harness.py`：`EvalResult.cfg` 带出**实际生效**的配置——外面重新 `load_config()` 拿到的是
  没被 harness 改过的那份（memory/mcp/截屏都还开着），记下来就是**说谎**。
- 自检 `tests/test_eval_record.py` **19 测**，全离线、不调模型不联网。

**对比表的列**（全部来自现有事件流）：
pass@1 比率 ｜ 步数 ｜ 工具调用 ｜ 重试 ｜ 子任务/失败数 ｜ **八类 nudge 各自次数** ｜
**错误分类分布** ｜ 耗时 ｜ token。nudge 与错误分类**按类拆开**（`nudge.stuck_hint` / `err.logic`），
合成一个总数就没法归因。**无差异的指标不列**，否则一屏 0 淹没真变化。

**可比性防呆**（对比时主动喊话，不然差异会被误读成"改动的效果"）：
工作树 dirty ｜ 配置快照不同 ｜ **换了模型**（这条最响：那种对比只能看模型差异）｜
记录里没有真实 `model_id` ｜ 两次是同一 commit（差异只可能来自配置/环境/模型随机性）。

**本地验收结果**：
| 项 | 结果 |
|---|---|
| 八种 nudge 一个不漏、未触发的记 0 而非缺键 | ✅ 缺键会让对比表漏行 |
| token/步数跨 agent 合计（usage 每个 loop 发一次） | ✅ `usage_events` 记合计了几份，避免把"子任务多"误读成"主线步数多" |
| 估算用量不冒充实测 | ✅ 任一段 `measured=False` → 整条标 False |
| 配置快照剔掉每跑必变的临时路径 | ✅ 否则两份记录**永远**判为不同、掩盖真差异 |
| `--repeat` 同名任务不互相覆盖 | ✅ 覆盖了就没法算 pass@N |
| **报表活性**：故意让某 detector 多触发 → 对应列必须报出来 | ✅ `test_compare_surfaces_a_changed_detector`（ADR 0027 V1 验收判据） |
| 端到端链路（跑 → 提炼 → 三件套 → 落盘 → 读回） | ✅ **用桩 provider 压过真实 `run_eval.main()`**：git sha/配置快照/指标全部落齐，不联网不用 key |

> **未做真跑**：本机 `active_model` 为空、无模型档案（v3.56.0 起开箱不预设 provider），
> 真跑需先配档案 + 烧 key。ADR 里"同一 commit 连跑两次 `--repeat 3`"这一半留到有档案时补；
> 报表本身的活性已由单测与桩链路证明。

## 块 V1a — 评估内核缺口修复（V1 揪出的两个 bug）✅ 已实现（2026-08-19）

V1 跑通链路时，评测设施**立刻照出评估内核自己的两个 bug**——这条路走对了的第一个旁证。

**bug 1：CodingEvaluator 吞退出码。** 它优先级高于 Shell，只要输出含 "pytest" 等特征词就接管；
但**接管了却解析不出计数时，会把 shell 的退出码一起吞掉**，判成"无 issues"。于是
`pytest 不存在的文件`（exit 4，测试根本没跑起来）这一整类"测试命令本身写错了"的失败，
对评估内核完全隐形——`error_classes` 为空、不进 Failure Memory、块G 永远学不到。

**bug 2（既有，更隐蔽）：计数正则跨行匹配，凭空造出幻影计数。**
`_ERRORS = (\d+)\s+errors?` 里的 `\s` 跨行，把 `pytest-9.1.**0**\n**ERROR**: file not found`
读成"0 errors" → `total=0` → `has_counts` 为真 → 判成"用例全过"。
**bug 1 的表象其实由 bug 2 制造**，只修 1 会得到一个"看着修好了、实则走错分支"的结果。

**修法**：
- 计数正则限定**同一行**（`[ \t]+` 而非 `\s+`）+ 结尾 `\b`；`has_counts` 要求 `total > 0`
  （`0 passed` 是"一个用例都没数到"，不叫有计数）。
- 退出码升为**共享词汇** `EXIT_CODE_RE`（shell.py 单一来源，Coding 导入）——
  同一格式抄两份正则迟早漂移，本项目已因"两处写"吃过亏。
- 三条新判定：无计数 + 非零退出 → `测试未跑成=blocker`（confidence 提到 1.0，退出码是硬事实）；
  有计数全过 + 非零退出 → `退出码非零=失败`；两种措辞**刻意分开**，块C 归类时
  "没跑成"多半是 NOT_FOUND/SYNTAX、"收尾炸了"更接近 LOGIC/RESOURCE，不该混进同一个干草堆。

**自检**：`tests/test_evaluators.py` +6 测（30/30，含幻影计数回归门）；
Golden +4 条 `evaluate` 语料（57→**61**），**既有期望一条未改**（是补盲区，不是改行为）。

## 块 V2 — 任务集扩量 ⏳ 批 1/3 已完成（2026-08-19，6 → 12 个任务）

工作量最大的一块，按"每次加几个、搭别的版本一起发"推进。**批 1 = 分层基建 + 可离线构造的 L2**。

### 批 1 交付（✅ 已实现并本地自检）

- **任务分层**：`Task` 加 `tier`（L1/L2/L3）、`expect_nudges`、`network`；
  `run_eval.py` 加 `--tier` / `--offline`——ADR 已知限制 4 要求 L1/L2/L3 能分别跑，
  全量 `--repeat 3` 按小时算，只给一个"全跑"入口没法用。
- **`verify_nudges`（纯函数）**：核验 nudge 期望，接进 `run_eval` 判定。
- **6 个 L2 任务**（3 反例硬断言 + 3 正例软观测），覆盖三个**可离线构造**的 detector：

| 任务 | 类型 | 观测/禁止 |
|---|---|---|
| `neg_edit_same_file_progressing` | 反例 | 同一文件连改三次但一直在推进 → 专测 `stuck_hint` 误报 |
| `neg_small_repo_survey` | 反例 | 小项目逐个读文件 → 专测 `search_hint` 误报 |
| `neg_plain_fix` | 反例 | 一处明显 bug 改完即绿 → 全程不该有任何 nudge |
| `pos_stuck_unfixable` | 正例 | 自相矛盾的测试 → 观测 `stuck_hint` |
| `pos_browse_many_modules` | 正例 | 60 个模块逐个浏览 → 观测 `search_hint` |
| `pos_deadend_missing_tool` | 正例 | 构建工具本机不存在 → 观测 `deadend_hint` |

- **误报升为一等指标**：`report.py` 的 `nudge_violation` 列——某次改动一旦开始让 detector
  在正常路径上乱插话，diff 表直接报出来。
- 自检 `tests/test_eval_tasks.py` **17 测**（全离线）。

### 实现中确认的三条设计（已回写 ADR 0027 决策 6）

1. **正反例判据不对称**：反例是**硬断言门**（误报即 FAIL，确定性），正例是**仪表**
   （只记触发率，漏报逼不出来、硬判会把"模型表现好"误记成"detector 坏了"）。
2. **反例用 `{"*": False}` 通配**，不拆成八个任务——覆盖更全、少跑七次模型。
3. **夹具必须先过启用门**：`search_nudge_files`(40)、`stuck_edit_threshold`、`trace_run` 可用性。
   越不过门的正例是**哑弹**。自检里为此钉了两条（正例必须越门、反例必须低于门）。
4. **只有"会插话"的事件才谈得上误报**（端到端压测时踩到）：八种事件里 `learning_shadow`
   **不注入模型**（`loop.py` 只把 `learning_advice` append 进 `inject_blocks`，shadow 纯观测）。
   把纯观测事件也当误报，会让**任何一次正常失败**都被误判成"detector 乱插话"。
   故 `record.py` 分出 `INJECTING_NUDGES`（7 种），通配只禁这几种；shadow 照常计数、不背锅。

### 夹具本身也是未验代码

沿用换手真跑那次的教训，任务自检直接**跑夹具**证明：自相矛盾的测试真的失败、
反例任务的起点真的是绿的、模块数真的越过/低于门槛、判分器拒绝篡改测试文件与糊弄式回答。

### 真跑验收（2026-08-19，DeepSeek anthropic 端点 / deepseek-v4-flash）

| | 结果 |
|---|---|
| 三个**反例** | **3/3 PASS，零误报**。`neg_edit_same_file_progressing` 用了 15 次工具、同一文件改了多次，`stuck_hint` 正确没响（每步都在推进、无失败信号）——这是 detector **精度**的第一份实证 |
| 三个**正例** | 任务本身 3/3 PASS，但**触发率全为 0** |
| V0 分库隔离 | ✅ 真跑下成立：`failures.db` mtime 停在补列那一刻、9 行历史数据一次没被碰；6 条评测语料全进 `failures.eval.db` |
| Run Record | ✅ 三件套齐（真实 `model_id`、实测 token、53 字段配置快照），`error_classes` 也抓到了 |

**正例触发率全 0 = 本批最重要的发现**，而且它有两种解释，**现在还分不清**：

- 解释 A：**夹具压力不够**。模型每次都高效解掉了——`pos_stuck_unfixable` 读完测试就看出矛盾、
  压根没编辑 3 次；`pos_browse_many_modules` 只用了 4 次工具就答完 60 个模块；
  `pos_deadend_missing_tool` 试一次 npm 失败即如实报告，没重复走同一条路。
- 解释 B：**这些 detector 在称职模型下本来就很少触发**——那它们的实际价值就要重估。

两种解释的区分办法是**换更弱的模型 / 加大任务难度再测触发率**，这是 V5 的活。
但无论哪种，一个结论现在就成立：**这三个正例目前是哑仪表，读数为 0 说明不了 detector 好坏。**
批 2 设计时要把"如何真正施加压力"当成头等约束，别再造出跑得很顺的正例。

> 附带印证了正反例非对称设计是对的：若按 ADR 原文把正例也当硬断言，
> 这六个任务会报"3 个 detector 全坏了"——而实际上它们一个都没坏。

### 批 2 / 批 3（未做）

- **批 2 — 联网侧 L2**（`login_hint` / `research_hint` / `truncation_hint` 的正反例）。
  这批**天生不稳定**：要真实检索、结果随时变。ADR 决策 4 的 cassette（块 V3）落地后再做，
  否则每跑一次都在赌搜索引擎当天返回什么。**故意排在 V3 之后。**
- **批 3 — L3 复合长任务**（~10 个：多阶段、需委派、需 crazy 跑完；判分只看终局可程序化事实）。

## 块 V3 — 录制/回放：让评测进 CI

**本阶段的技术核心**，也是唯一能让评测在开发机（2 核 4G）上自测的办法。

- V3.1 在 `build_provider()` 外包一层，**不动任何 provider 实现**（`BaseProvider.stream_chat` 已是统一接口）：
  `RecordingProvider`（真跑 + 落 cassette）/ `ReplayProvider`（离线回放）。
- V3.2 cassette key = `sha1(model_id + system + messages_json + tools_json)`；图片用 blob hash 代替内容。
  存 `data/cassettes/{task}/{key}.jsonl`，一行一个 `StreamEvent`。
- V3.3 **miss 必须硬报错并指出第几步 miss，绝不静默回落真跑**——静默回落同时犯两个错：
  偷偷烧 key，以及把"我的改动让轨迹发散了"这个**最有价值的信号**当噪声吞掉。
- V3.4 L1+L2 的 replay 跑进 CI（现在 `.github/workflows/` 只有 tag 触发的 release.yml）。

**角色划分，别混用**：

| | replay | 真跑 × N |
|---|---|---|
| 用途 | **回归门** | **A/B 效果验证** |
| 能验证 | 判分器、UI、非提示词路径的改动 | 提示词、detector 阈值、换模型 |

## 块 V4 — 喂饱 Learning

前四块做完才有料可吃。新增 `scripts/eval/harvest.py`：批跑 L2+L3（`--repeat 3`）→ 写满
`failures.eval.db` → `aggregate()` → `propose()` → 候选策略报告（含证据指纹 + 样例 detail）。

然后走已设计好的生命周期：**人审 → Golden 追加语料 → approve（强制 `golden_passed=True`）→ active**。
`learning_shadow` 事件说明影子模式已有——V1 落盘后，**影子建议的采纳率与效果第一次变成可统计的**。

**验收**：`propose()` 产出 **≥2 条有真实证据**的候选。一条都产不出 = 任务集失败面不够宽 → 回 V2 补任务。

## 块 V5 — detector 阈值调优（持续）

到这一步才有资格动那些拍脑袋的数字（`threshold=2` / `max_nudges=1` / 研究催重搜全局预算 / `_PARALLEL_CAP`）。

每个 detector 出三列表：**触发率**（L2 正例中触发几个）｜**误报率**（L2 反例中误触发几个）｜
**触发后是否改善**（nudge 后那轮 `Evaluation.issues` 是否减少 / Need 是否前进——判据现成，`eval` 事件里就有）。

第三列最容易被忽略：**触发得准 ≠ 有用**。
调参方式：replay 固定模型输出 → 只改阈值 → 对比 Run Record。**这是唯一能把模型随机性
从改动效果里剥离的方法**，也是 V3 最大的回报。**任何阈值改动必须附 report.py 前后对比，
不接受"感觉更好了"。**

## 分期与成本

| 块 | 依赖 | 粗估 | 开发机可做 |
|---|---|---|---|
| V0 | — | ~1 天 | ✅ **已完成并本地验收** |
| V1 | V0 | 1~2 天 | ✅ **已完成并本地验收** |
| V2 | V1 | 3~5 天，可分批 | ⏳ **批 1/3 已完成**（写 ✅ / 真跑验 ❌）|
| V3 | V1 | 2~3 天 | 录 ❌ / 放 ✅ |
| V4 | V0,V2,V3 | ~2 天 | ✅（回放） |
| V5 | V4 | 持续 | ✅（回放） |

**V0 必须最先**——唯一一块"不做就全白做"的。V3 之后 V4/V5 全程可在开发机用回放做，
只有录 cassette 与 A/B 要搬 Windows（符合既定重活分工）。

## 明确不做（记录理由，防重复讨论）

- **不做完整事件溯源架构**：Run Record + cassette 已覆盖评测所需的回放能力。等真要做分叉/时间旅行
  再上，别为架构漂亮提前付账。
- **不自动采纳 Learning 候选**：理由 ADR 0014/0017 已立，`trajectory.py` 论证过同一件事
  （"保守则永不触发，激进则批量生成垃圾"）。`approve` 的 `golden_passed=True` 硬门不变。
  **喂饱的定义是"让 propose() 产出有证据的候选"，不是"让候选自动上线"。**
- **不让模型判分当主判据**：只在纯程序化判不了时用，且必须多数投票。
- **不追 dsh 的插件化**：本阶段没有一项需要它。

---

# 待办

> 三档：**第一档挡着定版**（不是新开发）→ **第二档是明确写过"没做"的遗留**（小而确定）→ **第三档需要拍板**（工作量大）。

## 第一档 — 验证积压 ✅ 已清空（2026-08-13）

> ⚠ **流程教训**：v3.60.x 与 v3.62.1 都走过"先定版推送、验证后补"，验完**没有回头改文档**，
> 于是 CHANGELOG/DEVLOG/ROADMAP 里长期挂着假的"未验"状态，还一度被当成"最该先清的积压"。
> **这类版本验完必须回写三处记录**，否则文档会持续说谎。

| 项 | 结果 |
|---|---|
| `scripts/diag_handoff_realrun.py` | ✅ **2026-08-13 首次真跑通过 4/4**（DeepSeek anthropic 端点）。见下方复盘 |
| ~~macOS GUI~~ | 2026-08-12 清掉：距最初的 mac 版又迭代了几十版，这条早已失去针对性；将来真要打 mac 包时重新排 |
| ~~v3.15.2 ~ v3.26.x~~ | 2026-08-12 清掉：其后多版真机验证反复覆盖同一批界面，实际风险低 |

**换手真跑复盘（值得记）**：脚本此前一直因缺 key 没跑过，首跑**失败**——模型读完文件就用散文
反问"报告系统地址是什么"，全程没调 `request_handoff`。`scripts/list_tools.py` 确认工具**给了**
（32 个里有它），所以是"给了但没选"。查下去发现是 **fixture 欠缺目标**：那个登录墙只写了
"需要登录后才能查看"，**没有任何登录入口**，而 `request_handoff(target=...)` 要求填真实目标——
**模型拒绝凭空编一个 target，恰恰是我们想要的行为**。给登录墙补上真实入口后重跑即 4/4：
`read_file` 发现墙 → `web_fetch` 试入口 → `request_handoff`（target 真实、verify 写明"重读文件
确认已从占位变为实际报告"）→ 人登录 → **又读了一次现场**才作答。ADR 0023 决策 1~3 全部生效。
**教训**：没跑过的真跑脚本，其 fixture 本身也是未验代码；"测试失败"要先分清是**被测对象**错了
还是**测试设定**错了，再动手——但也别改到通过为止，这次只改了"登录墙要有入口"这一处。

## 第二档 — 明确遗留（小而确定）

- **Windows 侧进程/管道语义的覆盖缺口**（2026-08-13 CI 首跑留下）。以下三条是**真 POSIX 专属**、
  没有语义等价的 PowerShell 写法，已显式 `if IS_WIN: return` 跳过（宁可缺，也不凑一个守着别的东西的"绿"）：
  - `test_shell.test_bg_daemon_inheriting_pipe_returns_fast` —— **缺口最大的一条**。它守的是压测揪出的
    隐藏死锁（后台子进程继承 stdout、shell 秒退，老实现等管道 EOF 白挂满 timeout）。这类 bug 在
    Windows 上重现概率更高，恰恰最该在那儿跑。要单独排一段试 Windows 等价构造（句柄继承语义不同）。
  - `test_shell.test_timeout_kills_child_tree` / `test_procs.test_long_running_stop_kills_tree` ——
    进程树终止在 Windows 上是 Job Object 那套，跟 `$!` + `wait` 不是一回事，要另写用例。
  - `test_shell.test_runaway_output_is_bounded_not_oom` —— 靠 `resource.setrlimit(RLIMIT_AS)` 证明
    "没上限就会 OOM"，Windows 没有 `resource`（等价物是 Job Object 内存限额），`yes` 也不存在。
  - `test_procs.test_write_input_submit_false_sends_no_newline` —— `read -n 1` 单键读在 PowerShell 里
    只有 `[Console]::ReadKey()`，它要真控制台、拿不到重定向管道。
  跨平台命令底座见 `tests/_shellenv.py`（新加的测试要跑真命令，走它，别再写死 bash）。


- **FR-16 T2 完整形态**：长时间无打点时的轻提示。
- ~~**失败语料无来源标记**~~ → **2026-08-19 并入第三阶段块 V0**（ADR 0027 决策 2）。原描述只看到"混"
  这一半；读码又发现另一半更隐蔽——**指纹吃了临时工作区的绝对路径**，同一失败每跑生成不同指纹，
  使 `propose()` 的双门两个方向同时失真。**来源标记解决"混"、指纹归一解决"碎"，必须一起做**，故合并。
- **Learning 策略无 GUI 审批入口**：`proposed → active` 目前只能命令行。（下游依赖块 V4——
  `propose()` 先要能产出有证据的候选，审批入口才有东西可审）
- **技能化收尾**：生成的技能只落项目级 `.hermes/skills/`，无一键装全局；无"从已有技能反向生成命令"。
- **斜杠命令 P1 缺口**：`$1 $2` 位置参数 / 子目录命名空间 / 交互式命令。
- **设置面板未做三项**（v3.57.0 时用户圈掉的）：设置内搜索、记住上次 tab / 深链接、迁原生 `<dialog>`。
- ~~**待用户拍板**：分发包 `.env` 里已失效的 `ARK_API_KEY`~~ → **2026-08-12 已删**（用户不再用方舟订阅）。
  `.env`、`pack.py` 的 `ENV_TEMPLATE`、`config.yaml` 注释、README、`diag_handoff_realrun.py` 一并去掉方舟预设；
  方舟仍留作**可选 provider 预设**之一（设置面板里填 key 即用），只是不再是默认。
  **纪律**：别在 config / `.env` / 打包模板里预填任何一家——预设用户没订阅的 provider 只会制造
  "这模型我没配过怎么会在这"的误会，且失败时报错指向假原因。

## 对标主流 agent（2026-08-13 横向对比）

对比 Claude Code 2.1.198–2.1.229 / Codex CLI / Gemini CLI 近半年的工具面迭代。结论：**hermes 已与主流
同代**，个别处更细。详见 ADR 0024。

**A 档 ✅ 已收官**（**Windows 真机验证通过、定版 v3.65.0，2026-08-13**）：A1 权限绕过对抗性回归
（修 4 类真绕过）、A2 会话级工具预算、A3 `run_<shell>` 补 `cwd`。
同版另修真机 bug：预览面板「在浏览器打开」对含中文/空格路径的已有项目无效（`as_uri()` 百分号编码）。

**B 档 ⏳ 待拍板**（列进第三档）：
- **B2 外部技能的信任边界**——`skillhub` 从 GitHub 市场装的 SKILL.md 直接成为模型上下文里的指令。
  已确认 `install_skill` 只装技能目录、不装命令，碰不到 `commands.py` 的 `mode: exec` 任意执行路径，
  内置命令也不可被同名覆盖（这两道设计对了）；剩下的是纯 prompt injection 面。
  抄 Claude Code 2.1.228：装入时 sanitize + 标注"来自不可信市场"。
- **B3 hooks 事件面扩容**——现只有 `PreToolUse`/`PostToolUse`。最值钱的三个缺口：
  `UserPromptSubmit`（每轮注入项目上下文，取代硬编码）、`Stop`（收尾自动跑测试/通知）、
  `PreCompact`（压缩前抢救关键状态）。按现有 `match_hooks` 架子扩，成本低。

**C 档 — 明确不做**（记录理由，防将来重复讨论）：
- **delegate 后台化**（原 B1，2026-08-13 降级）。我最初照 Claude Code 2.1.198 把它列成"最大的结构性
  差距"，**这是照搬 changelog 没换算到 hermes 的形态上**——同一份对标里我自己写过的判据
  （"对方上消息总线是**它的架构约束逼出来的方案，不是优点**"）原样适用于这里：
  **Claude Code 的子 agent 后台化，是单会话 CLI 的架构约束逼出来的**。hermes 没有那个约束——
  每会话独立 worker + 队列（FR-8.2b），子任务跑着可以切到别的会话干活，**主线根本没被冻住**；
  同轮多个 delegate 本来就并行（`_PARALLEL_CAP=4`）。后台化真正多出来的只是"主 agent 在等结果
  期间还能自己动手"，但它派 delegate 恰恰是因为**需要那个结论才能往下走**——要让这事有价值，
  模型得能规划出"等待期间去干另一件独立的事"，那是**规划能力**要求、不是机制缺失。
  机制建好了模型不用，就是 `trace_run`/`search_code` 那条老规律。而代价是动 loop 控制流 +
  子任务完成投递 + 取消/失败/上下文合并。**用户 2026-08-13 判断"意义不大"，同意。**
- **子 agent 嵌套**（Claude Code 支持 3 层）：保持"子 Agent 不能再委派"。嵌套的成本失控风险大于收益。
- **PTY / 持久 shell**（Codex `unified_exec`）：`procs.py` 已覆盖交互式场景大半，边际收益只剩
  "程序检测 tty 才输出进度条"。
- **跨文件 apply_patch**（Codex 单一补丁格式跨多文件）：`multi_edit` 只作用单文件，收益只是省几次调用。

---

## 第三档 — 需要拍板的大方向

0. **站外协同：等待 / 唤醒 / 重开提醒（ADR 0026，草案）**——推 tag 触发 CI 后"好了自动回来报结果、
   红了自己修"这个闭环，hermes **只缺四分之一步**：零成本等待 + 条件成立时唤醒**回到同一上下文**。
   ①②④ 都已具备（发起、说一句、醒来后自主处理），③ 完全没有：`procs.py` 是纯拉取、
   全库无调度/唤醒、`Conversation.state` 纯内存无 resume。
   **今天唯一能走通的是 `/crazy` 自驱轮询，但每轮"看好了没"要烧一次模型调用**——记在这儿
   免得后人以为完全没路。做法见 ADR 0026（挂成 `run_<shell>` 的参数而非新工具；只投事实不下结论；
   关窗口不续等、重开**先异步补查一次再带着答案**提醒，三种结局分开说）。**验收判据已写死：半个月没被用过就撤。**
   与第 1 条的 loopback 回调共用"本机监听/轮询 + 回灌同一会话"，实现时别各写一套。

1. **订阅制一键 OAuth 登录**（ChatGPT / Claude Pro，仿 loopback PKCE）——方案未定，待选：先做谁 + 实现路子。
2. **检索线剩余**（承块 H「后续」）：目标满足驱动的换源（"目标数据点连续缺席"，价格/数字类先做）；研究墙·墙钟时间上限；搜索 API Provider 化（博查/Serper/Tavily）+ query fan-out。
3. **UX Tier2 余项**：会话「运行中」状态 + 并发轻量指挥中心（子 agent 角色管理已判低 ROI 暂缓）。
4. **P5 调试第三波**：G debugger / I 二分定位（按需）。
5. **4.X 工程深度**：provider 韧性 → 诊断升级（探测外部 linter，**不内嵌 LSP**）。
6. **类人记忆深化②**：碎片规模大时按主题聚类（等真有规模）。
7. **自动更新**（分发三件套末件，ROI 低）。
8. **对标主流 B 档**（B1 已降 C 档不做，见上方「对标主流 agent」）：
   B2 外部技能 sanitize → B3 hooks 事件扩容（`UserPromptSubmit`/`Stop`/`PreCompact`）。两条都小而确定。
