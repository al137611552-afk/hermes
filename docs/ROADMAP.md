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
- ⏳ **后续（治本，未做）**：上游检索（option B 拓宽抓取 20–30 条 + 模型重排过滤再读）；目标满足驱动的换源（触发从"零新域名"补成"目标数据点连续缺席"，价格/数字类先做）；研究墙·墙钟时间上限。Issue 生命周期 / 通用 Search Policy 抽象按需后置。

**交付物**：`evaluators/research.py`（H1）+ `loop.py detect_low_quality_research`（H2）+ `tests/test_research_evaluator.py` 8 测 + `tests/test_research_refine.py` 7 测 + config 两项 + ADR 0018 + Golden 2 条 + `scripts/diag_blockH.py`。
**验收**：H1 ✅ 小红书超预算结果 `within_budget=0`+blocker issue、有在预算内不误报、无标价只给 signal。H2 ✅ 不达标触发重搜提示、达标不触发、无预算不触发、per-query 封顶、换词另起计数均有测试；**待 Windows 真机验**（观察模型据提示真的换词/换源重搜）。H3/H4 待续。

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
- **块 A–D：已完成并 Windows 真机验证通过**（2026-06-30，定版 3.46.0）。块 D 自动重试经真实 PowerShell 子进程端到端验证（`_exec_tool_with_retry` 真机执行 3 次、重试 2 次、恢复）。
- **块 E：已完成并 Windows 验**（2026-06-30，World State + Failure Memory + 死路提示；定版 3.47.0）。
- **块 H：已完成并 Windows 验**（2026-06-30，Research Evaluator 全套 H1–H4 + 全局预算止血 + Novelty/换源阶梯；Golden 扩到 42 条；定版 3.48.0）。
- **块 F：已完成**（2026-06-30，Golden Dataset 23 条 + 回归门 + 活性自检；纯测试工具，无需 Windows 验）。
- **块 G：已完成**（2026-06-30，Learning Engine——离线聚合 + 候选策略 + 治理；半自动、人审 + Golden 把关、不自动改运行时；纯离线分析，无需 Windows 验）。Golden 门已扩到 26 条含块G 边界。
- **路线图 A–H 全部实现并 Windows 验完毕**（A–D 定 3.46.0 / E–G 定 3.47.0 / H 定 3.48.0）。下一步转**治本·研究检索上游**（option B 拓宽抓取+重排）与按需项，见块 H 末尾"后续"。
