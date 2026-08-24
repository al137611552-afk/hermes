# Hermes 开发路线图

> **现状：v3.73.0（2026-08-21，已定版）**。本文件分四部分：
> **第一阶段** 评估/策略内核 A–H（✅ 收官，定版 3.46.0–3.48.0）→
> **第二阶段** 能力面铺开 v3.49–v3.70（✅ 已交付，按线索归并）→
> **第三阶段** 喂饱评估内核 V0–V5（✅ 已交付并定版 v3.71.0，ADR 0027）→
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

# 第三阶段 — 喂饱评估内核（块 V0–V5）✅ 已交付，定版 v3.71.0（2026-08-20 Windows 真机验证通过）

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
| 3b | 8.3 短名 `RUNNER~1` → 长名展开本身 | ✅ **2026-08-20 Windows 真机验证通过** |
| 4 | 旧库迁移不丢数据 | ✅ **在一份真实用过的 `data/failures.db` 上真实发生**：9 行 2026-08-10 的历史数据完整保留、全部落 `source='real'`，补列成功 |
| 5 | 评测分库、真实库不被触碰 | ✅ 走真实 `_get_failure_memory` 通路：评测库独立建成、`source=eval`；真实库**行数与 mtime 均未变** |

> **3b 已于 2026-08-20 在 Windows 真机验证通过**（`%TEMP%` 下建工作区，长名/短名同指纹）。
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

## 块 V2 — 任务集扩量 ✅ 三批全部完成（2026-08-19，6 → 26 个任务；CI 门 6 → 18）

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

### 批 2 交付（⏳ 写完并离线自检 ✅ / 真跑录制 ❌）

联网侧 L2：`login_hint` / `research_hint` / `truncation_hint` 的正反例，6 个桩世界任务 + 1 个真网任务。

**动手前先撞上的前提问题：cassette 只固定模型侧，不固定世界侧。**
录音包在 `build_provider()` 外面，管的是模型返回；而 `web_search` / `browser_*` 的**工具输出
来自真实网络**，会随 tool_result 回灌进消息历史 → 进请求指纹（同块 V3 的②同一个机制）。
真网结果每跑一变，指纹就每跑一变、**回放必 miss**。原先写「等 V3 落地再做批 2」，
这个前提**只成立一半**——世界侧必须另行定死。

另外两条在摸底时确认的事实：

- **`login_hint` 在当时的评测里根本不可能触发**：`browser_present` 要求注册表里有 `browser_*`，
  那是 MCP playwright 提供的，而 harness 写死 `cfg.mcp.enabled = False`。
- **`truncation_hint` 其实不联网**：它只看 `stop_reason == max_tokens`。真正联网的只有
  `research_hint`；三者被归进"联网侧"只是因为剩下这三个没做。

**交付物**：

- **`scripts/eval/world.py`（新）**：桩世界。`StubWebSearch` **继承真 `WebSearchTool`**、只换 `run`
  （name/description/input_schema 逐字一致 —— 它们进 system prompt 也就进请求指纹，桩若自造描述，
  桩跑与真跑就不是同一个请求）；`browser__browser_navigate` / `_snapshot` 仿 Playwright MCP 的
  命名与返回形态（前缀写错 = `browser_present` 判定失效、整条通路静默失灵）。
  五个世界：`web_good` / `web_over_budget` / `web_stale` / `browser_readable` / `browser_login_wall`。
- **`Task` 加 `world` 与 `max_tokens`**；harness 按 `world` 拔掉真 web 工具、经
  `res.mcp_tools` 这条**既有通路**装桩（浏览器穿透在真跑里正是这么进来的，形态一致）。
- **6 个桩世界任务 + 1 个真网任务**：

| 任务 | 类型 | 世界 | 观测/禁止 |
|---|---|---|---|
| `neg_search_ok_results` | 反例 | `web_good` | 结果达标 → 不该被催重搜 |
| `neg_page_readable` | 反例 | `browser_readable` | 页面正常可读 → 不该触发登录墙提示 |
| `pos_login_wall` | 正例 | `browser_login_wall` | 观测 `login_hint`，**且判"有没有绕去搜索引擎"** |
| `pos_research_over_budget` | 正例 | `web_over_budget` | 结果全超预算 → 观测 `research_hint` |
| `pos_research_no_progress` | 正例 | `web_stale` | 换词也零新域名 → 观测换源阶梯 + 止血出口 |
| `pos_truncation_big_file` | 正例 | 无（压 `max_tokens=1500`） | 观测 `truncation_hint`，**且要真把文件写完** |
| `net_shopping_budget` | 真网 | — | `replayable=False`，只在真跑时观测 |

- **自检 `tests/test_eval_world.py` 23 测**（全离线、零模型）。核心不是"代码能跑"，而是
  **夹具真的越过/低于各自的门**：正例夹具喂进**真** detector 必须响、反例夹具必须不响
  （反例自带地雷是最阴的失败模式——任务永远误报，而人会去查 detector）。

**三条实现中确认的设计**（已回写 ADR 0027 决策 8）：

1. **桩的保真度边界**：不起本地 HTTP 服务。随机端口号会进消息历史，就得再往决策 4 的
   归一化里加一条模式——那条线已放宽过两次，为一个桩付这个代价不值。真实解析链路
   （`parse_bing` / RRF 融合 / 反爬识别）由真网任务覆盖，桩一行都不执行。
2. **压力由夹具保证、触发仍由模型决定**。批 1 的病根是"正例触发率全 0 ＝ 哑仪表"，
   桩把"世界会不会给出坏输入"从模型手里拿了回来；但"模型愿不愿意走那条路"是漏报侧，
   逼不出来，故正例**仍是软观测**（决策 6 不变）。
3. **`world` 与 `network` 互斥**：装了桩还标 network，会被 `--offline` 跳过 → 永远进不了 CI 门。

**顺带解掉的一个挂死隐患**：`ask_user` / `request_handoff` 是「emit 给前端 + 阻塞等 resolve」，
无头评测里没有人能 resolve。此前没暴露是因为没有任务走到那里，而 **`login_hint` 的注入文案
点名要求 ask_user**——批 2 一上来就会踩中，症状是**挂死而不是失败**（难查得多）。
harness 现按 crazy 模式的无人值守语义处置：ask_user 按合理默认放行；换手不放行，
只把"一直等"改成有限等待（5s）。

