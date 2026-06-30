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
