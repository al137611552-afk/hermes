# ADR 0018 — Research Evaluator（搜索/调研结果质量评估，块H）

状态：H1/H2/H3a/H3b/H3c 均已实现（2026-06-30，待 Windows 真机验）；H4 Golden 扩充+Win 验后定版。
H3c（萃取三态 + 接地/时效闸）：解决"污染结果整批丢弃→退回训练数据→过时且白搜"。裁判 Verdict 加 `use`（可萃取相关少数）+ `salvageable`，`detect_offtarget_research` 三态化（部分污染→萃取采用、**删掉"请不要采用这些结果"** blanket 措辞；基本垃圾才重搜且禁凭记忆）；`detect_ungrounded_answer` 纯正则接地闸挂终局——时效敏感 + 做过搜索 + 无引用无声明 → 催据来源作答/声明过时，保守触发不误杀稳定知识兜底。
H3b（带图答案多模态裁判）：`loop.py detect_offtarget_answer` 挂终局 `if not calls` 钩子，连模型本轮真看过的配图块（截图/浏览器图）判图文相关性，不对题→再放一轮据图重选；`conversation.py _make_research_judge` 把 image 块合成多模态 user 消息真喂像素。**已知边界**：仅判模型看过的 image 块；markdown 图 URL 需先抓取再判（后续增量）。
关联：[0014 评估/策略架构](0014-evaluation-policy-architecture.md)、[0015 错误分类](0015-error-taxonomy.md)、[0016 World State](0016-world-state-failure-memory.md)；块F Golden 门为其前置。

## 背景

块 A–G 把执行内核铺成"事实→差距→做法"的闭环，但覆盖的是**失败差距**——工具报错、返回空、反复死路。
真实反馈暴露了另一类未覆盖的差距：用户让 Hermes "在小红书搜 618 推荐的女士睡衣，500 元以内"，
工具**成功返回了**一堆结果，但**没几条对题**（不在预算内 / 不是品类），而 Hermes **判断不出结果好坏、不会自己重搜**。

根因：现有 `SearchEvaluator` 只数命中、判空——返回 8 条就当成功。`score()` 又被刻意隔离在决策之外。
**没有任何环节判"结果返回了但不达标"。** 这是**质量差距**，不是失败差距。

## 决策

新增 `ResearchEvaluator`，把"搜索/调研结果质量"也纳入同一条 `Evaluation→Need→Decision` 闭环。分四小块：

- **H1 事实层**（已实现）：`evaluators/research.py`，接管联网搜索（`web_search`，注册早于 SearchEvaluator）。
  除命中数外，抽**可校验约束**的满足度，当前抓最硬的一类——**预算上限**：
  从 query 解析"500元以内/不超过300/≤200"等上限，再从每条结果解析标价，算 `within_budget`（在预算内的条数）。
  **blocker `issues` 只在可证伪时触发**：有上限、有命中、有标价、却**无一在预算内** → 判"结果不达标"。
  品类关键词覆盖等模糊项只当 `signals`，不当 blocker。
- **H2 决策层**（接线）：质量 issue → Need（`PROGRESS_STALLED`：返回了但没进展）→ 确定性硬规则
  `REFINE_AND_RESEARCH`：注入"结果不达标（预算/品类未满足），换关键词或换数据源重搜"事实，促模型重搜。
  per-query 计数 + 上限封顶防无限重搜（config `research_refine_max`）。**喂事实而非硬拦截**（同块E 死路提示）。
- **H3 语义裁判**（待定，有成本）：正则判不了的相关性（"是不是好推荐""是不是 618 真优惠"）交**模型裁判**打分，
  补 H1 的硬约束之外的软质量。默认开关待定（按延迟/成本权衡）。
- **H4 安全网**：Golden 语料（**小红书 618 睡衣 500 元当验收用例**，已并入 28 条）+ Windows 真机验。

## 为什么硬约束先行、模糊留给模型