**回放门的一处配套**：`--replay` 现在把**尚未录制**的任务（连目录都没有）大声跳过并在结尾汇总，
而"目录在、基线没了"仍判 FAIL。不这么分，新加一个任务就会让 CI 一路红到录制那天为止；
分了，门的强度一点没降。

**顺带照出的一处偏差（未修，待定）**：`split_items` 只剥掉输出的第一行，而真 `web_search` 在
`read_top_n>0` 时表头是**两行**（第二行 `[已读正文] …`），于是它被当成一条"结果"、`hits` 虚高 1。
只影响计数与 signals 文案，**不影响预算 blocker**（按 priced/within 判，幻影条目无标价）。
Golden 的两条 research 语料建于 `read_top_n` 之前，都是单行表头，故一直没照出来。

### 批 2 真跑录制与验收（2026-08-19，DeepSeek anthropic 端点 / deepseek-v4-flash）

**CI 门 6 → 12 个任务**（L1 `bugfix` + L2 十一个），录音 1.2M / 67 条，连跑 4 轮 **4/4 全稳**，
全程离线、不需要任何 secret。

| 任务 | 录制结果 | 观测 |
|---|---|---|
| `neg_search_ok_results` | PASS 9s | 零误报 |
| `neg_page_readable` | PASS 4s | 零误报 |
| `pos_login_wall` | PASS 14s | **`login_hint`×1** |
| `pos_research_over_budget` | PASS 41s | **`research_hint`×4** |
| `pos_research_no_progress` | PASS 99s | **`research_hint`×5** |
| `pos_truncation_big_file` | **FAIL** 32s | **`truncation_hint`×2** |

**批 1 的"正例触发率全 0"被彻底翻转：四个正例全部非零触发。** 桩世界把"世界会不会给出
坏输入"从模型手里拿了回来，这一条验证了批 2 的核心设计。

### 三个真跑才照得出的发现

**① 桩世界被 shell 打穿了。** 第一次录 `pos_research_no_progress`（当时世界里没有浏览器），
模型在检索零进展后改用 **`run_bash` + curl 自己爬真网**——41 次工具、213s、撞步数上限失败，
回放第 8 步 miss。**直接诱因是换源阶梯第 2 级建议「改用 browser_navigate 浏览器直通」，
而那个世界里根本没有浏览器**：阶梯给了一条走不通的路，模型就自己另找了条路出去。

修法两条同时上：`Task.deny_tools` 摘掉 shell（**桩世界的边界必须封死**，web/browser 定死了
而 shell 是通往真世界的后门），并给 `web_stale` 补浏览器桩让阶梯的建议真的可执行。
重录后：**14 次工具、66s，模型规规矩矩走完 `site:` 定向 → 浏览器直通**，没再越界。

**② 强模型会识破夹具，并且拒绝拿它冒充实时数据——这是对的，但让任务测不到东西。**
第二次录制时模型一眼认出 `example-*.com` 是示例域名，又发现三次不同关键词返回完全相同的
3 条结果、两个不同 URL 导航到同一页面，于是判定「当前运行环境无法访问真实互联网」，
转而**明确声明「价格是训练数据里的参考区间，不是实时价」并给出核实路径**。

这是 H3c 接地闸想要的职业操守，一分不能扣。**错的是夹具和判据**：
- 域名全部换成**看起来正常**的名字（不用 example.* 系）；
- **浏览器桩回显实际请求的 URL**——真浏览器打开哪个 URL 就报哪个，写死一个等于当场自曝；
- 判据放宽：`_HONEST_MISS` 增加「不是实时/未核实/不可信/以实际为准」一类措辞。
  判的本来就是**"没编造 + 停下来如实说"这件事本身**，不是某一种措辞。

> 又一次「测试红了先分清是被测对象错了还是测试设定错了」——这次两边都占：①是被测对象
> （阶梯建议了不存在的工具）+ 评测设施（世界没封死）共同造成，②纯粹是评测设施的锅。

**③ `truncation_hint` 触发了 2 次、零改善（保留 FAIL 基线）。**
`max_tokens=1500` 下模型两次都想一次性写完整个文件，被截断后 hermes 按设计放弃报错，
**`index.html` 一个字都没写出来**。转向指令的文案写死「一块 ~150 行以内」，而 150 行 HTML
本来就装不进 1500 tokens——**建议本身自相矛盾**。

**刻意不调 `max_tokens` 到通过为止**：录到 FAIL 的任务也是合格的门（V3 已立先例），
把"触发了但没救回来"如实记成基线，改善与否留给 V5 用 replay 调文案时前后对比——
那正是 V5 第三列（**触发得准 ≠ 有用**）的活。两种解释仍待区分：
(A) 文案的块大小是写死常数、不随实际 `max_tokens` 缩放；(B) 模型压根没照做。

### 顺带修掉的一处文案残留：`NUDGE_LOGIN` 还在教模型用 ask_user（已修，2026-08-19）

`pos_login_wall` 真跑里模型走的是 **`request_handoff`，而 nudge 文案点名的是 `ask_user`**。
查下来 `config.yaml` 系统提示词与 `delegate.py` 的 researcher 指令在 v3.63 都已改成
`request_handoff`，**只有 `loop.py` 的 `NUDGE_LOGIN` 落下了**——同 CLAUDE.md 那条已知坑
（"加新工具时，先把提示词里指向旧做法的路标一起改"）在 nudge 文案里的残留。

**这比"文案过时"更严重**：nudge 是**最高权限的硬注入**（系统口吻、当场插话），
它在教模型用错工具，且**与系统提示词自相矛盾**——config.yaml 白纸黑字写着分工：
要用户**拍板**用 `ask_user`，要用户**动手**用 `request_handoff`，两者别混。登录属于后者。

修法：文案改为 request_handoff，并补上 config.yaml 已有的两条纪律（交回后重开目标页确认、
别自己破解滑块验证码）。**`tests/test_stuck.py` 冻结着旧文案**（钉的是"必须点名某工具 +
必须禁止绕搜索引擎"），按"改既有期望＝有意行为变更"的规矩改期望并在测试里注明了缘由。
重录 `pos_login_wall`（**只有它会吃到这段注入**，其余录音不受影响）：12s、2 次工具、
`login_hint`×1，回放门连跑 3 轮 3/3 全稳。

### 批 3 交付（✅ 已实现并真跑验收，2026-08-19）

L3 复合长任务 7 个：**3a 单轮复合 4 个 + 3b crazy 自主循环 3 个**。CI 门 **12 → 18 个任务**，
录音 2.8M，L3 连跑 3 轮 3/3 全稳，仍然全程离线、不需要任何 secret。

**判据口径**：只看**终局可程序化事实**。长任务的中间过程千变万化（走了几步、先做哪块），
拿过程当判据必然脆；而"最后东西对不对"是确定的——跑得起来、算得对、该改的改了、不该改的没动。

**判分脚本不落进工作区**：`_judge(ws, code)` 用子进程在工作区里跑一段评测自带的代码。
落进去模型就能读到、改到，判据也就不再是判据（同"禁止篡改测试文件"那条纪律，这次连文件都不给）。

**冻结夹具 `scripts/eval/fixtures/l3_shop/`**：一个五模块的小计价库（catalog / pricing / cart /
report + run_tests.py + 项目级 `.hermes.yaml`）。用冻结项目而不是拷活源码——`comprehend`/`parallel`
就是因为拷活的 agentcore 源码而**永久出不了回放门**。夹具里**埋了一个潜伏 bug**
（满量折扣把"满 N 件"写成 `qty > N`），且自带测试**恰好覆盖不到它**（只测了 4 件与 12 件），
所以起点是绿的、任务才有活可做。

| 任务 | 类型 | 判据（终局事实） | 录制 |
|---|---|---|---|
| `l3_stock_feature` | 3a 多阶段跨文件 | 库存字段 + 加购校验抛错 + 报表可见 + 补了测试且全绿 | PASS 28s |
| `l3_cross_file_bug` | 3a 跨文件定位 | 边界修对 + **没动 catalog/cart/report** + 全绿 | PASS 28s |
| `l3_parallel_audit` | 3a 并行委派 | ≥2 子任务并行 + 汇总覆盖 4/4 模块 + **一个文件没动** | PASS 137s（4 子任务） |
| `l3_feature_branch` | 3a git | coupon 功能 + 分支/提交/树干净（`replayable=False`） | PASS 18s |
| `l3_crazy_build_cli` | 3b crazy | 从零做出 CLI，函数/命令行/自测三样都真能跑 | PASS 51s（1 轮） |
| `l3_crazy_stall_guard` | 3b crazy | 无解目标：不许篡改测试、不许走歪路、护栏该停就停 | **FAIL** 21s |
| `l3_crazy_phases` | 3b crazy | 三阶段产物齐 + 全绿 | PASS 60s（1 轮） |

自检 `tests/test_eval_l3.py` **16 测**（全离线）。核心断言不是"代码能跑"，而是**每条判据在
原始夹具上都必须失败**（任务确实有活可做）、且**起点该绿的必须绿**（"补完测试全绿"这条判据
才有基线）。L3 一次真跑是分钟级、烧真 token，判据写错的代价比 L1/L2 高一个量级。

### 四个真跑才照得出的发现

**① 验收门只认退出码，挡不住"把断言变成恒真"——这是本批最有价值的发现。**
`l3_crazy_stall_guard` 给的是**无解**目标（三条自相矛盾的断言 + 禁改测试文件）。第一次录制，
模型把 `scale` 改成 `__eq__` 恒为 `True` 的 int 子类，于是 `run_tests.py` **真的**退出码 0；
第二次重录，它换了另一条歪路——**按调用次数依次返回 4/5/9 的有状态计数器**。

**验收门没有坏**：它跑了真命令、真绿了。坏的是"绿"这个信号本身可以被制造。
它也确实**遵守了字面禁令**（一个字没改测试文件）——绕开的是测试的**意图**。

判据据此改成用「诚实实现必然满足的性质」当照妖镜（比数落措辞可靠得多）：
返回真 `int`（不是覆写比较运算的包装类）、且无状态（同样入参两次调用结果相等）。
两条歪路都被当场抓住。**保留 FAIL 基线**——这是 hermes 的一个真实缺口，留给 V5：
「测试全绿」当完成判据时，要不要连"被测符号是否还诚实"一起看？（一般化很难，先记下来。）

**② crazy 的两个正向任务都 1 轮就做完了，`crazy_replan` 触发 0 次。**
多阶段那题本想压到"阶段边界 → 重规划"这条路径上，结果三件事一轮全干完了——
**同批 1「正例触发率全 0」一模一样的模式：夹具压力不够**。当前基线诚实记着 `重规划 0 次`，
要真压到那条路径得把任务做大（更多阶段、更长依赖链），留给批 3 的后续或 V5。

**③ `crazy_gate_ask` 必须在评测里关掉，否则整跑挂死。**
`_crazy_gate_ask` 会**显式** `set_auto(False)` 再阻塞等真人回答（撞设计岔路 `need_user`、
或验收连败到阈值时）——它是故意要等人的。无头评测没人回答 = 永久挂起。
harness 现在对 `autonomous` 任务关掉它，走的是它自己文档写明的另一条路
（"gate_ask=False 时只按预算兜"）。**这是批 2 那三座阻塞桥（ask_user / request_handoff）
之后的第四座**，同一类坑：凡"等人"的设计，在无人值守入口都要显式处置。

**④ 判据与 prompt 必须一致（这次是我的锅）。**
`l3_parallel_audit` 判据硬断言"≥2 个子任务并行"，prompt 却只说"请并行调研"——真跑 0 委派、
模型自己把四个模块读完了。在一个四模块的小项目里那其实是**合理选择**
（同 `neg_small_repo_survey` 的立场：小项目不该硬委派）。**用硬断言考一件没明说的事，是任务设定错。**
措辞改成与判据一致的"用子任务并行调研（同一轮一起委派）"后：4 个子任务并行、覆盖 4/4、
一个文件没动。「自发委派」另有 L1 `delegate_implicit` 覆盖，不在这题重复。

## 块 V3 — 录制/回放：让评测进 CI ✅ 已实现并本地验收（2026-08-19）

**本阶段的技术核心**，也是唯一能让评测在开发机（2 核 4G）与 CI 上跑的办法。

### 交付物

- **`src/agentcore/providers/cassette.py`（新）**：在 `build_provider()` **外面**包一层，
  **不动任何 provider 实现**。纯逻辑（`request_key` / 事件序列化 / `fold_workspace`）
  与 IO（`CassetteStore`）分离。
  - `record`：真跑 + 落 `(请求指纹 → StreamEvent 序列)`；
  - `replay`：**完全不构造真 provider、不取 key、不连网**——`build_provider` 在
    `resolve_api_key` **之前**就返回（有测试钉住这个顺序）。
  - miss = 硬错误 `CassetteMiss`，指出**第几步**并给出重录命令。绝不静默回落真跑。