预算是数值铁证——"500 元 vs 标价 899/1280/699"无歧义，正则即可、零成本、可 Golden、不会误报。
而"这条睡衣是不是好推荐"是阅读理解，正则做只会误报，**误报会把正常搜索judo成"不达标"→ 触发无谓重搜**，
反伤体验（违反块E 立的"喂事实防误报"纪律）。故 H1 只认可证伪的，软质量等 H3 用对的工具（模型）做。

## 影响

- 新增 `evaluators/research.py` + `tests/test_research_evaluator.py`（8 测，含小红书验收）；`base.py` 注册 Research（早于 Search）。
- 无新依赖（纯 re）。`SearchEvaluator` 不动（dispatch 顺序保证 web_search 归 Research，代码检索仍归 Search）。
- Golden 加 2 条 `evaluate(web_search)` 语料。H1 不改运行时控制流——`Evaluation` 经现有 `_emit_result` 纯观测通道，
  前端摘要条即时显示"结果不达标"。真正的自动重搜在 H2。

## 验收

H1：小红书 618 睡衣 500 元的超预算结果 → `within_budget=0` + blocker issue（`test_xiaohongshu_budget_miss_flags_issue`）；
有在预算内结果则不误报；无标价只给 signal 不武断判 blocker。H2：同场景下注入重搜提示、模型据此换词/换源重搜。

## 补遗：Novelty/Progress + 换源策略阶梯（2026-06-30，重搜空转的治本第一步）

**问题**：全局预算（`research_max_rounds`）只是止血——封顶轮数，治不了"为什么换词重搜没用"。根因是
**搜索引擎排序不变时换关键词只会反复召回同一批站点、零新信息**（实测搜「苹果 水果」仍得 Apple 公司）。

**与 GPT 讨论的结论**：① "Evaluator 只产事实、Policy 才决策"本就是 ADR 0014 的设计，无限重搜是**就地注入越权**
当了 Planner；② GPT 提的 `expected_gain: float` 被否——它是**喂决策的模糊分**，违反本内核「Evaluation 禁 score」
（score 仅 UI、不进决策，`test_evaluation_has_no_score_field`），且模型臆测不可证伪。改用**确定性事实**。

**机制**（`loop.py`，纯逻辑、零模型、零分数）：
- **Novelty**：`extract_domains()` 抽搜索结果域名（去 www./归一/去重），per-run `seen_domains` 累积算新域名差集。
- **Progress（两态）**：有新域名=NEW_INFORMATION、零新域名=NO_PROGRESS。**不做 REGRESSION**——"更差"需质量比较=
  又引模糊分，留到能挂硬指标（如 within_budget 减少）处再补。
- **换源阶梯** `switch_strategy_nudge`：NO_PROGRESS 且结果仍不达标 → 不换词，逐级升 `site:`官方/github → 浏览器直通
  → ask_user 问用户；走完交全局预算止血出口。
- **Policy**：`NEW_INFORMATION→换词重搜 / NO_PROGRESS→换源策略 / 预算用尽→停搜综合作答`——两个正交信号
  （Progress 决定换词 vs 换源、预算决定搜 vs 停）。

**刻意不做（YAGNI / 守 ADR 0014）**：① 换源阶梯**先焊死、不预抽象**成通用 Search Policy——Vision/Browser 第二个
消费者未到，「别建你不需要的引擎」；② Issue 生命周期(OPEN→MITIGATED→CLOSED/UNCHANGED)**暂不上**——Novelty+
Progress 已覆盖其 ~80%（"连续 NO_PROGRESS"≈"UNCHANGED"），不为优雅建跨轮身份追踪。两者待证明不够用再上。

**自检**：`test_research_answer_judge.py` +4（共 17，含阶梯升级 / 有新域名不换源两条端到端）；`diag_blockH.py` 22 项。
**待 Windows 验**：搜"2026最新显卡价格"，某轮无新来源时看是否从"换词"升级为"site:/浏览器/问用户"。