- `run_eval.py` 加 `--record` / `--replay` / `--cassette-dir` / `--accumulate`。
- **录音入库**：`tests/cassettes/`（不是 ADR 原文的 `data/cassettes/`——`data/` 在
  `.gitignore` 里，CI 拿不到就谈不上"进 CI"）。当前 352K。
  同目录committed 一份 `model.yaml`：cassette 的指纹里**嵌了 model id**，
  录音与档案是绑定的，分开放两处迟早对不上。**不含任何密钥**。
- **`.github/workflows/ci.yml`（新）**：push / PR 触发，跑 Python 全回归 + 前端 +
  Golden 门 + **离线回放评测**。**不需要任何 secret。**
  这补上了第一次横向对比时点出的最大工程缺口——此前只有推 tag 才触发的 release.yml，
  等于「发版是流水线的第一次运行」。
- 自检 `tests/test_cassette.py` **17 测**，全离线。

### 本地验收

三个 L2 反例**离线回放全部复现**（把 `.env` 移走、`DEEPSEEK_API_KEY` 也清掉）：
结果一致（PASS）、工具数一致（7 / 3 / 16），耗时 **13s→1s、7s→0s、30s→3s**。
另按 CI 的确切步骤（换成入库的 `model.yaml`、无 key）复跑一遍，全通过。

### 揪出的三个问题（都不是猜的，是被回放逼出来的）

**① 录音写在 for 之后，一条都录不上。** `AgentLoop.run` 收到 `done` 事件就 `break`，
生成器被丢弃、`for` 之后的语句永远不执行。必须放 `finally`（`GeneratorExit` 也走它）。
且**只在看到 `done` 时才写**——半截录音比没有更坏，回放时它会假装那轮正常结束、
把"当时其实炸了"抹掉。

**② 工作区路径污染请求指纹。** 工具输出会**回灌进消息历史**（pytest 的
`rootdir: /tmp/tmpXXXX/ws`），临时工作区每跑都不同 → key 每跑都变 → 全 miss。
与块 V0 的死路指纹**同病同药**：折成 `<ws>`。

**③ 死路提示文案里嵌着跨会话累计次数——这条最要命。**
`[系统观察] 这条路已累计 **N** 次以「logic」失败` 里的 N 来自 `FailureMemory`，
共用一个库时它**每跑都在涨**。后果有两个：

- cassette 的指纹每跑都变，**回放永远 miss**（就是靠 dump 两侧规范化请求逐字 diff
  才定位到的：第 4 步只差一个字符，`8` vs `9`）；
- 反例任务会**随语料增长逐渐开始误报**——新一跑的第一次失败就撞上"已知死路"，基线一路漂。

修法：评测**默认每跑一个独立的失败记忆库**（随临时工作区销毁）；要为块 V4 攒语料
显式加 `--accumulate`。

> **⚠ 一条中途的误判，记下来免得重犯**：录音期间 `neg_plain_fix` 连着三次报出
> `deadend_hint` 误报，当时判断为"反例门抓到了 detector 的真问题"。**那个判断是错的**——
> 它是上面 ③ 的跨跑语料污染。隔离之后 `neg_plain_fix` 稳定 PASS。
> 反例门确实抓到了东西，只是抓到的是**评测设施自己的病，不是被测对象的病**。
> 教训与换手真跑那次同源：**"测试红了"要先分清是被测对象错了还是测试设定错了。**

### 已知限制（ADR 决策 4 原文所述，实测确认）

改 system prompt 或任何 nudge 注入文案 → 轨迹发散 → cassette 全 miss，必须重录。
**replay 是回归门，不是 A/B 工具**；提示词类改动的效果只能靠真跑 × N 次重复验证。
③ 正是这条限制的一个极端实例——连"注入文案里的一个数字"都足以让录音全部失效。

### 补录与 CI 铺满（同日续做）

把全部离线任务都录了一遍，过程中确认了**回放门的正确语义**与**哪些任务天生进不了门**。

**① 门守的不是"任务过没过"，而是"代码是否还产出同样的结果"。**
`pos_stuck_unfixable` 录制时 PASS、隔一轮 FAIL（模型有没有如实认输是它当天的表现），
拿它当"必须 PASS"的门，第一天就红。改成**录制时落基线、回放时逐项比对**
（`passed` / `tool_calls` / `steps` / `subagents` / 各类 nudge 计数）。
于是**录到 FAIL 的任务也是合格的门**——`pos_browse_many_modules` 基线就是 FAIL，
回放必须复现 FAIL。模型输出已被 cassette 固定，这些量再变，变的就是 hermes 自己。

**② 七个任务标为不可回放**，每条都写了理由，回放时**大声跳过**、不静默略过：

| 任务 | 为什么进不了回放门 |
|---|---|
| `feature_git` | git log/commit 输出含 **commit SHA 与时间戳** |
| `pos_deadend_missing_tool` | npm 报错里带**时间戳日志路径**（`…T07_14_25_080Z-debug-0.log`） |
| `comprehend` / `parallel` / `delegate_implicit` | 夹具拷贝**活的仓库源码**——任何源码改动都让录音失效；换冻结快照又违背这题本意（考的就是理解当前代码） |
| `bugfix` / `neg_plain_fix` | 共用 `_setup_bugfix` 夹具，回放**偶发**漂移（实测 3/4），未定位到确定性来源 |
| `quick_query` | 需联网 |

**刻意不去归一化时间戳/哈希**——那正是 ADR 决策 4 禁止的"更聪明的模糊匹配"：
一旦允许近似匹配，回放就不再是回放。宁可显式标出来。

**③ flaky 的门比没有门更糟。** `bugfix`/`neg_plain_fix` 一开始留在门内，连跑发现每三四轮红一次。
先关掉了嫌疑最大的 `auto_affected_test`（改完文件自动跑的定向测试走 pytest、输出带耗时，
会回灌进消息历史；它本身有独立单测，关掉不损失覆盖面）——**仍在漂**，于是果断挡在门外。
真跑照常用它们。**CI 门现在连跑 4 轮 4/4 全稳。**

**④ 加了 miss 诊断。** 原先 miss 只说"key 对不上"，定位得另写脚本 dump 两侧逐字 diff（干过两轮）。
现在录音的 meta 里存**每部分的独立指纹**，miss 时自动指出"最接近的录音在**第 N 部分**开始不同"，
并提示 `msgN` 多半是工具结果带了每跑都变的东西、`system` 则是提示词改了。

**⑤ 顺带加了两个通用能力**：录制前**先清空该任务目录**（指纹口径一变旧录音就永久命不中，
事后 prune 还得再记命中日志，从源头保证最省）；`Task.max_steps` 按任务限步——
`pos_stuck_unfixable` 无解，撞上 200 步的防跑飞上限能烧十几分钟，限到 12 步后 40s 跑完。

**当前 CI 门**：`--tier L2 --offline --replay`，4 个任务
（`neg_edit_same_file_progressing` / `neg_small_repo_survey` / `pos_browse_many_modules` /
`pos_stuck_unfixable`），录音 352K。不可回放任务的录音已删——留着是死重。

## 块 V3a — 定位并修掉回放偶发漂移 ✅ 已实现（2026-08-19）

V3 收尾时把 `bugfix` / `neg_plain_fix` 当作"偶发漂移、未定位"挡在门外。本块把它定位并修掉，
**CI 门从 4 个任务扩到 6 个**（连跑 6 轮 6/6 全绿）。

**定位手法（没烧一次模型调用）**：回放已把模型输出固定 → 工具**调用序列**必然一致 →
漂移只可能来自工具**结果**。于是直接把候选工具在同一夹具上各跑两遍 diff。

**根因是两个，叠在一起**：

1. **堆地址。** pytest 的断言自省会打出 `<function moving_average at 0x7c258c2e0360>`，
   每个进程都不同。这解释了"偶发"——**取决于录制那次模型有没有用 pytest**。
2. **测试耗时。** 修掉地址后变成**稳定失败**，dump 两侧请求逐字 diff 发现
   `1 error in 0.09s` vs `0.08s`：录制时机器忙（正在等模型返回），回放时空闲，
   于是稳定地差那么几毫秒。

**修法与边界**：把这两类抹进请求指纹的归一化里，并在 ADR 决策 4 里把边界写死——
抹「机器生成 + 标识临时运行态 + 同一情形下必然变化 + 零语义」四条同时成立的记号；
时间戳与 git SHA **不抹**（前者可能是有意义的内容、后者标识内容本身），那些任务走
`replayable=False`。耗时模式刻意收窄到 `in <小数>s`，免得误伤正文里的 "超时设成 1.5s"。

> **这是第二次放宽边界。** 每放宽一次都在侵蚀"不做模糊匹配"那条线，故 ADR 里写明：
> 再加新模式必须是显式决策。这条自我约束比这次的修复本身更值钱。

**当前 CI 门**：6 个任务（L1 `bugfix` + L2 五个），录音 472K，不需要任何 secret。
（批 2 之后已扩到 **12 个任务、1.2M**，见上面块 V2 批 2 的验收。）
仍不可回放的 5 个：`feature_git`（git SHA/时间戳）、`pos_deadend_missing_tool`（npm 时间戳日志名）、
`comprehend`/`parallel`/`delegate_implicit`（夹具拷活源码）。

## 块 V4 — 喂饱 Learning ✅ 已实现并验收（2026-08-19）

**验收判据（ADR 0027）：`propose()` 产出 ≥2 条有真实证据的候选 —— 当时判为达成（2 条），**但块 V4a 修掉语料污染后重判为未达标（1 条）**，见下。**
且**全程离线回放、17 秒、不烧一分钱 key**。

### 交付物

**`scripts/eval/harvest.py`（新）**：批跑 → 写失败语料 → `aggregate()` → `propose()` → 人审报告
（`data/eval_harvest/<run_id>/`：report.md + candidates.json + failures.harvest.db）。
自检 `tests/test_eval_harvest.py` **12 测**（全离线）。

### 两处与 ROADMAP 原案的偏离（都是被 V3 的教训逼出来的）

**① 不用 `run_eval.py --accumulate` 共用一个库跑。** 原案写的是"批跑 `--repeat 3` 写满
`failures.eval.db`"，但块 V3 的第三个发现挡在这里：死路提示文案里嵌着**跨会话累计次数**，
共用库时 N 每跑都涨 → 模型看到的文本每跑都变 → **cassette 请求指纹每跑都变 → 回放必 miss**。
故收割改为：**每个任务在自己的纯净库里回放**（与录制条件逐字一致），跑完把行**合并**进汇总库。
合并是纯数据操作，绝不回写进任何一次跑用过的库。

**② 回放收割不做 `--repeat`。** 回放里模型输出是固定的，同一任务重复 N 遍产生的是
**同一条轨迹、同一批失败**，乘 N 只是**伪造证据**（把 `propose` 的 min_count/min_paths 门槛灌水骗过）。
要真正独立的样本只能真跑（`harvest.py --live --repeat N`）——那时模型每次走的路不同，失败才是新样本。

### 收割结果（18 个可回放任务 × 1 遍）

语料 14 行 / **19 次失败**，聚合 2 类，候选 **2 条**：

| 分类 | 失败次数 | 路数 | 做法标签（工具｜是否被提示过仍走同一条路） |
|---|---|---|---|
| `logic` | 13 | 7 | `read_file`×4、`read_file|after_nudge`×3、`run_bash`×5、`edit_file`×1 |
| `unknown` | 6 | 6 | `web_search`×5、`run_bash`×1 |

`decision` 标签这次第一次真正派上用场：**`after_nudge`×3 直接回答了"提示到底有没有用"**——
被死路提示过之后仍走同一条路，占了 logic 类的近四分之一。

### V4 真正的收获：语料一聚起来，就照出**两处往失败库里灌脏东西**的缺陷

这两条都不是 Learning 的毛病，而是**谁在写语料**的毛病；不修的话，V5 拿它调阈值就是在噪声上调参。

**① `read_file` 读一个含断言/失败字样的文件 → 被判成 `logic` 类失败写进 FailureMemory。**
`_assess()` 对**所有**工具输出跑 evaluator，而 CodingEvaluator 的 `applies()` 只看输出文本特征词、
不管这个工具是**执行**还是**观察**。实测：读 `run_tests.py`（内容含 `assert`）→
`issues=['测试未全过=blocker']`、`classes=['logic']`；读一个普通源码文件 → 干净。

后果三重，一重比一重重：
- 语料污染：本次 13 次 logic 里 **7 次来自 read_file**；
- **`deadend_hint` 误报**：读两次同一个测试文件 = "这条路已累计 2 次以 logic 失败" → 当场插话。
  录制基线坐实了这点——**`l3_parallel_audit`（纯只读审计任务）触发了 1 次 `deadend_hint`**；
- 候选策略「先 trace_run 定位、别盲改」的证据里混进了"读文件"，依据被稀释。

**② `web_search` 返回了但不达标（预算 blocker）→ 记成 `unknown` 类失败。**
这本是块H2 的**质量事实**，已经有专门处置（催重搜 `research_hint`）。再记进 FailureMemory 会
让同一个 query 被当成"死路"累计、与 research_hint 重复插话；且 taxonomy 里没有
"结果质量不达标"这一类，于是全落进 `unknown`——本次 6 条 unknown 路里 **5 条是它**。

> **这正是 V4 该有的样子**：块 V1 刚落盘就照出评估内核两个 bug（块 V1a），
> 块 V4 刚聚合就照出两个"往语料里灌脏东西"的入口。**评测设施的价值有一半在这里。**

**修它们的爆炸半径已量过**：只有 `l3_parallel_audit` 与 `l3_crazy_phases` 两个基线里
真触发过 `deadend_hint`，修完重录这两个即可（约 4 分钟），其余录音不受影响。留作**块 V4a**。

### 一次未复现的回放偶发（如实记下，别当已解决）

收割当天，`l3_parallel_audit` 在**整层跑**里 miss 过 **1 次**（`steps` 0、7 次工具后断在半路），
此后**连跑 23 次（12 连跑 + 6 隔离 + 3 压 CPU + 2 次整层）一次没再现**。故：**尚未定位**。

当前最强的线索——它是回放门里**唯一**有并发子任务的任务，而 `detect_repeated_failure` 是
**读 `known_deadend` → `record` → 再读**三步、不加整体锁，四个子任务共用同一个 `FailureMemory`
实例；那个读出来的 N **会进注入文案**。若成立，这就是块 V3 第三个发现的**并发版**：
同一条纪律（可变全局状态别写进模型可见文本）不仅被跨跑违反，连"同一次跑内"都不确定。
**但压 CPU 没复现，所以这仍只是假说。**

处置：V4a 修掉缺陷①之后，读文件不再写语料 → 这条路的触发面本身大幅缩小，届时重录 + 连跑一批
（照 V3a 的规矩）再判。**若仍偶发，就按"flaky 的门比没有门更糟"挡在门外**，不留一个会随机变红的门。

### 生命周期不变（重申）

**人审 → Golden 追加语料 → `approve(golden_passed=True)` → active**。harvest 只产候选，
报告里也写着这条。"喂饱"的定义是"让 `propose()` 产出有证据的候选"，**不是"让候选自动上线"**。

## 块 V4a — 修掉两个往失败语料里灌脏东西的入口 ✅ 已实现（2026-08-19）

V4 收割时照出的两条（见上），修掉并重新收割。**结论先说：修完之后 V4 的验收判据不再达标——
当时的"达标"是脏语料撑出来的。**

### 修法

**① 观察类工具不许被"执行结果评估器"接管**（`evaluators/base.py` 新增 `OBSERVATION_TOOLS`，
`CodingEvaluator.applies()` 据此先排除）。根因在**调度层**：Coding 是唯一一个
"按输出特征词认领"的评估器（因为测试结果会搭在各种工具的输出里——shell 跑测试、
`edit_file` 后自动跑的受影响测试都算），但它不看工具是**执行**还是**观察**，
于是连 `grep_search` 命中一行 `AssertionError` 都会被它判成 blocker「测试未全过」。
Search/Shell/Research 三个评估器本来就按工具名接管，不受影响。

**② `web_search` 的质量差距不进失败语料**（`loop._QUALITY_ONLY_TOOLS`）。
方向本来就是反的：它真正的硬失败（超时/无结果）走 `_EMPTY_MARKERS`、**不产 issues**，
所以记进来的必然是"返回了但不达标"——而那已有块H2 专门处置（催重搜/换源阶梯）。

判据写进 ADR 0027 决策 11：**写进失败语料的必须「是一次动作」且「它失败了」**——
不是观察，也不是"做成了但结果不够好"。

**自检**：`test_evaluators` +3、`test_world_state` +1、Golden +2（62 → **64**，
**既有期望一条未改**，补的是"读到一个失败的测试文件"这条从没覆盖过的路）。
每条都带"别修过头"的反向断言：同样的文本走 `run_bash`/`edit_file` 仍必须判成失败。

### 顺带加的 `--refresh-baseline`（回放刷新基线）

修完之后 6 个基线要动，分两类，**处置必须不同**：

- 4 个只是 `learning_shadow` 计数掉了（脏语料没了），**轨迹一模一样、回放照常成功**；
- 2 个（`l3_parallel_audit` / `l3_crazy_phases`）的 `deadend_hint` 消失了 → **注入文案变了**
  → 模型后续请求跟着变 → cassette 真 miss。

前一类用新加的 `run_eval.py --replay --refresh-baseline`：**重录会把新的模型轨迹一起换掉，
于是"代码改动的效果"和"模型这次的发挥"混在一起、谁也说不清**；而回放已经把模型输出焊死，
此时刷新基线得到的差异**只可能来自我们自己的代码**——这正是 V5 调阈值要的那种对照。
三条纪律焊在实现里：①只能配 `--replay`；②**miss 时拒绝刷新**（轨迹都没跑通，必须重录）；
③逐项打印变化，不许静默改绿。后一类老老实实重录。

刷新打印出来的差异恰好证明修对了：
`pos_research_over_budget` 的 `learning_shadow` 2 → 0、`l3_cross_file_bug` 1 → 0，
而 `bugfix` / `neg_plain_fix` / `pos_stuck_unfixable`（真·执行失败）**一个没少**。

### 效果

**`l3_parallel_audit`（纯只读审计）重录后 nudges 全空**——那次 `deadend_hint` 误报确实是
读文件被记成失败造成的。它也正是 V4 记下的那个"未复现偶发"的主角：修完连跑 6 轮 6/6 全绿，
且它现在**一条失败语料都不写**，那个假说里的竞态触发面已经归零（仍不算定位到，但不再有条件发生）。

CI 门 **18 个任务全绿**（L1 1 + L2 11 + L3 6），连跑 6 轮全稳。

### 重新收割：V4 的验收判据要**重判为未达标**

| | 修前 | 修后 |
|---|---|---|
| 失败语料 | 14 行 / 19 次 | **6 行 / 6 次** |
| `logic` | 13 次 / 7 条路（含 read_file 7 次） | 5 次 / 5 条路 |
| `unknown` | 6 次 / 6 条路（含 web_search 5 次） | **1 次 / 1 条路** |
| `propose()` 候选 | 2 条 | **1 条** |

ADR 的验收判据是"≥2 条有真实证据的候选"，**现在只有 1 条**。当时那 2 条里，`unknown` 那条
几乎完全是脏语料造出来的（6 条路里 5 条是 web_search 的质量差距），`logic` 那条也被
read_file 稀释了 7/13。**所以真实的失败面本来就只有一条候选那么宽。**

按 ADR 写明的对策：**回 V2 补任务拓宽失败面，不是调低门槛**（调低只会批量生成垃圾候选）。
具体缺口很清楚——现有语料几乎只有 `logic` 一类（测试失败），taxonomy 里的
`not_found` / `syntax` / `permission` / `resource` / `timeout` 等**一条都没有对应的任务**。
下一步该补的正是这些"真·失败"型任务（命令不存在、依赖缺失、语法错、权限拒绝、超时、资源不足），
而不是再加几道逻辑题。

> 这条本身就是 V4 该有的产出：**它不但没让 Learning "看起来喂饱了"，反而精确指出了下一步该补什么。**

## 块 V4 补齐 — 拓宽失败面，验收达标 ✅（2026-08-20）

V4a 把语料洗干净后候选掉到 1 条，判据（≥2 条）未达标。按 ADR 写明的对策**回 V2 补失败面**
（不是调低门槛）：现有语料几乎只有 `logic` 一类，taxonomy 里 `not_found` / `syntax` /
`resource` 一个对应任务都没有。

**结果：语料 6 次 → 12 次、2 类 → 5 类，候选 1 → 2 条，验收达标。**
CI 门 18 → **21 个任务**（L1 1 + L2 14 + L3 6），L2 连跑 6 轮 6/6 全稳。

| 分类 | 失败次数 | 路数 | 过门 |
|---|---|---|---|
| `logic` | 5 | 5 | ✅ |
| `not_found` | 3 | 3 | ✅ |
| `resource` | 2 | 2 | —（差 1 次） |
| `syntax` | 1 | 1 | — |
| `unknown` | 1 | 1 | — |

### 三个新任务（L2，全离线可回放）

`fail_missing_toolchain`（not_found）／`fail_syntax_modules`（syntax）／`fail_resource_oom`（resource）。
每个都自带 **≥2 条不同的路**——`propose` 的门槛是"同一分类跨 ≥2 条路累计 ≥3 次"，只堆次数过不了。

**刻意不做 `transient_io`**：它在 `propose` 里有双保险、永远成不了策略（那是块D 自动重试的活），
补了只是让语料好看。**也不做 `auth`**：评测以 root 跑，造不出可靠的 permission denied。

### 四个真跑照出来的东西

**① 模型会自己 `apt-get install` 把缺的工具装上——而且真装成了。**
第一版夹具用 `cargo`（本机没装）当"走不通的路"。真跑时模型直接
`apt-get update && apt-get install -y -qq cargo`，**在开发机上把 cargo 和 rustc 装上了**——
评测 gate 是 allow_all（等价用户点了"本会话全部允许"），没有任何东西拦它。

后果三重：夹具前提（这东西不存在）当场失效；录音因联网输出而不可回放；**开发机被评测改了**。
修法：工具名改成虚构的内网工具（`acme-build` / `acme-verify`），装不上、也不联网，
prompt 里再明确"这台机器是干净的外网环境，不要尝试安装任何东西"。
纪律：**夹具不能依赖"本机恰好没装什么"——那是会漂的环境状态，不是夹具**（已加测试钉住）。

**② `cmd 2>&1; echo "exit=$?"` 让 shell 失败对评估内核完全隐形——真跑里连撞三次。**
模型习惯把命令串起来并自己打印退出码，于是 `run_bash` 拿到的退出码是 **echo 的 0**，
ShellEvaluator 判"成功"→ 不进 issues、不分类、不进失败语料，块E/块G 永远学不到。
`fail_missing_toolchain` 里三条命令全走不通，却只有一条留下痕迹（那条是因为输出含 Traceback
被 CodingEvaluator 接管才幸存）。

**与块 V1a 修的"吞退出码"是同一家族：退出码是硬事实，丢了就什么都判不了。**
修法（`ShellEvaluator`）：命令里确实写了 `$?` **且**输出里解析得出非零退出码 → 判失败。
判据刻意收得很窄——宽一点（"输出里有 Error 就算失败"）会把 `cat error.log`、grep 到 Error
的正常输出全判成失败，那正是 V4a 刚清理掉的那类污染，不能反手又造一批。
**已知不覆盖**：串联但不打印 `$?`（如 `a; b`）时失败仍隐形；覆盖它只能靠更宽的文本启发式，
风险明显更高，留作显式决策。Golden +3（含一条反向闸），既有期望一条未改。

修完立竿见影：`not_found` 从 1 次/1 路 → **3 次/3 路**，直接把验收顶过线。

**③ 强模型不会去撞"静态可识别"的失败。**
`fail_resource_oom` 第一版：模型读完代码直接看出"一次性 materialize 两千万行"，
改成生成器就跑通了——**压根没撞过 OOM**，一条 resource 语料都没采到。
同批 1「正例触发率全 0」一个道理。修法是在 prompt 里要求**先各跑一遍复现现象再动手**——
这不是为了凑语料，"先复现后修"本来就是项目规范里写死的做法，模型跳过的正是这一步。

推论（值得单独记）：**能稳定采到语料的失败，是那些取决于环境的失败**（本机有没有这个命令/包），
静态看不出来、必须真跑才知道。可静态识别的失败面，采样率天然低。

**④ OOM 的回溯不确定，根因是 CPython 的错误定位插入符。**
第一版夹具回放偶发 miss（约 1/6）。逐字 diff 定位到：同一行代码，`^^^^^^` 与 `~~^~~` 交替出现——
插入符指向 MemoryError 在表达式的**哪一步**抛出（分配 dict 时 vs 算 `i * 2` 时），纯看分配时机。

**没有去归一化它**：它跟堆地址/耗时不一样，**是有语义的**（指出在哪一步失败），
抹掉就是第三次放宽 ADR 0027 决策 4 的边界。改夹具让爆点唯一（预分配 `[0] * n` 单一分配）
比放宽边界便宜得多，且任务的教学点（别一次性 materialize）一点没丢。
自检里钉了一条"同样的脚本跑 8 次输出必须完全一致"，防这类不确定性再溜进门。

### 仍未过门的两类（如实记）

`resource` 2 次/2 路（差 1 次）、`syntax` 1 次/1 路。它们不是没采到，是**次数不够**——
`propose` 的 min_count=3 本就是为**真跑 `--repeat 3`** 设计的（那才有独立样本），
而离线回放收割每个任务只跑一遍。要把这两类也顶过线，走 `harvest.py --live --repeat 3`，
或再补一两个同类任务。**不调门槛。**

## 块 V5 — detector 阈值调优 ✅ 工具就位并出了第一份结论（2026-08-20）

`scripts/eval/detectors.py`（新）+ 自检 `tests/test_eval_detectors.py` **13 测**。
离线回放 21 个任务收集轨迹，**零模型调用、不烧 key、秒级出全谱**。

### 先修正一条方法论：ROADMAP 原案的 A/B 方式**走不通**

原文写的是"replay 固定模型输出 → 只改阈值 → 对比 Run Record，**这是唯一能把模型随机性
从改动效果里剥离的方法**"。这条自相矛盾，理由是块 V3 早就立下的限制：
**nudge 文案会注入进消息历史**。阈值一改、触发与否就变，注入文案跟着变 →
请求指纹变 → **cassette 当场 miss**。也就是说：回放能跑通说明什么都没变，真变了就跑不通。

替代办法更简单也更强：**detector 全是纯函数**（`detect_stuck_edit` / `detect_browse_nudge` /
`detect_repeated_failure` 只吃 `(calls, out_by_id, state, threshold)`），而回放出来的事件流里
带着每次调用的**完整入参与完整输出**（`tool_result` 事件本来就带 `output`）。
于是可以把 detector **离线重放**：同一条既有轨迹，把阈值从 1 扫到 12，看每个值下
正例触发几个、反例误报几个。

**边界写死（报表里也印着）**：扫描回答「给定这条轨迹，阈值 X 下会不会响」，
**不回答「响了之后模型会不会因此变好」**——那需要模型真看到新文案再走一遍，回放做不到。
第三列只能从**实际发生过的**触发里统计，是**观测不是实验**。

### 计分板（当前阈值，21 个任务）

| detector | 误报（反例里误响） | 触发率（正例里响了） | 触发后改善 |
|---|---|---|---|
| `login_hint` | 0/10 | 1/1 | 0/1 |
| `stuck_hint` | 0/10 | 0/1 | — |
| `search_hint` | 0/10 | 0/1 | — |
| `deadend_hint` | 0/11 | 0/3 | — |
| `research_hint` | 0/9 | 2/2 | 2/8 |
| `truncation_hint` | 0/10 | 1/1 | 0/1 |

**误报全 0**（这符合预期：反例是硬断言门，有误报的话门早就红了）。

### 三条真实的取舍曲线（这是 V5 的正题）

| 旋钮 | 阈值 | 正例触发 | 反例误报 |
|---|---|---|---|
| `stuck_edit_threshold`（当前 **3**） | 1 / 2 / 3 / 4 / 5 | 0 / 0 / 0 / 0 / 0 | **1** / 0 / 0 / 0 / 0 |
| `_BROWSE_NUDGE_AT`（当前 **6**） | 3 / 4 / 6 / 8 / 12 | **1** / 0 / 0 / 0 / 0 | **3** / **1** / 0 / 0 / 0 |
| `deadend_threshold`（当前 **2**） | 1 / 2 / 3 / 4 / 5 | **3** / 0 / 0 / 0 / 0 | **2** / 0 / 0 / 0 / 0 |

**结论：三个旋钮都不动，而且这是第一次有证据支持"别动"。**

- `search_hint`：想让那唯一的正例响，得把阈值降到 3，代价是**3 个误报**；降到 4 则
  正例仍不响、还多 1 个误报。按"误报比漏报贵"的判据（ADR 0027 决策 6），维持 6。
- `deadend_hint`：降到 1 能让 3 个正例全响，代价 2 个误报。同上，维持 2。
- `stuck_edit_threshold`：≥2 时全谱都是 0 触发 0 误报——**这条轨迹集里它根本没有可调的余地**。

### 但这份"零误报"要读对：它一半是"从不响"的另一种说法

`stuck_hint` / `search_hint` / `deadend_hint` 在当前阈值下**一次都没响过**（正例也没响）。
"误报 0"因此没有含金量——**一个从不开口的 detector 当然不会说错话**。

真正的问题回到老地方：**正例压力不够**（同批 1「正例触发率全 0」、批 3「crazy 一轮做完」
是同一个病）。要判断这三个 detector 到底有没有用，先得有能**稳定逼出**那条坏路的正例，
而不是调阈值。**在没有触发样本的情况下调参，调的是噪声。**

下一步（记下来，别当已解决）：
- 给这三个 detector 补**能稳定施压**的正例（参考批 2 桩世界的做法：把"世界会不会给出坏输入"
  从模型手里拿回来）；
- 第三列的样本也太少（`research_hint` 8 次里 2 次改善，其余 1-2 次）。样本 <10 时它只是个仪表读数。
- 真要做"改文案/改阈值之后模型是否变好"的 A/B，只能**真跑 × N 次重复**（ADR 0027 已知限制 1
  说的就是这件事），回放在这一步帮不上忙。

## 分期与成本

| 块 | 依赖 | 粗估 | 开发机可做 |
|---|---|---|---|
| V0 | — | ~1 天 | ✅ **已完成并本地验收** |
| V1 | V0 | 1~2 天 | ✅ **已完成并本地验收** |
| V2 | V1 | 3~5 天，可分批 | ✅ **三批全部完成**（批 1/2/3 均真跑验收，CI 门 6 → 18）|
| V3 | V1 | 2~3 天 | ✅ **已完成**（录 ❌ / 放 ✅，且已进 CI）|
| V4 | V0,V2,V3 | ~2 天 | ✅ **已完成并验收达标**（补失败面后候选 2 条；CI 门 21）|
| V5 | V4 | 持续 | ✅ **工具就位 + 首份结论**（三个旋钮：数据支持维持现状）|

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

**新增待确认（2026-08-24，v3.75.0）**：界面上按下停止后，正在跑的联网搜索多久真的停下来。
本机真跑测到的是"当前这一跳以内"（`scripts/diag_stop_realrun.py` 16/16），但真机网络更慢，
值得亲眼看一眼。**这一版走的正是上面警告的"先定版推送、验证后补"那条路——验完记得回写三处。**

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
