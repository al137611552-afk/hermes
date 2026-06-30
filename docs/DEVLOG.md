# 开发日志（DEVLOG）

按时间倒序记录。每条包含：阶段 / 做了什么 / 关键决策 / 自检 / 验证状态 / 遗留问题。

---

## 2026-06-30 — 评估/策略内核 · 块H3c：萃取（三态裁判）+ 接地/时效闸

**背景**（用户反馈）：H3a 能判搜索结果好坏，但多轮搜索后结果**污染严重**（有相关有无关混杂）时，模型会**整批丢弃→退回训练数据凭常识答**——① 把有效的少数也一起扔了；② 时效问题（618 价格/最新榜单）凭训练记忆答必然**过时**、且**白搜**。诊断发现**部分是我埋的雷**：H2/H3a 的提示语"**请不要采用这些结果**"是 blanket 整批丢弃指令，正是诱导"丢光→凭记忆"的直接原因。用户："开 H3c；萃取有产出会采纳吧？和那句冲突就改掉。"——确认冲突、改掉。见 [[adr-0018]]。
**做了什么**：
- `agent/judge.py`：Verdict 加 `use`（污染结果里可萃取的相关少数）+ `salvageable`（整体不对题但有 use→该挑出来用）。prompt 升级：**"即使整体不对题也要把相关项放进 use（哪怕一两条），绝不因掺垃圾就整批丢，绝不让人凭训练记忆替代有效内容"**。parse_verdict 解析 use。
- `loop.py detect_offtarget_research` 二态→**三态**：① 对题→直接用（静默）；② **部分污染（salvageable）→ "可采用并标注来源的有——{use}；无关的丢弃即可，别整批丢、别凭训练记忆硬编，用这些有效内容作答"**（**删掉"请不要采用这些结果"**）；③ 基本是垃圾（use 空）→ 才换词/换源重搜，且"**别凭训练记忆直接作答——这类问题需要实时来源**"。H2 措辞同步加"别凭训练记忆编"。
- `loop.py detect_ungrounded_answer`（**纯正则、零模型成本**）：接地/时效闸，挂终局 `if not calls`。`_FRESH_RE`（最新/今年/价格/榜单/年份/618…）判时效敏感、`_CITED_RE`(http/来源) 判已接地、`_DISCLAIM_RE`(可能过时/以实时为准) 判已声明。**三者同时**——做过搜索 + 时效敏感 + 既无引用又无声明 → 催"据搜到内容作答并标注来源，没有就声明可能过时"。与 H3b 共用 `answer_refined` 每轮一次重答。
**关键决策**：① **优先萃取、其次声明、最后才退**——取代旧的"不行就重搜/再不行凭记忆"两极端。② **杀掉 blanket"不要采用"措辞**——它制造了整批丢弃；改成"挑出有效的用"。③ **接地闸纯正则不调模型**——不让"同样可能过时的"裁判去判过时，靠确定性信号（有无引用/声明）+ 时效正则。④ **保守触发不误杀**——已引用或已声明都放行，稳定知识（光合作用）凭常识答不打扰；只在"时效敏感 + 有搜过却没用 + 没声明"的危险区才拦。⑤ H2 预算闸（within==0 可证伪地一条都不达标）**不算冲突**、不改判定——那确实无可萃取，只软化措辞。
**自检**：`tests/test_research_judge.py` +10 测（共 22：use 解析/salvageable 三态；钩子 部分污染萃取不丢弃/基本垃圾才重搜且禁记忆；接地闸 有引用放行/有声明放行/时效敏感无依据催/非时效不误杀/没搜过不触发）。`tests/test_research_answer_judge.py` +2 端到端（共 11：搜了凭记忆答时效→再放一轮；带来源→不触发）。`scripts/diag_blockH.py` 扩到 19 项。**全回归绿：Python 55 文件 + 前端 30 + Golden 28 + 编码守卫，0 失败。**
**验证状态**：纯逻辑全本地自检。**改了决策措辞 + loop 终局控制流，待 Windows 真机验**——`diag_blockH.py` 验机制 + 活体：搜"2026最新显卡价格"看是否挑相关项用（不整批丢）、答案是否带来源而非凭记忆。
**待做**：H4 Golden 扩充（污染/萃取/接地 case）+ Win 验后定版。

---

## 2026-06-30 — 评估/策略内核 · 块H3b：带图答案的多模态裁判（看图判季节/款式）

**背景**：H3a 在**文字层**判语义，但用户原始反馈"配图一看就是冬季"——很多时候文字摘要看不出季节，是**配图**露的馅。这正是 H3b：把裁判挂在**带图答案收尾前**，连图一起看。先核实"附图"链路：`web.py` 不返回图，配图作为真 `image` 块进对话**只有**经 `browser_take_screenshot`(Playwright MCP)/`screenshot` 工具/用户上传三条路——即模型本轮**真看过**像素。见 [[adr-0018]]。
**做了什么**：
- `loop.py detect_offtarget_answer(goal, answer_text, images, judge_fn, max_images=6)`：连图喂裁判判图文相关性，不对题→返回"配图与目标不符（理由）+据图重选"提示；无图/无目标/无答案/裁判故障→None（放行）。只喂最近 6 张控成本。
- `loop.py` run()：每轮 `_exec_calls` 后累积 `seen_images`(extra_blocks 里的 image 块) + `did_research`(本轮用过 web_search/browser_*)。终局 `if not calls` 钩子：研究轮且有配图且未重判过→跑裁判，不对题→注入提示 user 消息 + **再放一轮**让模型据图重选/重搜，`answer_refined` **每轮封顶一次**防无限。
- `conversation.py _make_research_judge`：judge_fn 收到 images 时合成多模态 user 消息（`[{text},{image}...]`），provider 各自序列化（anthropic 直传 / openai 转 image_url data-url）**真把像素喂给原生视觉模型**。
**关键决策**：① **只判模型真看过的 image 块**（浏览器截图/截屏/上传图）——这正是用户 case 的链路（穿透浏览→截图）；配图若是模型没看过的 markdown 图 URL，得先抓取再判，列为后续增量（**诚实边界，已记 ROADMAP/ADR**）。② **范围限研究轮**（did_research）——避免误扰编程截图答案。③ **终局再放一轮**而非事后报警——要让模型真的据图重做，不是只提示用户。④ `answer_refined` 每轮一次 + 裁判故障放行，双保险不卡收尾。
**自检**：`tests/test_research_answer_judge.py` 9 测（detector：带图不对题催/对题静默/无图不空跑/无目标放行/故障放行/喂图封顶6；端到端 loop：不对题触发再放一轮且每轮只判一次+重答落库/对题不重答/judge=None 全惰性）。`scripts/diag_blockH.py` 扩到 15 项（加 H3b 假裁判机制）。**全回归绿：Python 55 文件 + 前端 30 + Golden 28 + 编码守卫，0 失败。**
**验证状态**：纯逻辑全本地自检（假裁判+假 provider/工具）。**改了 loop 终局控制流 + 多模态真模型调用，待 Windows 真机验**——`diag_blockH.py` 验机制 + 活体：GUI 开**浏览器穿透**搜"618夏季女士睡衣并附图"，配图若冬季款观察裁判连图判"配图与目标不符"并据图重选。
**待做**：H4 Golden 扩充（带图 case）+ Win 验后定版；markdown 图 URL 抓取再判（后续增量）。

---

## 2026-06-30 — 评估/策略内核 · 块H3a：搜索结果模型裁判（语义相关性）

**背景**：H1/H2 只判数值硬约束（预算）。真实反馈更难的一类——"搜 618 夏季女士睡衣却推厚秋冬款、配图一看就是冬季"——是**语义+多模态**相关性，正则判不了，资料查询/竞品调研同理。用户拍板"开，且两处都挂"。分两步：H3a=裁判引擎+挂搜索结果（文字层，本条）；H3b=挂带图答案前（多模态看图，下一步）。见 [[adr-0018]]。
**做了什么**：
- `agent/judge.py`：provider **注入式**模型裁判（`judge_fn(prompt, images)->str`，**多模态就绪**）。`build_judge_prompt`（goal+结果，要求只回紧凑 JSON）/`parse_verdict`（稳健解析，**解析失败→放行不拦**）/`judge_research`（goal 或内容缺失、judge_fn 故障→一律放行）。裁判是决策层的**质量闸**（有 IO），**不是**纯逻辑 Evaluator，单独住这里。
- `loop.py detect_offtarget_research`：挂 web_search 结果，**在 H2 正则之后**跑（H2 已就该 query 提示过则跳过）。不对题→注入"多数不对题（理由），换词/换源重搜"。per-query 封顶同 H2。`_latest_user_text(messages)` 抽用户目标作裁判基准。
- config `research_judge`(默认 true)；构造器默认 `research_judge=None`→存量零变化。conversation.py `_make_research_judge(provider,enabled)` 用当前 provider 建 judge_fn，主+子 Agent 两路传入。
**关键决策**：① **裁判故障/解析失败一律放行不拦**——模型会判错/超时，绝不能因裁判出错就误触发重搜或卡死（比"喂事实非硬拦"更进一步：连"喂"都不喂）。② **H1/H2 正则先行、裁判兜底**——预算这种铁证零成本秒判，判不了的才花钱调模型；H2 拦过就不再调裁判（省一次调用）。③ **judge_fn 注入**——单测用假裁判全覆盖、不连真模型；也为 H3b 换便宜模型/接图留口。④ 引擎一次建好**多模态就绪**（收 images），H3b 只差接图+挂答案路径。
**自检**：`tests/test_research_judge.py` 12 测（解析对题/不对题/垃圾输入兜底；judge_research 调用+解析/无目标放行/故障放行/多模态传图；钩子 不对题催/对题静默/per-query封顶/尊重H2已催；_latest_user_text）。`scripts/diag_blockH.py` 扩到 12 项（加 H3a 假裁判机制）。**全回归绿：Python 54 + 前端 30 + Golden 28，0 失败。**
**验证状态**：纯逻辑全本地自检过（假裁判）。**改了 loop 控制流 + 真模型调用，待 Windows 真机验**——`diag_blockH.py` 验机制 + 活体：GUI 搜"618夏季女士睡衣"若返回厚款，观察裁判判"多数不对题"并重搜。
**待做**：H3b 多模态看图（挂带图答案前，抓"配图冬季"）；H4 Golden 扩充 + Win 验后定版。

---

## 2026-06-30 — 评估/策略内核 · 块H1+H2：Research Evaluator（搜索质量评估 + 不达标重搜）

**背景**：真实反馈——让 Hermes "在小红书搜 618 推荐女士睡衣 500 元以内"，工具**成功返回**一堆超预算/不对题结果，但 Hermes **判不出好坏、不会自己重搜**。根因：A–G 覆盖的是**失败差距**（报错/空/死路），这是**质量差距**（返回了但不达标）——`SearchEvaluator` 只数命中、判空，返回 8 条就当成功；`score()` 又被刻意隔离在决策外。见 [[adr-0018]]。
**做了什么**：
- **H1 事实层** `evaluators/research.py`：新增 `ResearchEvaluator` 接管 `web_search`（`base.py` 注册早于 Search，代码检索仍归 Search）。抽**预算约束满足度**——从 query 解析上限（"500元以内/不超过300/≤200"等）+ 从每条结果解析标价 → `within_budget`（在预算内条数）。**blocker issue 只在可证伪时触发**：有上限/有命中/有标价却**无一在内** → 判"不达标"。品类关键词等模糊项只当 signal。
- **H2 决策层** `loop.py detect_low_quality_research`：web_search 出 blocker issue → 注入"返回了但不达标，换词/换源重搜"事实促模型重搜（仿块E 死路提示：探测+注入+emit `research_hint`，try 包死）。per-query 计数封顶（`research_refine_max`）防无限重搜，换关键词=新 query=另起计数。config `research_refine`(默认 true)/`research_refine_max`(1)；构造器默认 `research_refine=False` → 存量行为零变化（同 auto_retry/failure_memory 纪律）。conversation.py 主+子 Agent 两路传入。
**关键决策**：① **硬约束先行、模糊留给模型**——预算是数值铁证（500 vs 899/1280/699 无歧义），正则即可、零成本、可 Golden、不误报；"是不是好推荐"是阅读理解，正则做只会误报、**误报会触发无谓重搜反伤体验**，故留给 H3 模型裁判（待点头，有成本）。② **喂事实非硬拦截**——只回灌"这次不达标"，重搜与否模型定（沿用块E 纪律）。③ **per-query 封顶**——防"换汤不换药"被无限催。④ `research_hint` 同 `deadend_hint`：纯 model-facing 注入 + 遥测事件，无前端渲染（行为靠注入，不靠 UI）。
**自检**：`tests/test_research_evaluator.py` 8 测（预算解析多语序/分块/调度归属/**小红书超预算验收**/达标不误报/无标价只给signal/无预算只数命中/空结果）+ `tests/test_research_refine.py` 7 测（不达标催重搜/达标不催/无预算不催/per-query封顶/换词另起/非web_search忽略/构造器默认关）。Golden 加 2 条 `evaluate(web_search)` 语料（共 28 条）。**全回归绿：Python 53 文件 + 前端 30 + Golden 28，0 失败。**
**验证状态**：纯逻辑全本地自检过；H1 无运行时控制流改动（经现有纯观测通道，前端摘要条即时显示"不达标"）。**H2 改了 loop 控制流，待 Windows 真机验**——`scripts/diag_blockH.py`（9 项确定性机制）+ 活体：GUI 里真实搜一次，观察模型据提示**真的换词/换源重搜**。
**待做**：H3 模型裁判（语义相关性，有成本，待点头）；H4 Golden 扩充 + Win 验后定版。

---

## 2026-06-30 — 评估/策略内核 · 块G：Learning Engine（路线图收官）

**背景**：A–F 已把执行内核铺成稳定契约（事实/差距/做法分离、9 类 taxonomy、跨会话 Failure Memory、Golden 回归门）。块G 回答收官问题：**积累的失败证据怎么变成对 `Need→Decision` 的改进，又不失控？** 见 [[adr-0017]] `docs/adr/0017-learning-engine.md`、ROADMAP 块G。
**做了什么**：
- 新增 `src/agentcore/agent/learning/`（`aggregate`/`propose`/`StrategyStore`）：① `aggregate(FailureMemory)` 把失败行按错误分类归并成 `Aggregate`（总次数/涉及几条路/失败时 Decision/样例 detail）；② `propose()` 只对**系统性**失败（同分类跨 ≥min_paths 条不同的路累计 ≥min_count 次）升级为候选，带人话建议（`_SUGGESTION` 分类骨架）+ 理由 + **语料证据**，`transient_io` 双保险永不成策略；③ `StrategyStore`（JSON 治理）生命周期 `proposed →(人审 approve+Golden 通过)→ active → retire/rollback`，`approve()` 强制 `golden_passed=True`、状态变迁留 `history` 审计。
- `FailureMemory.rows()` 导出全部失败行供聚合（只读，不动写入路径）。
- Golden 门加 `learn` 类 3 条语料（系统性升级 / 单路不升级 / 瞬时永不升级），runner 加 `_check_learn`——候选生成边界纳入回归门。
**关键决策**：① **不自动改运行时**（ADR 0014 不变量③）——决策层仍是确定性硬规则 + 模型，G 只产**带证据的建议**，采纳与生效由人 + Golden 把关。最坏情况只是多了几条没人采纳的建议，风险面最小。② **"没过语料门不准上"写进代码**——`approve(golden_passed=False)` 直接抛错，不靠自觉。③ **只系统性失败才升级**——单路偶发块D 重试/块E 死路提示已管，避免把噪声当策略。④ `active()` 留作将来运行时只读消费接口，**本块暂不接线 loop** → 零控制流改动、零回归风险（同块A/F），接线留后续增量。
**自检**：`tests/test_learning.py` 14 测（聚合分组/证据/空库；propose 系统性才出/单路不出/瞬时永不出/门槛可调；store 持久/幂等刷新证据/approve 强制 Golden/retire/rollback/按状态过滤；端到端"历史轨迹→一条可解释、Golden 后生效的策略"）。Golden 26/26（+3 块G）含活性自检。**全回归绿：Python 51 文件 + 前端 30，0 失败。** 过程中编码守卫（`test_encoding_guard`）抓到 JSON store 的 `read_text/write_text` 漏显式 `encoding=`，已改 keyword 形式——守卫起作用。
**验证状态**：✅ 纯离线分析工具、无运行时/GUI 行为，本地自检即等价证明，**无需 Windows 验**（同块A/F）。
**待做**：A–G 全部实现完毕。块 E 死路提示待 Windows 真机观察；E+F+G 三块统一在 E 验过后定下一版本。Learning 运行时接线（让 active 策略真正影响选路）属后续增量，须再过 Golden。

---

## 2026-06-30 — 评估/策略内核 · 块F：Golden Dataset + 回归门（Learning 的安全网）

**背景**：块G（Learning）要自动/半自动改 `Need→Decision` 映射，**没有语料门就不准上**——否则一次坏改动会悄悄劣化决策内核且无人察觉。块F 先把当前决策内核的行为**冻结成回归基线**。见 [[adr-0014]] 块F / ROADMAP。
**做了什么**：
- `tests/golden/cases.py`：23 条决策点语料（`输入→期望输出`），覆盖块A verdict→Need / 块B evaluate 事实（issues 有无 + 关键 metric）/ 块C classify 主分类（含 None=不匹配）/ 块D decide_retry（重试与否+退避）/ 块E detect_repeated_failure（第几次起提示换思路、瞬时永不算死路）。
- `tests/golden/runner.py`：按 `kind` 分派、重放**真实**决策函数比对期望，返回 `{passed,total,failures}`，可独立跑（退出码非零=回归）。
- `tests/test_golden.py`：并入"全回归"（已在 `tests/test_*.py` 循环内）；除"全语料过"外加**门活性自检**——注入一条错误期望，runner 必须报红（防门形同虚设）+ 未知 kind 即失败 + 语料数下限守卫。
**关键决策**：① **语料即代码（cases.py）**——决策内核是纯函数，冻结基线放代码里比 JSON 解析更直观、可注释。② **门必须自证活性**——只断言"全过"会让"门坏了恒过"也悄悄溜过；加一条"劣化必红"的元测试钉死。③ **追加不改既有期望**——新增能力追加语料；改既有期望=有意行为变更，需同步说明（这正是 G 改策略时该走的流程）。④ 纯测试工具、无运行时/GUI 行为 → 本地自检即等价证明，**不需 Windows 验**（同块A）。
**自检**：Golden 23/23 语料过；`test_golden.py` 3 测（全过 + 活性 + 未知 kind）。**全回归绿：Python 50 文件 + 前端 30，0 失败。**
**验证状态**：✅ 本地完成（纯测试工具，无需 Windows 真机）。
**待做**：块G=Learning Engine（按 `Need×taxonomy` 统计各 Decision 成功率 → 候选策略 → 人审+Golden 验后生效）。门已就位。见 ROADMAP。

---

## 2026-06-30 — 评估/策略内核 · 块E：World State + Failure Memory（跨步/跨会话死路记忆）

**背景**：块A–D 已 Windows 验证定版 3.46.0。块E 让"差距/失败"被记住——同一条死路（同工具+同入参以同种方式失败）不再被反复尝试。见 [[adr-0016]] `docs/adr/0016-world-state-failure-memory.md`。
**做了什么**：
- 新增 `src/agentcore/agent/world_state.py`：`WorldState`（单会话纯内存：Need 历史 / 按指纹聚合的失败计数 / 已证伪路径 / 未决阻塞）+ `FailureMemory`（跨会话 SQLite `data/failures.db`，key=(指纹,错误分类,Decision)，`known_deadend()` 查死路）+ `fingerprint(工具,入参)`（关键入参归一 + sha1 截 16 位）。
- `loop.py`：`detect_repeated_failure()`（仿现有 nudge：探测+记录+注入）——每个非瞬时失败经 `_assess` 拿分类、记入两库，本会话累计 ≥ 阈值或跨会话已知死路 → 注入"此路已 N 次不通，换思路"。`detect_repeated_failure` 内整段 try/except 包死。构造器加 `failure_memory=None`(默认)/`deadend_threshold=2`。`run()` 每轮建 `WorldState`，`failure_memory` 非 None 时启用。
- 配置：`config.py` AgentConfig + `config.yaml` 加 `failure_memory`(默认 true)/`deadend_threshold`(2)。`conversation.py` 加 `_get_failure_memory()`（懒建复用单实例，打开失败降级 None），主+子 Agent 两处构造都传入。
**关键决策**：① **喂事实而非硬拦截**——死路判断有误报风险，硬禁会把误报变功能缺失；注入"N 次不通"让模型自己换思路（与块A nudge、块D 回灌一脉相承）。真避坑留块F（Golden 门）兜底后、块G 按语料证据收紧。② **瞬时 IO 不算死路**——那是块D 自动重试的活，避免与重试打架。③ **一次失败=一行**——classify 给一次失败多分类，只记主分类（防重复计数把单次失败误判成多次）。④ 构造器 `failure_memory=None` 默认 → 存量测试零行为变化（同块D 手法）。
**自检**：新增 `tests/test_world_state.py` 15 测（指纹稳定/区分/忽略无关入参；WorldState 计数+证伪+阻塞；FailureMemory 记录/计数/阈值/主分类/重开持久/空分类兜底；detect 接线：第二次撞死路提示、瞬时不算死路、成功不记、每指纹一次、跨会话首撞即提示）。**全回归绿：Python 49 文件 + 前端 30，0 失败。**
**验证状态**：✅ **已 Windows 真机验证通过**（2026-06-30，随 E/F/G 定版 3.47.0）——`scripts/diag_blockE.py` 11/11 全过：SQLite 死路记忆**跨会话重开仍在**、真实 `detect_repeated_failure` 第2次起提示换思路、瞬时 IO 不误判。
**待做**：块F=Golden Dataset + 回归门（**必须早于 G**）。见 ROADMAP。

---

## 2026-06-30 — 评估/策略内核 · 块D：Auto-Retry（第一条 Need→Decision 硬规则，工具调用级）

**背景**：A/B/C 都是纯观测/等价重构（零行为风险）；块D 起**首次碰控制流**。落地第一条、也是最便宜的 `Need→Decision` 硬规则——瞬时 IO 失败自动退避重试。它本身是个论证：决策层**不必是大引擎**，几条确定性硬规则覆盖最高频情形即可。见 [[adr-0014]] 块D。
**做了什么**：
- 新增 `src/agentcore/agent/policy.py`：`decide_retry(error_classes, attempts_done, max_attempts, backoff_base) -> RetryDecision|None`。**只对 `TRANSIENT_IO` 触发**（网络抖动/超时/端口占用），指数退避 `base*2^(n-1)`，撞上限即停（不伪造 Need）。Decision = 标签 `RETRY_WITH_BACKOFF` + delay。
- `loop.py`：抽出 `_assess()`（事实评估+分类，`_emit_result` 与重试共用，口径一致）；新增 `_exec_tool_with_retry()` 包在 `_exec_tool` 外——**串行 + 并行两路工具执行都走它**。**关键：重试判据是分类（TRANSIENT_IO），不是 ok 标志**——因为 curl 超时这类常 ok=True/exit 非零返回，gate 在 ok 上会漏掉。无 Evaluator 的硬错误（ok=False）走 `classify_text` 兜底。`tool_retry` 事件上报（纯观测）。`_sleep` 可注入便于测试。
- 配置：`config.py` AgentConfig + `config.yaml` 加 `auto_retry`(默认 true)/`retry_max_attempts`(2)/`retry_backoff_base`(0.5)。conversation.py 主循环 + 子 Agent 两处 AgentLoop 都传入。
**关键决策**：① 工具调用级（每个单位调用独立重试），用户确认。② 默认开。③ **AgentLoop 构造器默认 `auto_retry=False`**——production 由 config 显式传 True，存量测试不传则 off，故**对现有测试零行为变化**（绿不靠改测试）。④ 配置走 config.yaml 而非设置面板：面板是手写分节（providers/browser/theme），所有 agent 功能开关（auto_review/auto_test/crazy_*）都在 config.yaml，auto_retry 同处才一致；为它单加面板开关反而是一次性特例。
**自检**：新增 `tests/test_autoretry.py` 12 测（decide_retry 规则全分支 + loop 接线：瞬时重试至成功/撞上限返回最后失败/逻辑失败不重试/关掉只跑一次/成功不重试/硬 ToolError 瞬时兜底）。**全回归绿：Python 48 文件 + 前端 30，0 失败。**
**验证状态**：**✅ 已 Windows 真机验证通过（块 A–D 整体，定版 3.46.0）**。块 D 经真实 PowerShell 子进程端到端验证：`_exec_tool_with_retry` 真机执行 3 次（瞬时失败 2 次→恢复 1 次）、`tool_retry` 事件 2 次、最终成功。**踩坑记录**：① 早期"断网跑 curl"测法不可靠——curl 离线会**挂起到超时**而非快速失败，工具不返回，重试自然不触发（测试方法问题，非 bug）；② `powershell -File flaky.ps1` + `Write-Error` 经 RunShellTool 的 `-Command` 包裹后**退出码被吞成 0** → 评估判成功 → 正确地不重试（测试脚本缺陷）。最终用**计数文件 + 内联 `exit 1` + `[Console]::Error.WriteLine`** 的确定性脚本（`diag2.py` 真机端到端跑通）定位：磁盘代码/config 全对，app 早先没重试只是**进程未重启持旧内存态**。
**待做**：块E=World State + Failure Memory（记住已证伪路径、跨步避坑）。见 ROADMAP。

---

## 2026-06-30 — 评估/策略内核 · 块C：Error Taxonomy（错误分类）

**背景**：块B 出了结构化事实，但"失败"还只是自由文本，无法聚合/当 key。块C 把失败归到稳定分类，作块D 重试判据、块E/G 聚合 key。见 [[adr-0015]] `docs/adr/0015-error-taxonomy.md`。
**做了什么**：
- 新增 `src/agentcore/agent/taxonomy.py`：`ErrorClass` 枚举 9 类（TRANSIENT_IO/AUTH/NOT_FOUND/SYNTAX/LOGIC/RESOURCE/AMBIGUOUS/EXTERNAL_BLOCKED/UNKNOWN，与 Need 正交）+ `classify_text()`（正则规则跑文本，按优先级去重）+ `classify(evaluation, output)`（主入口）。
- **两条纪律**：① 失败判定=`evaluation.issues` 非空，没失败→`[]`（不污染 Failure-Memory，如空检索结果不算失败）；② 有失败必给类，规则没命中→`[UNKNOWN]` 兜底，绝不吞失败。
- **优先级**：TRANSIENT_IO 最前（最可行动，块D 据此重试）；根因类 NOT_FOUND/SYNTAX 排在表象类 LOGIC 前（import 缺失常是断言失败真因）。
- 接线（纯观测，不参与控制流）：`loop.py _emit_result` 在 `eval` 里附 `error_classes`；前端 `formatEval` 把 `[transient_io]` 标签缀在摘要条末尾。
**关键决策**：先规则不先上模型——零成本、确定、可测、可解释；UNKNOWN 占比就是规则盲区的度量，块G 可据此补规则/引模型，对外两函数+枚举形态不变。
**自检**：新增 `tests/test_taxonomy.py` 20 测（8 类规则 + 优先级 + 失败门控 + UNKNOWN 兜底 + 三类 Evaluator 典型失败端到端 + 枚举完整性）；`tests/web/pure.test.js` +2（分类标签）。**全回归绿：Python 47 文件 + 前端 node:test 30，0 失败。**
**验证状态**：纯后端逻辑 + pure.js 纯函数本地全自检过；前端标签视觉随块B 摘要条一起待 Windows 验。
**待做**：块D=Auto-Retry（第一条 `TRANSIENT_IO + Need` → 退避重试硬规则）。见 ROADMAP。

---

## 2026-06-30 — 评估/策略内核 · 块B：Evaluator 标准化（事实层）

**背景**：块A 搭好契约后，块B 让每个 Skill 把工具原始输出解析成结构化 `Evaluation`（事实），不再散落字符串/退出码。见 [[adr-0014]] + ROADMAP 块B。
**做了什么**：
- 新增 `src/agentcore/agent/evaluators/`：`base.py`（Evaluator 协议 + `evaluate()` 调度器 + `score()` 投影）+ 三个 Evaluator——
  - `CodingEvaluator`：解析测试/构建输出（pytest 摘要 `N passed/failed/errors`、hermes runner `N/M passed`、verify 的 🧪、裸 Traceback/AssertionError）→ metrics{passed,failed,errors,total} + 「测试未全过=blocker」issue。
  - `SearchEvaluator`：grep/glob/search_code/web_search → metrics{hits} + 空结果信号；**空结果只当事实、不判 blocker**（严重度归 Policy）。
  - `ShellEvaluator`：`[exit code] N` / stderr / 超时 / 缺程序 / 后台启动 → metrics{exit_code} + 「退出码非零=失败」issue。
  - 调度优先级：Coding > Search > Shell——「shell 跑 pytest」按内容归 Coding（出 total），而非只出 exit_code。
- 接线（纯观测，**不参与控制流**）：`loop.py _emit_result` 给能评估的工具结果附 `ev["eval"] = {metrics,signals,issues,confidence,score}`；try/except 包死，评估失败绝不影响工具结果回传。
- 前端：`web/pure.js` 加 `formatEval(eval)` 纯函数→一行人读摘要（ok/warn 级）；`app.js renderToolResult` 据此在结果下渲染 `.tr-eval` 摘要条；`style.css` 加 ok(绿)/warn(橙) 样式。
**关键决策**：Score 只是事实投影、**仅 UI 展示用**，`Evaluation` 里不存 score、决策不读它（ADR 决策第 1 条，有测试守 `test_evaluation_has_no_score_field`）。issues 是「默认策略」可被上层 Policy 覆盖，Evaluator 只给合理默认。
**自检**：新增 `tests/test_evaluators.py` 24 测（三类 Evaluator 各种真实格式 + 调度路由 + Coding 优先 + score 投影 + loop 接线附 eval）；`tests/web/pure.test.js` +5（formatEval）。**全回归绿：Python 46 文件 + 前端 node:test 28，0 失败。**
**验证状态**：后端事实层 + pure.js formatEval 纯逻辑**本地自检全过**；**前端 `.tr-eval` 摘要条的视觉呈现待 Windows 真机看一眼**（DOM 渲染，Linux headless 看不了）。
**待做**：块C=Error Taxonomy（把 signals/issues 归到稳定错误分类，作 Failure-Memory/Learning 的 key）。见 ROADMAP。

---

## 2026-06-30 — 评估/策略内核 · 块A：契约骨架（行为等价重构）

**背景**：crazy 4 块跑通后，要继续叠 Auto-Retry / Failure-Memory / Learning，必须先把散落的"判断"抽成稳定契约（见 [[adr-0014]] `docs/adr/0014-evaluation-policy-architecture.md` + `docs/ROADMAP.md`）。块A=地基：定义契约、把现有判断映射上去，**不引入任何新能力、不改任何行为**。
**做了什么**：
- 新增 `src/agentcore/agent/contract.py`——`Need` 枚举（9 个差距，**绝不含动作**：无 REPLAN/RETRY/SWITCH）+ `Evaluation` dataclass（metrics/signals/issues/confidence，**不存 score**，Score 只是投影不回喂）+ `verdict_to_need()` 纯映射 + 三个 nudge 各自的 Need 常量。
- `loop.py` 重构：三个情境探测器（login_wall/browse/stuck_edit）从"探测里直接拼字符串"改成"探测事实→归到 Need→`_nudge_injection(need)` 按 Need 选文案"。**文案逐字不变**、三个 `detect_*` 公开签名与返回（`str|None`）不变。
- `conversation.py` run_autonomous：解析 verdict 后加 `emit("crazy_need", {need})` 上报稳定 Need（仅观测/Learning 聚合 key）；**分支仍按 verdict 走**（done→验收门、phase_done→重规划），保证逐字节等价——Need 在旁记账不夺权。
**关键决策**：Need 故意抽掉 scope（done 与 phase_done 都→GOAL_SATISFIED），scope 差异留在 verdict 里驱动分支；契约只新增"映射 + 观测"，不改控制流——这是"行为等价重构"的边界。
**自检**：新增 `tests/test_contract.py`（9 测：Need 无动作成员/恰好 9 个/str 可序列化/verdict 映射/未知→CONTINUE/Evaluation 默认空/as_event 拷贝隔离/无 score/nudge 全是差距）。**全回归绿：Python 45 文件全过（含 test_conversation 83、test_stuck 13）+ 前端 node:test 23 全过。**
**验证状态**：纯后端逻辑、无 GUI/真实模型依赖，**本地自检即等价证明**（无需 Windows 真机）。`crazy_need` 是新增观测事件，前端未处理=安全忽略，不影响现有 UI。
**待做**：块B=Evaluator 标准化（Coding/Search/Shell 出结构化事实）；块C=Error Taxonomy。见 ROADMAP。

---

## 2026-06-28 — crazy 阶段化重做 · 块1：开局产出阶段计划、一阶段一阶段推进

**背景**：用户批评 crazy 只按步数续命、没结构（见 [[hermes-dev-crazy-phase-design]] 方案，gating 已定自适应）。开始落地，先做地基块1。
**块1（directive 改动，把执行单位从"步"导向"阶段"）**：`_CRAZY_DIRECTIVE` 改——开局先把目标拆成**有序阶段 P1/P2/…，每阶段写明【目标 + 怎么验收（尽量是能跑的测试/可检查产物，而非'写完了'主观判断）】**，用 update_tasks 建阶段清单；然后**一个阶段一个阶段推进**（不一上来铺开并行硬干），当前阶段跑验收确认达成再进下一阶段；自评从"对照 GOAL"改成"对照**当前阶段验收**"，`[[CONTINUE]]` 标明在推进哪个 Pn。`_build_crazy_prompt` 首轮提示拆阶段、后续轮"聚焦当前阶段、跑验收再进下一阶段"。
**自检**：`[[DONE]]/[[CONTINUE]]` 格式未变、解析照常；conversation 测试过、全回归绿。
**待做**：块3=阶段门 checkpoint + **自适应过门**（测过自动续 / 失败到限次·目标模糊·设计岔路→ask_user 停下问，需让 crazy 的 ask_user 在阶段门"该问就真问"而非自动放行）；块4=阶段后重规划。

## 2026-06-28 — crazy 阶段化 · 块2：验收门（测试不绿不准收尾，把"完成"从自报改成测试驱动）

**块2（治"卡步数没价值"的关键）**：crazy 外层循环在 `verdict=="done"` 处加 `_crazy_verify_gate`——声明 DONE 前强制验收：① 纯调研/无文件产物→无可测，放行；② 配了 `test_command`→**真跑**，红了**不放行**、把失败输出回灌让它修（硬门，反复修到绿或耗尽预算护栏）；③ 没配 test_command→**逼一轮"用项目自己的方式实跑验收并贴真实输出"**（`verify_forced` 只逼一次防死循环，之后信任）。`continue` 回 while 顶部、预算护栏仍生效。这样"完成"不再是模型嘴上说，而是**测试驱动**。`test_conversation.py` +1（4 分支：无产物放行/无命令逼一次/命令红不放行/命令绿放行）79/79、全回归绿。
## 2026-06-28 — crazy 阶段化 · 块3：自适应过门（该问就真问，类人协作）

**块3（最出体验的一块）**：让 crazy 在阶段门**"该问就真问"**——平时无人值守自走，**只在卡住/岔路才停下问用户**。
- **难点解法**：crazy 里 ask_user 全自动放行（无用）。新增 `_crazy_gate_ask`——临时 `set_auto(False)` 真问一次（复用现成 ask_user 阻塞 UI）、等回复、问完恢复 auto（阶段内零碎决策仍自动不打扰）。stop 时 `_ask.reset()` 会唤醒、不卡。
- **两个停下问的触发**：① **agent 主动**——撞设计岔路/下一阶段目标模糊时，那轮末输出 `[[NEED_USER: 问题]]`（`_parse_crazy_verdict` 新增解析、优先于 DONE/CONTINUE），外层 `_crazy_gate_ask` 真问、回复带进下一轮；② **验收反复真红**——验收门返回三元组多带"是否真红"，连续真红到 `crazy_verify_ask_at`(默认3) 次 → 停下问用户（继续修/换思路/跳过收尾/我来处理），据选择走。
- **可关**：`crazy_gate_ask`(默认 True)；设 False = 纯无人值守（NEED_USER 按合理默认自走、验收只按预算兜，回到旧行为）。directive point4 改成"零碎自决、真岔路用 NEED_USER"，verdict 列表加 NEED_USER。
**自检**：`test_conversation.py` 80/80（验收门三元组 / NEED_USER 解析优先级）、全回归绿。
**待做**：块4=阶段后重规划剩余阶段。**块1+2+3 待真机/headless 真跑验"拆阶段→逐阶段做+验收→卡住/岔路停下问你"。**

## 2026-06-29 — crazy 阶段化 · 块4：阶段后重规划（计划随进展演化，别死守初始拆分）

**块4（收口优化项）**：每个阶段通过验收后，**先按这阶段实际学到的调整剩余阶段，再推进下一阶段**——真实开发计划会变（踩到难点 / 发现新约束 / 找到更省事的做法），固守开局那版拆分不合理。
- **阶段边界信号**：新增第 4 种 verdict 标记 `[[PHASE_DONE: 刚完成的阶段Pn + 下一阶段做什么]]`，把"**单个阶段**完成"与"阶段**中途** CONTINUE"区分开——这是检测阶段边界的最自然点。`_parse_crazy_verdict` 新增解析，优先级 **need_user > done > phase_done > continue**（撞岔路先停下问；全部完成的 done 优先于单阶段 phase_done，避免最后一阶段误触重规划而不收尾）。
- **重规划注入**：外层循环 `verdict=="phase_done"` 处 → 发 `crazy_replan` 事件（前端加系统行展示）+ `_crazy_replan_directive` 把"回顾本阶段所学、调整 update_tasks 里**尚未完成**的阶段（补遗漏/删多余/重排/拆细合并，**已完成的别动**）、再继续"接上模型自报的下一阶段，作为下一轮 nxt 注入 `_build_crazy_prompt`。
- **可关**：`crazy_replan`(默认 True)；设 False = PHASE_DONE 退化成普通续命（不注入重规划、不发事件，死守初始拆分）。directive point6 标记列表加 PHASE_DONE 并说明触发重规划。
**自检**：`test_conversation.py` 83/83（+3：PHASE_DONE 解析与优先级 / 重规划注入 / replan 关退化）、全回归 Python 44 + 前端 23 全绿。
**至此 crazy 阶段化 4 块全落**：拆有序阶段 → 逐阶段实现+自测 → 验收门测试不绿不收 → 卡住/岔路停下问你 → **阶段后按所学重规划** → 据回复继续。**块1-4 待真机/headless 真跑验**（重点观察：阶段过验收后是否真的回顾并调整了剩余 update_tasks，而非机械往下做）。

---

## 2026-06-28 — 登录墙「绕去搜索引擎」再治：从 directive 升级成 loop 强制注入

**问题**：上一版给了 directive「遇登录墙用 ask_user 暂停、别绕去搜索引擎」，但**没压住**——用户报「跳登录弹窗会转用 google 搜索」（之前转百度、现在转 google）。又一次印证「行为类 directive 压不住绕路本能，得上结构/硬注入」。
**修（loop 级强制注入，借 detect_stuck_edit/browse_nudge 那套）**：`agent/loop.py` 加 `detect_login_wall`——浏览器穿透下（registry 有 browser_* 工具），某次 `browser_*` 结果命中**登录墙强信号**（`请先登录`/`登录后查看`/`扫码登录`/`需要登录`/`sign in to continue`/`/login`/`/signin` 等；刻意避开"页头有登录按钮但正文可读"的弱信号）时，**当场往下一轮注入硬指令**：「**必须**用 ask_user 暂停让用户登录、等回复后重开目标页；**严禁** browser_navigate 到 google/baidu/bing 绕开」。每轮最多注一次。比静态 directive 在关键时刻更管用。`test_stuck.py` +3（命中即注入并禁搜索引擎 / 正文页不误伤 / 非浏览器工具忽略）。
**自检**：全回归 Python + 前端全绿（修了 `_DummyReg` 无 `names()` 的防御）。

---

## 2026-06-28 — 浏览器遇登录墙改成「暂停让用户登录」+ minimap 刻度文字放大

**① 遇登录墙暂停让用户登录**（用户：遇到登录应该让我登，现在直接绕去百度了）：原 directive 是「靠已登录好的会话、过不去就换其它来源」→ 一撞登录墙就绕走。改成——**遇登录墙/验证码/滑块先用 `ask_user` 暂停**，提示「需要登录 X，请在弹出的浏览器里登录·划滑块，好了回复『继续』」，**等用户回复后再 browser_navigate 重试**（登录态进持久 profile，这次就进）；只有用户明确说登不了才换来源。改 config.yaml 深度调研（主 agent）+ delegate.py researcher 两处 directive。**子 Agent 原本没 ask_user** → `_subagent_registry` 补传 `ask_user_binding` + `ask_user` 进 `_READ_ONLY_TOOLS`，researcher 也能暂停求登录（真测 researcher.names() 含 ask_user）。注：crazy 无人值守模式 ask_user 自动放行，登录暂停在那不生效（固有，登录墙在 crazy 里只能跳过）。
**② minimap 刻度文字放大**（用户：鼠标挪到刻度时对应文字放大、更直观知道是哪条）：原悬停标签要精确悬到 3px 刻度才显示。改成**由鱼眼 mousemove 驱动**——鼠标在刻度区滑动时取最近那条刻度，把它的消息文字弹出；标签字号 12.5→14.5px、加 scale 入场。刻度本身的 dock 放大保留。
**自检**：全回归 Python + 前端全绿，`node --check` 过。**GUI 待真机验**。

---

## 2026-06-28 — 修两个真机 bug：浏览器穿透「装好却用不了」+ 前台命令超时放宽

**① 浏览器穿透 connects 23 工具但 browser_navigate 报「chrome-for-testing is not installed」**（真复现定位）：在 Linux 真跑 `npx @playwright/mcp@latest` 暴露——**新版 `--browser` 合法值只有 `chrome/firefox/webkit/msedge`，没有 `chromium`**！我们传 `--browser chromium`（无效）→ server 回退默认 chrome-for-testing → 没装 → 报错。而旧安装命令 `@playwright/mcp install-browser chrome-for-testing` 在新版**只打个 playwright warning、啥也不装还退 0**（缓存里只有 chromium-*、无 chrome-for-testing），害得「装好了但用不了」。修：① `browser_mcp_args` 的 `--browser chromium`→`chrome`（chrome 通道直接用系统已装 Google Chrome，多数 Windows 本就有→零下载）；② 安装命令 `install-browser chrome-for-testing`→`playwright install chrome`（真装、系统有 Chrome 则秒过）。**venv+mcp SDK 真测**：`--browser chrome` 后 navigate 越过「未安装」错（只剩 Linux 容器 sandbox 问题，Win 无）。
**② 前台 run_powershell 命令超时 >60s**：用户以为之前加了"自动答 y"——其实加的是 `stdin=DEVNULL`（交互命令拿 EOF 快速失败/走默认，**实测 `npm create vite --template vanilla` 1.6s 完成不卡**）+ 超时提示，没加字面 auto-y（`(y/N)` 答 y 会误删、不通用）。这次超时多半是**真·慢命令**（装依赖/编译本身 >60s）在前台跑。修：`shell_timeout` 60→180s 容纳装依赖/编译；长服务仍应 `background:true`。
**自检**：全回归 Python + 前端全绿。**待真机验**：浏览器穿透重新启用（或重启）后 browser_navigate 能真开 Chrome；慢命令不再轻易撞超时。

---

## 2026-06-28 — 指挥中心弹层（整合切换/停止/改名）+ 浏览器穿透安装状态持久化

**① 指挥中心弹层**（用户：切换/停止/改名按钮太分散、且没法指定切到哪个会话）：topbar「▶ N 运行中」chip 点击 → 弹出**运行中会话列表**，每行可**点名切换 / ⏹ 停止 / ✎ 就地改名**，一处搞定。`runningSessions()` 统计、`lastSessions` 缓存标题、点外部关闭、列表变化时同步刷新。
**② 浏览器穿透安装状态持久化**（用户：点击下载→关窗重进显示「未启用」→ 再点又从零下载）：根因 `set_browser_mcp_state(True)` 只在**下载成功后**才写，关窗中断就丢。修：① 点击启用时**立即持久化 enabled=True**（意图先落盘），`_install_browser_bg` 成功不再重复设、硬失败/异常才撤销（避免每次开机重试同一失败）；② 开机 + 打开面板时检测「已启用但没连上」→ **后台自动续装**（`install-browser` 幂等：已装秒过、没装完续上，不从零重下）；状态文案加「正在继续安装…」。
**自检**：全回归 Python + 前端全绿。**GUI（指挥中心弹层 / 穿透续装）待真机验**。

---

## 2026-06-28 — UX Tier2-②：会话「运行中」状态 + 并发 + 每会话独立模型

**先读后改发现核心已实现（FR-8.2b）**：后端每会话独立 worker 线程 + 队列 + 取消，`_leave` 切走时**不停** `is_busy` 的会话、`load_session`/`switch_conversation` 复用后台活着的运行时——**并发本就在跑**；前端运行中脉冲点 / 等待权限橙点 / 未读实心点 + 状态变化实时刷 sidebar 也都有。**没有内部串行化**：每会话每轮各自 `build_provider(self.active_model)`，不共享客户端。
**补成"指挥中心"（增量）**：
- **全局「▶ N 运行中」概览**（topbar chip）：统计跑着的会话、点击跳到下一个非当前的运行会话。
- **sidebar ⏹ 停止按钮**：运行中会话行直接停（复用 `stop_conversation(cid)`），不必先切过去。
**回应用户关键点「不同会话能不能用不同主 agent，否则共享一个 api 会拥堵」**：
- 澄清——`active_model` **本就是每会话独立**、各建各的 provider，不共享 agent/客户端、不内部拥堵；唯一"拥堵"是多会话用**同一模型**撞 provider 速率限制，分散到不同模型即可，而这**架构上早支持**。
- 真缺口在 UX：① 切换会话时模型下拉没同步成该会话的模型（看起来像共享一个）→ `switch_conversation`/`load_session` 返回 `active_model`，前端 `syncModelSelect` 切换时同步（仅改显示、不动全局默认）；② 每会话模型**没跨重载存活** → sessions 表 `model` 列既有（create_session 已存初始），补 `store.get/set_session_model`，`_make_conversation` 对已存会话读回其模型、`set_active_model` 改完绑定到当前会话。`test_p6_store.py` +1。
**自检**：全回归 Python + 前端全绿。**GUI（运行概览/停止按钮/切换同步模型）待真机验**。

---

## 2026-06-27 — ✅ 定版 v3.45.0（UX Tier2 统一管理面 MCP/Hooks + 一批真机 bug 修复 + 配置傻瓜化）

**阶段**：UX Tier2-① 统一管理面（MCP server / Hooks 可视化增删改）+ 用户真机暴露的一批 bug 修复 + MCP 配置傻瓜化（模板/选文件夹）+ minimap 鱼眼，经**用户 Windows 真机验证通过**后定版。MCP/Hooks 后端我**headless 真跑过**（venv 装 mcp SDK + 真连官方 filesystem server = 14 工具、真跑 hook 拦截）。
**含**（详见下方各日条目）：Tier2-① MCP/Hooks 管理；3 bug（GBK 卡死/mermaid 炸弹图/读草稿连带）+ 同根复查（8 处 subprocess + 编码守卫测试 + marked/hljs 加固）；交互命令卡死；MCP 诊断升级（拆 ExceptionGroup + stderr 捕获 + GBK 智能解码 + 超时 60s）；MCP 傻瓜化（一键模板 + 选文件夹）；minimap 鱼眼；pydantic 警告。
**真机验证轨迹（MCP 连接逐轮揪定，全过）**：参数串错（手填易错→模板）→ 目录不存在（stderr 捕获显示真因）→ GBK 乱码（_decode_best 智能解码）→ 首次下载超时（20→60s）→ **连上**。**教训**：① bug③「默认编码=GBK」根更广（subprocess + 文件 I/O + 子进程 stderr 捕获，三处都栽过），靜态守卫测试比跑时复现彻底；② 配置类功能对非技术用户，**模板+选择器**比"手填命令/参数/路径"可靠得多；③ 配置类后端能 headless 真跑真验（装 SDK + 本机 npx），别只靠单测。
**自检**：`test_permissions.py` 18/18、`test_p6_mcp.py` 15/15、`test_config_keys.py` 14/14、`test_encoding_guard.py` 1/1、`test_procs.py` 13/13，全回归 Python + 前端全绿（test_cli 偏慢、单独 7/7）。

---

## 2026-06-27 — 修 3 个真机 bug（GBK 卡死 / mermaid 炸弹图破布局 / 连带的"读未发送草稿"）

**用户真机报 3 个 bug，逐个查修**：
- **③（根因·最严重）run_powershell 卡死 + 后台 `UnicodeDecodeError: 'gbk' codec can't decode`**：`subprocess` 用 `text=True` 但**漏 `encoding`**→ Windows 中文环境默认按 GBK 解码，命令的 UTF-8 输出在读取线程崩溃、进程卡住。系统性排查：**8 处** subprocess 全漏（仅 `gitsupport.py` 早有 encoding）→ `shell.py`/`procs.py`(Popen,原有 errors 缺 encoding)/`verify.py`×2/`hooks.py`/`trace.py`/`fixture.py`/`conversation.py` 全补 `encoding="utf-8", errors="replace"`。（与 v3.35.0 浏览器穿透 GBK 同根，但那次只修了安装子进程、漏了这批执行路径。）
- **①mermaid 语法错把"报错炸弹图"喷到页面顶部破坏布局**：mermaid 10.9.1 `render()` 解析失败时**先往 `document.body` 注入报错元素（副作用）再抛异常**，原 try/catch 只接住异常、拦不住注入。修：渲染前先 `mermaid.parse(src,{suppressErrors:true})` 校验（不抛不注入），不合法就保留源码块跳过；`initialize` 加 `suppressErrorRendering:true`；finally 兜底清理可能残留的 `#d{id}`/`#{id}` 元素。
- **②"agent 读取未发送的草稿并纳入工作安排"**：查实**前端无任何自动读输入框并发送的路径**（`send()` 仅在显式 Enter 触发，非 Shift+Enter、非输入法候选回车；后端只收 `send_message` 传的、不碰草稿）。判定为 **①的连带**：mermaid 炸弹图冻住 GUI 后，用户在冻结态按了 Enter→运行中 Enter=走 steering 把草稿"纳入当前任务"（既有设计），但 GUI 冻着看不到"消息已入对话"的反馈，遂误以为被偷读。①修好后应不再复现。
**自检**：全回归 Python + 前端全绿，`node --check` 过。**待真机验**：中文输出的命令（如 `npm install`/`uvicorn`）不再卡死；含 mermaid 的长回复不再破坏布局；② 不再复现。
**同根复查（用户要求，根比想的更广）**：
- **bug③ 根 = 任何依赖系统默认编码（Windows 中文=GBK）的文本 I/O，不止 subprocess**——也含 `open()`/`read_text`/`write_text`。AST 扫全库：subprocess 8 处已修，文件 I/O 全部本就显式带 encoding（grep 误报的两处是续行有 encoding）。**新增守卫测试 `tests/test_encoding_guard.py`**：AST 扫所有 `subprocess(text=True)`/`read_text`/`write_text`/`open(文本)`，漏 `encoding` 即红——把这一类彻底钉死、防复发。
- **bug① 根 = 第三方渲染在坏输入下抛异常/注入坏元素破坏 GUI**——除 mermaid，`renderMarkdown` 里 `marked.parse`（line 192）与 `hljs.highlightElement`（line 196）**也没裹 try/catch**（另一处 2963 裹了、不一致）。加防护：marked 抛错降级纯文本、hljs 单块失败不影响其它块/整体。畸形或流式半截 markdown 不再能崩气泡。

---

## 2026-06-27 — MCP 配置傻瓜化（模板/选文件夹）+ minimap 鱼眼放大 + 又一记 GBK

**用户真机连 MCP 反复失败（参数手填易错：把聊天里的"(每行)"也敲进去、参数串成一串）**——根因是"手敲命令+参数结构"门槛高。**傻瓜化**：
- **一键模板**：🔌 面板加 📁文件系统 / 🔧Git / 🌐网页抓取 预设，点一下自动填好 command+args 结构，filesystem/git 的目录默认填**当前工作区**（多数情况点完直接能存）。
- **选文件夹**：新 `api.pick_directory()`（复用 `open_project` 的 `FOLDER_DIALOG`，只返回路径不起会话）+ 表单"📁 选择文件夹填入目录"按钮——弹原生对话框选目录、自动填进 args 目录行，**全程免手填路径**。
- **又一记 bug③ 同根**：捕获 server stderr 当 UTF-8 读，但 npm 的中文警告/Windows 系统错误是 GBK 混合 → 乱码 + `UnicodeDecodeError`。`_decode_best` 已按字节读 + UTF-8/GBK 智能解码（混合编码下至少 ASCII 部分可读）。诊断显示真因（参数串错的 `npm warn Unknown cli config`）。
**minimap 鱼眼放大**（用户提，对标主流体验）：`#chat-index` 刻度太扁太密不好点 → 鼠标移动时按纵向距离 dock 式放大附近刻度（smoothstep 过渡、纵向多放、近的高亮），便于点击。容器加宽 16→22px，监听挂容器上（不随 rebuild 丢失）。纯前端、CSS transform。
**自检**：全回归 Python + 前端全绿，`node --check` 过。**GUI（模板/选文件夹/鱼眼）待真机验**。

---

## 2026-06-27 — MCP/Hooks headless 真机端到端验证 + B1 真因定位 + stderr 捕获

**背景**：用户代码基础弱、怕测不好配置类功能，要我代测。配置类**后端**可在 Linux headless 真跑（GUI 渲染才需真机）。装 mcp SDK 到 venv（系统 jsonschema 是 debian 管的卸不掉，故用 venv），node/npx/uvx 本机已有。
**真测结论（全过）**：
- **MCP 全链路**：`set_user_mcp_server`→`merge_user_mcp`(自动开 mcp.enabled)→`McpManager` 真连**官方 filesystem server = 14 工具**→停用(不挂载)→删除，全对。Windows `cmd /c` 包装：模拟 `os.name=nt` 验证 npx→`cmd /c npx`、含空格路径完整保留、uvx 不包（它是 .exe）。
- **Hooks 全链路**：写 user_hooks.json→merge→真 `HookRunner`：PreToolUse `exit 2` **真拦 write_file**（带密钥提示）、不误拦 read_file、PostToolUse 真回灌 lint 输出、停用后放行。
- **B1 真因锁定**：filesystem server 在我这连得上 → 用户 Windows 失败是**环境特定**。复现：**给不存在的目录** → server 打 `ENOENT: no such file or directory` 后退出 → 连接 `McpError: Connection closed`（即用户看到的 ExceptionGroup）。**所以 B1 八成是目录路径不存在/写错**。
**附带修（诊断升级）**：`_flatten_exc` 拆出的 "Connection closed" 是症状不是根因（真因"目录不存在"在 server 的 stderr 里）。给 `stdio_client` 传 `errlog=临时文件`捕获 server stderr（StringIO 无 fileno 不行、必须真文件；老 SDK 无此参数则跳过；`close()` 清理临时文件），失败时把 stderr 头部人话错误拼进提示 → GUI 现在直接显示 **"server 说：Error accessing directory X: ENOENT no such file..."**。正常连接不受影响（仍 14 工具）。
**又一记 bug③ 同根**：用户重测显示 "server 说：�����﷨…" 乱码——捕获文件当 UTF-8 读、但 **Windows 中文系统错误是 GBK**。修：`_decode_best` 按字节读 + 智能解码（先 UTF-8、失败退本地 GBK、再兜底 replace），node 的 UTF-8 错误与 Windows 的 GBK 系统错误都能读对。`test_p6_mcp.py` +2 → 14/14（含 _flatten_exc / _decode_best）。

---

## 2026-06-27 — UX Tier2-① 第 2 块：统一管理面 · Hooks

**痛点**：可编程 hooks（写文件前扫密钥、编辑后跑 linter/SAST）只能手编 `config.yaml` 的 `agent.hooks`。
**做了什么**（完全复刻 MCP 管理那套覆盖层模式）：
- 后端 `config.py`：运行时覆盖 `user_hooks.json`（list）；`read/write/upsert/remove_user_hook`（index 寻址，越界/None=追加）+ `_norm_hook`（event 非法归一 PreToolUse、timeout 兜底）+ 纯逻辑 `_apply_user_hooks`（只挂 enabled 且 command 非空、**剥掉 enabled 字段**因 HookConfig 没有、追加不覆盖手编 hooks）+ `merge_user_hooks` 接进 `load_config`。
- 后端 `api.py`：`get/save/delete/toggle_hook`，写覆盖文件 + `_reload_agent_hooks()`（重读 config 刷新 `self.config.agent.hooks`——它与 `self.res.config` 同对象，下一轮 `_make_hook_runner` 现读即生效）。
- 前端：设置面板加 🪝 Hooks 项 + `renderHooksPane`（复用 `.mcp-*` 样式）——hook 列表（调用前/后徽标 + 匹配范围 + 命令 + 启用开关 + 编辑 + 删除）+ 表单（event 下拉 / 显示名 / 工具名正则 matcher / 命令 textarea / 超时）。
**自检**：`test_config_keys.py` +2 → 14/14（hooks CRUD·index·event 归一 / 过滤·剥 enabled·与手编共存）；全回归 Python + 前端全绿，`node --check` 过。
**取舍**：Tier2-① 第 3 块「子 agent 角色管理」低 ROI（多数用内置 researcher/reviewer）+ 需额外查 `_roles` 重建路径，**暂缓**；先交这批（含 3 bug 修复 + MCP + hooks）测。
**待真机验**：面板加个 PreToolUse hook（matcher `write_file`、命令打印/扫描）→ 写文件时触发；PostToolUse 回灌；停用/编辑/删除即时生效。

---

## 2026-06-27 — UX Tier2-① 第 1 块：统一管理面 · MCP server（对标 Cursor Customize 页）

**痛点**：接外部 MCP server（filesystem/git/自定义）要手编 `config.yaml` 的 `mcp.servers`，解注释改缩进易踩坑。
**做了什么**（复用穿透那套运行时覆盖 + 重连基建）：
- 后端 `config.py`：运行时覆盖文件 `user_mcp.json`（仿 `feature_flags.json`/`mcp_browser.json`）；`read/write/set/remove_user_mcp_server` + 纯逻辑 `_apply_user_mcp(data, user)`（只挂 enabled 且 command 非空的、有启用项就自动开 mcp 总开关、Windows 下 npx/npm/pnpm/yarn 包 `cmd /c`）+ `merge_user_mcp` 接进 `load_config`（排在 `merge_browser_mcp` 前，穿透 browser 优先）。与手编 config.yaml 的 server、穿透托管的 browser 三者互不干扰。
- 后端 `api.py`：`get_mcp_servers`（附每个 server 实连工具数）/`save_mcp_server`/`delete_mcp_server`/`toggle_mcp_server`，写覆盖文件 + 复用 `_reconnect_mcp()` 即时生效。
- 前端：设置面板加 🔌 MCP 扩展项 + `renderMcpPane`——server 列表（名称/命令/连通状态/免确认徽标 + 启用开关 + 编辑 + 删除）+ 添加/编辑表单（参数/env 用 textarea 每行一条，**含空格的 Windows 路径也安全**，不用空格切分）。
**自检**：`test_config_keys.py` +3（CRUD roundtrip / 只挂启用且有命令·自动开总开关 / 空原样）12/12；全回归 Python + 前端全绿，`node --check` 过。
**待真机验**（依赖 GUI + 本机 MCP 运行环境）：面板加个 filesystem server（`npx -y @modelcontextprotocol/server-filesystem <目录>`）→ 显示连上 N 工具 → Agent 能用其工具；停用/删除即时生效。
**遗留**：Tier2-① 余 hooks / 子 agent 角色 的可视化管理；Tier2 ②③。

---

## 2026-06-25 — ✅ 定版 v3.44.0（易用性 UX Tier1：智能确认分级 + 实时预览面板）

**阶段**：调研主流 agent（Claude Code/Cursor/Windsurf→Devin Desktop）2026 上半年 UX 迭代 → 对标 hermes 出三大方向（智能确认 / 实时产物 / 统一管理面）→ 先做 ROI 最高的 Tier1 两项，经**用户 Windows 真机逐条验证通过**后定版。
**含**：① 智能确认分级（含 Windows/PowerShell 命令、脚本块守卫）；② 实时预览面板（命令行抽端口兜底、`<select>` 列全部 server）；③ 文件预览刷新按钮。详见下面两条 ②①明细 + 各自的真机迭代。
**真机验证轨迹**：①`dir` 漏白名单（Unix-only）→ 补 Windows 命令 + 大小写不敏感 + 脚本块洞；②`http.server` URL 卡 stdout 缓冲 → 命令行抽端口兜底；③多 server 下拉只显示一个（datalist 按输入过滤）→ 换 `<select>`；④文件预览缺刷新按钮 → 补 ↻。
**自检**：`test_permissions.py` 18/18、`test_procs.py` 13/13，全回归 Python + 前端全绿。
**教训**：跨平台 agent 的命令分类**必须同时覆盖 Unix 与 PowerShell**（命名、大小写、脚本块语义都不同）；GUI 小部件别想当然（datalist 会按输入过滤），真机一验就现形。

---

## 2026-06-25 — UX Tier1-②：实时预览面板（对标 Claude Artifacts / Cursor Canvas）

**痛点**：agent 建了登录页/起了 dev server，工作区面板只能看**源码**、看不到**渲染效果**——做 web 项目体感差一截（对比走查里最明显的缺口）。
**做了什么**（hermes 本身就是 webview，天然能挂 iframe）：
- 后端 `procs.py`：纯函数 `extract_localhost_url`（从 dev server 输出/命令抽 `http://localhost:PORT`，`0.0.0.0`→`localhost`、去尾随标点）+ `ProcessManager.preview_targets()`（运行中后台进程里能识别本地 URL 的，最新启动在前、已退出不列）。`api.get_preview_urls()` 暴露给前端。
- 前端：工作区 header 加 🖥 预览开关 → `ws-preview` 切成「工具条 + 占满的 iframe」：URL 栏**自动对准**检测到的 dev server（多 server 走 datalist 下拉）、可手填、回车/↻ 刷新、⤴ **在系统浏览器打开**（兜底禁止内嵌的站点，复用 `open_external`）、✕ 关闭。点文件/看 diff/切换会话**自动退出预览态**（`setPreviewToggleState` 集中管 `.previewing` 布局类，不靠 `:has` 以兼容旧 WebView）。iframe 带 `sandbox`（allow-scripts/forms/same-origin/popups）。
**设计取舍**：① MVP 聚焦「挂运行中的 dev server」（headline 价值、最稳）；静态 HTML 文件 srcdoc 渲染因相对资源路径会断、先不做。② 禁止 iframe 内嵌的站点（Django 等）不硬刚——给「在浏览器打开」兜底。③ URL 只从**输出 buffer**可靠识别（dev server 启动即打印），不从命令瞎猜端口（避免假 URL）。
**自检**：`test_procs.py` +3（URL 识别多形态 / 运行中进程识别·stop 后消失 / 无 URL 返回空）11/11；`node --check` app.js 过；全回归 Python + 前端全绿。
**真机验证迭代（用户逐条验，步骤 1–5 核心过）**：
- ① `python -m http.server` 的 "Serving HTTP on…" 打在 **stdout、piped 时块缓冲刷不出**→ 输出 buffer 抓不到 URL、不自动对准。补 `url_from_command`：从命令行抽端口（`http.server 8000`/`--port`/`-p`/django `runserver`/`host:port`）拼 `http://localhost:PORT` 兜底。`test_procs.py` +2（13）。
- ② 多 server 时下拉只显示一个：根因是 **`<datalist>` 会按输入框当前值过滤选项**（输入 `:8000` 就把 `:9000` 滤掉）。改用真 `<select>` 列**全部**检测到的 server（+「自定义 URL…」项），不受输入过滤。
- ③ 用户指出**文件预览没有刷新按钮**（改了要回树里重新点），与实时预览的 ↻ 不对称。给文件预览 header 也加 ↻（原地重读当前文件）。
**遗留**：静态 HTML 已由既有 `renderHtmlPreview` 在 iframe 渲染（非本次）；Tier2 三项（统一管理面 / 会话运行中状态+并发 / diff 行内反馈）。

---

## 2026-06-25 — UX Tier1-①：智能确认分级（治确认疲劳，对标 Claude Auto mode / Cursor Auto-review）

**背景**：联网调研主流 agent 2026 上半年 UX 迭代后，与 hermes 对标出三大方向（智能确认 / 实时产物 / 统一管理面）。Tier1 ROI 最高，先做①智能确认分级（纯后端可就地验，不依赖 GUI 真机）。
**痛点**：每条 `run_*` 都 `dangerous=True`，连 `ls`/`cat`/`git status`/`pytest` 都弹确认；长项目里点确认点到烦（只读工具 read/grep 本就不过 gate，疲劳全来自 shell）。
**做了什么**：
- `permissions.py` 加纯逻辑分类器 `command_is_safe` / `is_safe_autorun`：**保守白名单**（只读/检视 + 测试/静态检查命令）+ 子命令名单（git/npm/pip/cargo/go 只放只读子命令）+ 多重守卫（写重定向 `>`、命令替换 `$(`/`` ` ``、`sudo`、find `-delete`/`-exec`、git `-D`/`--force` 等一律拒）+ **管道/串接逐段校验**（`&& || ; |` 每段都安全才放）。**safe-by-default：拿不准一律落回确认**（误判只是多弹一次、绝不放过真危险）。`python -m pytest` 归一成把模块当命令。
- `gate.py`：`PermissionGate` 加 `auto_safe` **闭包**参数；`confirm()` 在「无显式规则且非 allow_all」时，若开关开 + `is_safe_autorun` 命中则自动放行。**闭包现读 config**——🛠 面板切换即时生效、不必重建 gate。deny 规则与毁灭性拦截优先级不变。
- `config.py`：`AgentConfig.auto_approve_safe=True`（默认开）+ 列入 `TOGGLEABLE_FLAGS`。`api.py` get/set_feature_flags 接入。`conversation.py` 构造 gate 传 `auto_safe=lambda: self.res.config.agent.auto_approve_safe`。
- `app.js` 🛠 面板加开关行（默认开，置顶）。
**自检**：`test_permissions.py` +8（白名单接受 / 危险·歧义拒绝 / 管道逐段 / 仅 shell 工具 / gate 开启免确认但写文件仍弹 / 关闭回旧行为 / deny 优先 / None=旧行为）16/16；config 默认值+`feature_flags.json` 持久化+merge 覆盖 round-trip 冒烟过；全回归 Python + 前端全绿。
**设计取舍**：① 只盖 shell 只读/测试，**不自动放行文件编辑**（那是更高信任档，本次不做，符合对用户的承诺范围）；② 默认开（对标主流 Auto mode 默认开、且保守），但给 🛠 开关让想逐次确认的用户关掉；③ 装依赖（pip/npm install）**故意仍确认**——会跑 postinstall 任意脚本，风险高。
**待真机验**：GUI 开关点击交互（后端逻辑已就地全验）。
**追加修（用户真机报 `dir` 也弹确认）**：白名单原是 Unix 命令名、**漏了 Windows/PowerShell**（`dir`/`Get-ChildItem`/`gci`/`gc`/`findstr`/`where`…），且 PowerShell **大小写不敏感**没处理。修：① 命令名匹配改大小写不敏感 + 剥 `.exe`/`.bat` 后缀（`_norm_cmd`）；② 补一批 Windows cmd/PowerShell 只读命令与别名；③ 新增**脚本块守卫**——含 `{`/`}` 一律不放行（PowerShell `gci | where { rm $_ }` 能在脚本块里藏 rm，是真安全洞）+ `@(...)` 子表达式也拦。`test_permissions.py` +2（Windows 只读放行 / 脚本块·写 cmdlet 拒绝）18/18。
**遗留**：Tier1-②实时预览面板（依赖 GUI）、Tier2 三项见 CHANGELOG。

---

## 2026-06-25 — ✅ 定版 v3.43.0（浏览器穿透质量收口：登录态模式 + browser-native 搜索）

**阶段**：把 06-24 起的浏览器穿透三件事（有头·登录态模式 + 穿透搜索掰成 browser-native + 会话行按钮顺序）经**用户三轮真机（登录知乎+穿透）验证通过**后定版。
**真机验证轨迹（三轮逐层揪定，全过）**：
1. 结构性约束（穿透开→去掉 web_fetch/web_search）先只盖了委派子 researcher → **主 agent 自己直查仍跳 web_search**。用户两条铁证（模型自述"页面没加载、先试 web_search"但实为已登录已加载；委派不跳、主 agent 跳）坐实漏的是主 agent。→ 抽 `_drop_web_when_browser` helper，`_build_registry`（主 agent）也调，主/子一律去退路。
2. 主 agent 不跳了，但 navigate 后**首次 snapshot 看着空就误判"没加载/要登录"**。→ directive 补「snapshot 空/骨架≠没加载，重 navigate/scroll/点进结果再读」。
3. 工具走对了、不跳 web_search 了，但**知乎结果页已列出内容却没认出，转头在浏览器里开百度重搜**（换搜索引擎＝新绕路）。→ directive 补「结果页带 ref 条目就是结果，点进前几条读，别换搜索引擎重搜」。→ **用户确认"开始读结果了"。**
**自检**：`test_conversation.py` 78/78（+`test_main_agent_drops_web_tools_when_browser_present`）；全回归 Python + 前端全绿。
**教训**：① 结构性约束 > 堆 prompt，但**要盖全所有触发入口**（主 agent + 每个子角色），漏一个就从那条路退回老行为；② 结构堵不住的（不能禁 navigate）只能上 directive，比结构弱一档，靠真机逐轮校准。
**遗留**：若日后模型仍爱"换搜索引擎"，可上更硬的——结果页给精简版"可点条目清单"收敛杂乱 snapshot。

---

## 2026-06-24 — 浏览器穿透加「有头·登录态」模式（反爬正解：用登录态而非破解滑块）

**需求（用户）**：穿透式搜索常撞反爬/滑块/验证码，想让 hermes 用「我登录好的浏览器」类人查询。**取舍（与用户确认）**：在本地桌面（有屏）跑「有头+持久 profile」——第一次弹可见浏览器，用户手动登录/划滑块那一次，登录态存进 Playwright MCP 的持久 profile（默认行为）跨次复用。

**做了什么**（最小且安全：只切 `--headless`）：
- `config.py`：浏览器状态文件扩 `headed`；`browser_mcp_state`/`browser_mcp_headed`/`set_browser_mcp_state(on, headed=)`/`browser_mcp_args(headed)`（有头去掉 `--headless`），`merge_browser_mcp` 据 headed 构造参数。
- `api.py`：`set_browser_headed(headed)`（写状态+重连 MCP 生效）、`get_browser_mcp_status` 返回 headed。
- `app.js`：🌐 浏览器穿透面板加「有头·登录态模式」勾选（复用 feat-row 样式，Playwright 截图核对达标）。
- `delegate.py` researcher directive 加：遇登录墙/验证码靠登录态通过、**别破解滑块**（军备竞赛必输），过不去就换官方 API/数据导出/说明跳过。

**为何只切 --headless 就够**：Playwright MCP 默认就用**持久 profile**（登录态本就跨次保留），此前只是被 `--headless` 挡着你登不进去。去掉它=可见浏览器=你能登录，profile 自动留存。
**自检**：`test_config_keys.py` +2（args 有头去 --headless / 状态 roundtrip 关闭保留 headed）9/9；全回归 Python 43 + 前端 23。
**待真机验**（需有屏桌面）：开「有头登录态」→ 跑个浏览任务弹出可见浏览器 → 手动登录某站 → 再跑发现已登录、能类人查询。

**附带修：浏览器调研「保守/低效、质量不如原版」**（用户报：相关结果被判"不是直接内容"跳过）。**几轮迭代+用户拍醒后收敛到精简方案**：
- 弯路：先瞎改 directive 改坏质量→回退；再 A/B 实测出「web_search 搜+web_fetch 读+反爬回退浏览器」一大套，**但用户点破**：反爬遍地，绕 fetch→撞墙→回退这一圈不如直接浏览器读，且加了大量描述词占上下文；**根问题其实是行为**——模型读个标题/snapshot 标题栏就轻判不相关。
- **收敛方案（精简，砍掉 ~500 字冗余 directive）**：① 搜索用 `web_search`（干净，别用浏览器搜）；② **读页面内容开了浏览器穿透就优先用浏览器**（navigate+滚动多次 snapshot 读全、有登录态过反爬、读就一直在浏览器读别回头 fetch）；③ **最关键：别只看摘要/标题/snapshot 标题栏就判「不相关」，往下读正文看够再判断**。
- **代码改**（小、不占常驻上下文、只在真被拦时冒一句）：`web_fetch` 加 `looks_blocked` 检测——Cloudflare/登录墙/JS空壳/验证码等「假成功(200)」明确报「⚠抓取受阻+改用 browser_navigate」，让模型可靠识别该回退浏览器。`test_web.py` +1。
- **真跑确认**：精简版 FastAPI 题 2 搜/4 读（理想的少搜多读）、uv 题答案多源扎实；行为掰到「读内容、不轻判」。**教训：行为类 directive 别凭感觉堆、先本地 A/B 实测，且要听用户「这是不是过度设计」的直觉。**
- **用户真机（浏览器穿透开+登录知乎）暴露真问题，定终方案**：之前"模型不调浏览器"是**没开穿透**（researcher 没浏览器工具、撞反爬只能换搜索）的误判；开着穿透后它**会**用浏览器、答案质量高（知乎 5000内 HiFi 给出具体型号+价+答主+对比表），但**效率差**——打开知乎搜索页后 snapshot 又杂又长读不出，误判"被墙"就**跳回 web_search（中文站名查询返回垃圾）瞎逛几十步**，最后才点进具体回答页读到。**终方案（结构+directive）**：① 结构性——浏览器穿透开着时 `_subagent_registry` 给 researcher **去掉 web_fetch+web_search**（断了"跳回外部搜索绕路"的退路，逼它 navigate+点结果+读内容页）；② directive——「有浏览器就一切走浏览器：到目标站/搜索引擎→**点进具体结果→内容页 snapshot 读**，别从杂乱列表页硬读、别跳回 web_search」。`test_conversation.py` 77/77（+researcher 有浏览器去 web 工具）、全回归 43/43。**待用户真机（登录+穿透）验是否不再绕 web_search、直接点结果读。** 教训补：行为压不住时上**结构性约束**（去掉绕路的工具）比堆 prompt 可靠。
- **用户真机复测，揪出漏网：结构性约束只盖了「委派的子 researcher」，没盖「主 agent」**。用户两条决定性证据：① 模型自述「页面内容似乎没加载出来，可能要登录或反爬…先试试 web_search」**但实际知乎已登录、页面也加载了**——是 navigate 后**首次 snapshot 看着空/骨架就误判**（知乎正文 JS 懒加载/在下方），加上**手里仍有 web_search 可退**就跳了；② **委派子任务不跳、主 agent 自己查就跳**（最近调研都是主 agent 直接做、没委派），精准坐实漏的是主 agent。**补全（两手）**：① 抽 `_drop_web_when_browser(reg)` helper，**`_build_registry`（主 agent）也调**——浏览器穿透开着时主/子一律去掉 web_fetch+web_search，断退路；② directive（config.yaml 深度调研 + delegate.py researcher）加「**snapshot 看着空/像骨架/没看到正文时别急着判『没加载/要登录/被反爬』就换路**——很多站（知乎等）正文是 JS 懒加载/在下方：重新 navigate 等一下再 snapshot、或 scroll 再 snapshot、或直接点进具体结果；snapshot 没出全是常态≠没加载」。+`test_main_agent_drops_web_tools_when_browser_present`，`test_conversation.py` 78/78、全回归全绿。**待用户真机（登录+穿透、主 agent 直查不委派）复验。** 教训补：结构性约束要**盖全所有会触发的入口**（主 agent + 各子角色），漏一个就从那条路退回老行为。

## 2026-06-24 — ✅ 定版 v3.42.0（crazy Ralph 修复 + 跨平台 macOS/Linux）

**收口**：crazy 串味修复 **Windows 真机 + 真 kimi 双验证通过**；跨平台改造 **Windows 侧已验**（shell auto→powershell、行为零改变）、**macOS GUI 待真机验**（无 Mac 无法预验窗口，代码已完成）。pyproject+CHANGELOG+DEVLOG+记忆已同步；全回归 Python 43 + 前端 23。Windows 包：`hermes-dev-src-3.42.0.zip`（自用）。

## 2026-06-24 — crazy 模式改造成 Ralph 式 fresh context（修「crazy 前对话串味」）

**问题（用户报）**：开 crazy 前若有对话，crazy 第 2 轮起会把 **crazy 前的对话**和**第 1 轮产出的下一步目标**混在一起重新思考、跑偏。**根因**：crazy 外层循环每轮 `self.send_message(instruction)`，而 send_message 跑在**共享的 `self.history`** 上、重喂全历史——crazy 前的内容一直躺在历史里污染目标推理（压缩只压小、不去污）。

**对照主流**：Ralph 循环 / OpenAI Codex `/goal`（2026）的核心是**每轮 fresh context、状态放文件/git/TODO 而非对话历史**。hermes 当初对标它做的，但实现成「追加历史+重喂」，恰好相反。

**改造（方案 B，与用户确认）**：让 crazy 每轮用 **fresh context**——只喂【本轮目标 + 已改动文件清单 + 下一步】，跨轮记忆全靠 `update_tasks`/`update_notes`（`_effective_system` 每轮重新注入最新态）。
- `_run_turn(messages, *, fresh=False)`：fresh 模式跳过压缩、只把 messages 原样喂模型（不背全历史），新增消息仍落 history/DB 供前端显示。
- 新 `_run_crazy_round(prompt)`（fresh 跑一轮）+ `_build_crazy_prompt(goal, step, first)`（组织目标+状态+自评标记）+ `_crazy_changed_files`（注入已改动文件）。
- 外层循环改调 `_run_crazy_round` 而非 `send_message`；`_CRAZY_DIRECTIVE` 加「跨轮记忆只有 tasks/notes、看不到上一轮对话，必须随时写进去」。
- **隔离是 fresh context 的免费副产品**：每轮只看本轮 prompt，crazy 前历史自动不进上下文。

**验证**：`test_conversation.py` 76/76（7 个 crazy 测试改 mock `_run_crazy_round`、行为不变；+2 新测：fresh 轮不含 crazy 前历史 / prompt 锚定目标）。**真 kimi 端到端**：注入无关的「做计算器」crazy 前对话 → 跑「greeting」目标 → **没被带偏**、1 轮完成、用了 tasks。全回归 Python 43 + 前端 23。
**附带好处**：每轮上下文更短更省 token、跨轮不漂移。后续可上 C（全 Ralph、状态完全靠文件/git）。

## 2026-06-24 — 跨平台：macOS / Linux 支持（一份代码，非另起分支）

**阶段**：用户要 macOS 版（原「非目标：跨平台」被此需求覆盖）。**取舍（与用户确认）**：**保持一份跨平台代码**，非复制两份单独维护——平台差异用系统判断隔离，Windows 行为零改变（双份维护会双倍工作量+漂移）。**状态**：Linux(=POSIX，mac 同源) 全验；**待 mac 真机验 GUI**（我无 Mac 无法预验）。

**审计结论**：真正锁 Windows 的极少——pywebview 外壳本就跨平台（mac WKWebView/Linux GTK）；截图(Pillow ImageGrab)、后台进程(非nt 用 POSIX killpg)、hooks/浏览器穿透(已写 posix 分支) 早就跨平台。**唯一硬锁＝默认 shell=powershell**。
**改了什么**：① `config.agent.shell` 支持 `auto`，`load_config._resolve_shell` 按系统解析（Windows→powershell，macOS/Linux→bash），config.yaml 默认改 `auto`；shell.py 加 `zsh`。② pyproject 加 `pyobjc-*; sys_platform=='darwin'`（mac 装 WKWebView 绑定，Win/Linux 不装）。③ 系统提示去掉硬写「Windows machine」，`_effective_system` 运行时注入真实 OS + `run_<shell>` 工具名（避免 mac 上误用 Windows 命令）。④ README 加 macOS/Linux 运行段（含截图授权、node 提示）。
**Windows 安全性**：所有改动平台守卫，Windows 路径解析仍 powershell、pyobjc 标记排除、行为不变。全回归 Python 43 + 前端 23。
**待 mac 验**：`pip install -e .`(自动装 pyobjc)→`python -m agentcore.app` 起 WKWebView 窗口；run_bash 执行命令；截图授权后可用。

## 2026-06-24 — ✅ 定版 v3.41.0（深度审计后的能力深化 + 情境自启，Windows 全验通过）

**本版收口**：自 v3.40.0 起这一批全部 Windows 真机验证通过、定版 v3.41.0——委派评分回炉、可编程 hooks、大库检索 search_code、情境自启①（反复改→trace_run / 大库浏览→search_code）②（工作区探测→智能默认）、导出改系统保存对话框，以及真机暴露并修掉的 PYTHONPATH/pytest假通过/BOM误报/面板不反映智能默认等。pyproject+CHANGELOG+DEVLOG+记忆已同步。全回归 Python 43 文件 + 前端 23。性能核验：日常无可感知影响（见下条）。**深度审计三缺口（委派浅/无 hooks/无大库检索）至此全部补齐。**

## 2026-06-24 — 性能核验（这批新功能对 hermes 性能的影响）

**结论：日常性能无可感知影响。** 实测（微基准 + 真 kimi 工程任务）：
- 固定开销全可忽略：工作区探测 30ms（仅绑定一次）、每步 nudge 检测 ~1µs、智能默认计算 0.5µs、无 hook 返回 None 零开销。
- **没有"每轮模型调用都加税"的项**——nudge/智能默认 µs 级、探测仅绑定时、hook/search_code/评分回炉都按需才跑。
- 唯一实质开销＝**「改完跑定向测试」每次编辑 ~0.3s**（只跑那一个受影响测试文件、非整套；只在有测试项目+改的文件有对应测试时）。真实任务里 1 次编辑=0.3s/总44s < 1%，远小于模型往返(5–8s/次)。
- 真正"贵"的是**委派评分回炉**（每次委派多几次模型调用），但默认关、按需开。

## 2026-06-24 — 情境自启②：工作区探测 → 智能默认（零基础用户无需配置）

**阶段**：情境自启第②层（内置自动行为可情境自动开，🛠 面板降级为"覆盖默认"）。**状态**：纯逻辑+探测+端到端集成全过；纯后端 + 一个前端透明提示。

**做了什么**：绑定/切换工作区时探测项目特征，自动给内置行为设智能默认。新 `src/agentcore/profile.py`：
- `detect_project_profile`（浅扫：有无测试 / 代码文件数 / 是否 git，带上限）+ `compute_smart_defaults`（纯逻辑）+ `describe_smart_defaults`（给用户的一句话）。
- **当前规则**：检测到**有测试** → 自动开「改完跑定向测试」（最高价值：零基础用户打开带测试的项目就有即时对错信号）。
- 接入 `conversation._refresh_smart_defaults`（`_build_registry` 时按工作区算一次、缓存）；`_make_verifier` 读**有效值** = config/面板显式开 **或** 智能默认；发 `smart_default` 事件 → 前端 toast 告知（"检测到本项目有测试，已自动开启…，可在 🛠 功能开关里关闭"）。
- **三条铁律**：①不覆盖用户面板显式选择（feature_flags.json 有该键就让位）②只加不减（绝不替用户关）③透明（自动开了告知一句）。

**端到端验证**：有测试的工作区 + config 关 + 无面板选择 → 智能默认开「改完跑测试」→ 改坏文件自动回灌 🧪（零配置即生效）；无测试 → 不开；面板设过 → 尊重不动。

**自检**：`test_profile.py` 9/9（有测试开/无测试不开/尊重面板选择/已开不重复/探测跳噪声目录）；全回归 Python 43 文件 + 前端 23。

**Windows 真机暴露并修掉（2026-06-24，又一个"真测才知道"）**：用户开带测试的项目，toast 弹了（智能默认确实开了开关），但改坏代码没回灌 🧪。根因＝**运行器假通过**：hermes 不依赖 pytest、用户机没装，runner=auto 退回"独立脚本"跑 `python test_x.py`，而 **pytest 风格的 `def test_*()` 当脚本跑函数根本不执行 → 退出码 0 → 假通过**（智能默认是对的，被运行器骗了）。三修：①pyproject 加 `pytest>=7` 依赖（auto 总有 pytest、根治，需重跑 `pip install -e .`）②防呆 `is_pytest_style`：没 pytest 又是 pytest 风格测试 → **明确报"需装 pytest"而非静默通过**（绝不再假通过）③`test_affected_tests.py` +2。
**回答用户第二问（中途出现测试文件能否自动识别）**：原只在工作区绑定时探测一次→不能。已修：`_make_verifier` 闭包里，当**新建/改的是测试文件**且智能默认尚未开启时，失效缓存重探测→自动开启（开发中途冒出 `tests/` 也即时生效、不必重开项目）。

**第二轮 Windows 真机暴露并修掉（2026-06-24）**：①**BOM 误报**——PowerShell `Set-Content -Encoding utf8` 给文件加 UTF-8 BOM(U+FEFF)，`verify_text` 的 `ast.parse` 把 BOM 当非法字符误报「语法错误」，且语法校验失败会短路、定向测试根本没跑（用户以为没自动开）。**其实带 BOM 的源码合法**(Python/pytest 都能跑)。修：`verify_text` 先剥开头 BOM 再 parse（`text[:1]=="﻿"`）；真语法错仍报。+回归测试。②**面板不反映智能默认**——智能默认开的是运行时行为，面板读 config 静态值显示"关"，对不上。修：`api.get_feature_flags` 返回**有效值**(config 或当前会话 `_smart_defaults`)+`auto_affected_test_smart` 标记，前端显示「🤖（已按本项目自动开启）」。
**情境自启进度**：①模型自调工具（反复失败→提示 trace_run）✅；②内置行为情境自动开 ✅（首条规则 auto_affected_test）。③hooks 维持逃生口。

**追加规则（情境自启①，2026-06-24）：大库浏览太多 → 提示用 search_code**。来自真跑发现（kimi 在多文件库逐个 list/read/grep、没用 search_code）。`loop.py` 的 `detect_browse_nudge`：项目代码文件 ≥ `search_nudge_files`(默认40) 且本轮累计浏览（read/list/grep/glob/outline）≥6 次仍没用 search_code → 注入提示劝其按意图检索；每轮提一次、用过 search_code 即不提。conversation 据 `_profile.n_code_files` 决定是否启用、传入主/子 loop。`test_stuck.py` +4（共 10/10）+ 确定性 loop 集成（假 provider 浏览 6 文件→search_hint 触发并回灌）。`stuck_hint`/`search_hint` 是给模型的内部转向信号、不在 UI 显示（`smart_default` 那种用户向的才显示）。全回归 Python 43 + 前端 23。

## 2026-06-24 — 情境自启①：反复改不好 → 自动提示用 trace_run（产品哲学：少让用户操作）

**阶段**：用户提的产品方向——hooks/trace_run 等仍需手动配/调，对零基础用户有门槛；hermes 应**自己判断何时触发**。三层拆解：①模型自调的工具（用户从不配，问题是模型该用没用）②内置自动行为（可情境自动开）③自定义 hooks（power-user 逃生口，不自动）。**首攻①的最小切片**。

**做了什么**：在 `agent/loop.py` 加情境检测——**同一文件被改 ≥N 次且本步仍有失败信号**（且环境有 trace_run）时，自动把一条提示注入模型下一轮回灌，劝它停止盲改、用 trace_run 看中间值定位。纯逻辑 `detect_stuck_edit`/`looks_failing` + 接入 run() 的回灌注入 + 发 `stuck_hint` 事件。config `stuck_edit_threshold`（默认 3，0=关）。主/子 Agent 都生效。**零用户配置**。
- **设计取舍（诚实）**：自动**执行** trace_run 需猜"测哪个函数、喂什么输入"（hermes 不知道）→ 故做自动**提示**（hermes 判时机、模型补具体），不硬替它跑。每文件只提一次、避免打扰。

**三层验证**：①`test_stuck.py` 6/6（到阈值+失败才提示 / 成功不提示 / 无 trace_run 不提示 / 阈值0关 / 各文件独立计数）；②**确定性 loop 集成**（脚本化假 provider 强制"同文件改3次都失败"→ stuck_hint 真触发、nudge 含 trace_run+文件名、**确实注入进模型下一轮回灌**）；③**真 kimi**：造同文件两隐蔽 bug 想逼它卡，但 kimi **一次性 multi_edit 修好两 bug、还主动用了 capture_fixture**——**没卡→nudge 正确沉默**。

**印证元规律**：nudge 是给"卡住瞎改"兜底的安全网，强模型在可读小代码上根本不卡（再次验证"专用能力价值在大到读不动/盲不可见时"）；且 kimi 主动用 capture_fixture 说明 A/F 调试 directive 真在起效。**自动行为做对的关键＝触发条件准（宁可少提示也别乱打扰），而非"敢不敢自动"。**

**自检**：全回归 Python 42 文件 + 前端 23。下一步（情境自启②）：工作区绑定时探测项目/测试/规模，自动设 auto_affected_test 等的智能默认（面板降级为"覆盖默认"）。

## 2026-06-24 — 大库相关性检索 search_code（对标 Windsurf RAG，深度审计缺口③）

**阶段**：深度审计缺口③（grep/symbol 够不到「按意图找代码」，大型陌生库读不动全部、喂不进上下文——正是"长任务反复改不好"的根上一环）。**状态**：纯逻辑+端到端测全过；**真跑验证（工具本身准、模型在小库会绕开）**。

**设计取舍**：真·向量语义检索要引入嵌入模型+网络+按库索引，破坏 hermes「无新依赖/离线/按需扫」一贯原则、且对代码（满是精确标识符）边际收益不如散文。故做**关系排序检索**（BM25 + 代码感知分块 + 标识符切词），零依赖、离线、按需无缓存（同 codeindex）。**诚实命名**为"相关性检索"非神经语义。
**做了什么**：新 `src/agentcore/retrieval.py`：`tokenize`（拆 snake/camel + **中文二元组**）/ `chunk_source`（按顶层符号体切、无符号滑窗、复用 codeindex 抽取）/ `Bm25` / `rank_chunks` 纯逻辑 + `search_code` 遍历 IO（MAX_FILES=800 护栏）。新工具 `search_code`（只读、免 gate、researcher/reviewer 可用）。系统提示加「大/陌生库按意图定位先用它」引导。

**真跑暴露并修掉（又一个"真测才知道"）**：①`tokenize` 只抽 ASCII → **中文查询/注释全被丢**，纯中文「折扣计算」查不到（对中文用户是硬伤）。修：加 **CJK 二元组**（「分级折扣」→分级/级折/折扣），中文查询与中文 docstring 都可检索、真跑「订单退款 退回款项」→ 准确命中 `reverse_charge`（与查询无共同标识符、靠中文 docstring）。②**真 kimi 在 7 文件小库上没选 search_code**（用 outline+read+grep 也定位对了）——同 trace_run：强模型在小库绕开新工具，其价值在真·大库。故加引导 + 明确"小项目不必检索"。

**自检**：`test_retrieval.py` 12/12（tokenize 含中文、chunk、BM25 排序、端到端按中/英意图命中、无匹配兜底）；全回归 Python 41 + 前端 23。
**待 Windows 验**：大项目里问「在哪处理 X」，看模型用 search_code 定位（或直接调工具验准确度）。
**已知局限**：相关性检索非神经语义；要更强的跨语言近义/语义泛化，需另接嵌入模型（按需，会破零依赖）。

## 2026-06-24 — 可编程 hooks（对标 Claude Code PreToolUse/PostToolUse、Windsurf Cascade Hooks）

**阶段**：深度审计缺口②（auto_verify/test/review 全硬编码、用户加不了自定义守卫）。**状态**：纯逻辑+真子进程+经 `AgentLoop._exec_tool` 端到端测全过；**真 kimi 端到端验证通过**；GUI 无关、纯后端。

**做了什么**：把生命周期钩子做成用户可配——工具调用前/后跑用户命令。新 `src/agentcore/hooks.py`：
- `match_hooks`（事件+工具名正则匹配，坏正则跳过）/ `parse_pre_result`（退出码语义，沿用 Claude Code：**2=拦截/1=警告/0=放行**）纯逻辑；`HookRunner.pre/post` 子进程（stdin 收 `{event,tool,params,workspace[,result]}` JSON、cwd=工作区、超时、跑不起来 fail-open 不阻塞）。
- 接入 `agent/loop.py._exec_tool`：PreToolUse 在 gate 前跑（拦截→工具不执行、拒绝理由回灌；警告→并入结果）；PostToolUse 成功后把 stdout 追加回灌。`AgentLoop` 加 `hook_runner` 入参；`conversation` 的主/子循环都注入 `_make_hook_runner()`（无 hook 返回 None、零开销）。
- config 加 `HookConfig`（event/command/matcher/name/timeout）+ `agent.hooks: list`；config.yaml 带注释示例（扫密钥拦截 / 改完跑 ruff）。

**价值**：用户能自己加守卫/动作——写文件前扫密钥、保护某些文件不被改、edit 后跑项目 linter（之前砍掉的 H 轻量诊断，现在用户用一行 PostToolUse hook 即可自接，不必内置）。

**真跑确认**：真 kimi 改受保护文件 → PreToolUse hook 退出码 2 拦截 → 「⛔ 被 hook「保护文件」拦截」、**文件没落盘**、kimi 优雅适应改提建议。

**自检**：`test_hooks.py` 13/13（含经 `_exec_tool` 端到端：含 SECRET 的写入被拦不落盘、PostToolUse 追加 lint 输出）；全回归 Python 40 + 前端 23。
**待 Windows 验**：在 config.yaml 配一条 hook（如保护文件/扫密钥），真机确认拦截/警告/追加生效。

## 2026-06-24 — 委派评分回炉（借 Claude Code Performance Outcomes）

**阶段**：深度审计后首攻项（委派子 Agent 此前"跑一次取文本当摘要、无质量回路"是最大深度缺口）。**状态**：核心机制**真 kimi 端到端验证通过**；GUI 面板开关待 Windows 验。

**做了什么**：子 Agent 产出后，由 **lead 模型按验收标准评分**，不达标带具体反馈**打回、复用同一子循环上下文重做**，最多 `delegate_max_revisions` 轮（0=关，默认关）。
- `delegate.py` 纯逻辑：`build_grader_prompt` / `parse_grade`（首行 PASS/REVISE，歧义偏严）/ `summarize_activity`（提炼执行证据）。delegate 工具加 `acceptance`（验收标准）入参。
- `conversation.run_subagent` 加评分回炉循环 + `_grade_subagent`（lead 模型一次性短调用、无工具、异常一律判通过不卡死）。
- 接入「🛠 功能开关」面板（伪开关 delegate_grader → delegate_max_revisions 2/0）+ config 白名单持久化。

**真跑暴露并修掉的关键问题（这就是"要真测才知深浅"）**：
- 单测全过，但**真 kimi 跑出 grader 过严**：v1 只看摘要，把"声称完成但没在摘要里贴日志/diff"的**合理产出也判 REVISE**（它甚至挑出我示例摘要里的逻辑矛盾——挺聪明，但会误退正常工作）。
- **修法**：①校准 rubric——按"执行证据是否可信达成"判、明确"别因没贴日志就打回"；②**给 grader 喂 `summarize_activity` 的执行证据**（子 Agent 实际调了哪些工具+结果），据真实动作评而非只看自述。
- **重跑确认**：校准+证据后判别正确——真做了+证据→PASS、只动嘴无工具→REVISE（理由在点）。端到端：子 Agent 算对 active 均分 81.0、grader 首轮 PASS、产物正确。

**自检**：`test_delegate.py` 28/28（+parse_grade/build_grader_prompt/summarize_activity）；全回归 Python 39 + 前端 23。
**待 Windows 验**：面板开「委派评分回炉」后，真委派一个子任务能看到 grade 事件、不达标打回重做。
**已知局限/下一步**：grader 仍是"读证据判断"、不自己跑验证；更强版可让 grader 实际跑测试核验（Performance Outcomes 完整形态）——按需再说。

## 2026-06-23 — 设置面板加「🛠 功能开关」（隐藏能力做成按钮，免改配置文件）✅ 验证通过，定版 v3.40.0

**阶段**：易用性（用户提：`auto_affected_test` 每次改配置文件麻烦，做成按钮；顺带把其它默认关的隐藏能力摆出来）。**状态**：✅ **已 Windows 真机验证通过（2026-06-23，定版 v3.40.0）**——开关即时生效（开后不重启、改文件即跑定向测试）、auto_test 命令框显隐、重启后状态保留。

**做了什么**：
- **后端持久化**（复刻浏览器穿透的状态文件范式）：`config.py` 加 `feature_flags.json`（`read/set_feature_flags` + `merge_feature_flags` 在 load_config 覆盖 agent 默认）；白名单 `TOGGLEABLE_FLAGS`=auto_affected_test/auto_review/auto_test/affected_test_runner(+test_command)，**只接受白名单键**（防注入任意 config）。
- **即时生效**：`api.get_feature_flags`/`set_feature_flags` 直接改活动 `config.agent`（所有对话共享同一 config 引用）。auto_review/auto_test 本就每轮现读；**把 `conversation._make_verifier` 改成「每次调用现读 config」的稳定闭包**——让 auto_affected_test 也即时生效，**无需重建 registry**（重建会重置改动台账，故特意避开）。
- **前端**：设置面板左列表加「🛠 功能开关」特殊项（跟 🌐穿透/🎨外观 并列），右 `renderFeaturePane`：三个 checkbox 行（auto_affected_test/auto_review/auto_test）+ auto_test 开时显示「测试命令」输入框；即点即存即提示。新 `.feat-*` CSS。
- **范围**（与用户确认）：默认开的能力（auto_verify/web/截屏等）没必要关就不放按钮；MCP/vision 不是纯布尔不放。

**自检**：`tests/test_feature_flags.py` 9/9（持久化 roundtrip/合并不覆盖/拒未知键/test_command/坏档/merge 覆盖默认/创建 agent 段/合法 JSON）；全回归 Python 39 文件 + 前端 23；Playwright 渲染功能开关面板深浅双主题截图核对布局达标。

**待 Windows 验**：设置面板「🛠 功能开关」开/关三个开关即时生效（开 auto_affected_test 后改文件即跑定向测试）、auto_test 命令框显隐与保存、重启后开关状态仍在（feature_flags.json）。

## 2026-06-23 — P5 调试能力工程化（A/B/C/D/E/F 六件）✅ Windows 验证通过，定版 v3.39.0

**阶段**：P5 调试能力工程化（第一波 A/B/C/F + 第二波 D/E）。**状态**：✅ **已 Windows 真机验证通过（2026-06-23，定版 v3.39.0）**——
受影响测试即时回灌、trace_run 看中间值、报错定位、capture_fixture 固化复现、整套 debug 协同（复现→看证据→固化→验证）全部真机通过。仅剩第三波 G/H/I（按需）。

**起因**：讨论「怎么让 Agent 更高效 debug」。现状只有零成本语法校验（FR-11.2a `py_compile`/`node --check`），
缺**运行时对错信号 + 中间证据**——「每轮改完没即时对错信号」「盲调看不到中间数值」「bug 不可复现」。借鉴
Claude Code 工作流，立闭环「编辑→运行→看证据→定位→修」。

**A–I 能力 → 三波优先级（与用户定）**：
- 第一波（便宜立竿见影，prompt/directive + 轻扩展）：**A** 复现优先 + **B** traceback 定位 + **C** 编辑后跑定向测试 + **F** 调试便签。
- 第二波（核心，投入大但质变，补「运行时证据」）：**D** trace_run 插桩（最值得投入）+ **E** 失败固化 fixture。
- 第三波（锦上添花）：**G** debugger 子角色 + **H** 轻量诊断（曾撤 LSP，仅重提轻量 linter 探测）+ **I** 回归二分定位。

**落点已勘察**（每项都有现成扩展点）：C→`verify.py`（同形态零成本 hook）；F→`tools/notes.py`；G→`delegate.py` 的 `ROLES`；D 为新 tool。

**用户补充的真实靶心**：长项目后期 debug **反复「定位不准原因」、改半天改不好**。据此校准：三波按成本排，但最对症「定位不准/盲调」的是
**D 插桩 trace + A 复现 + B traceback**（给运行时证据去定位）；C 给的是「对错信号」（知道*还错着*、不直接说*错在哪*），是便宜该先有的地基。
**模型限制?** 定位不准分两层——①没数据瞎猜（**工程可解**，D/B/A 摘眼罩，是大头）②拿证据后的推理（吃模型，Claude 更准，是残差）。
故「接 Claude 叠加非替代、先补闭环最划算」成立。

**关键认知**：这些能力对**任何模型都加分**（弱模型少猜几轮、闭环+证据让弱模型不至瞎改）。

**推进顺序（用户定）**：**C → D(+A) → B/F/E → G/H/I**（把最对症的 D 从第二波拉到紧接 C，不等第一波全做完）。
**FR-13.C 进度（编辑后跑定向测试，已接入待 Win 验）**：
- 纯核心（`verify.py`）：`affected_tests`（按文件主名映射 test_<名>/<名>.test、改测试跑自己、无对应测试跳过、上限 MAX_AFFECTED=4）/
  `detect_test_argv`（auto 探测 pytest｜独立脚本｜node --test）/ `discover_test_files`（浅扫跳噪声）/ `make_affected_test_runner`（runner=auto/pytest/python）。
- 接入：`make_post_edit_checker` 把零成本语法校验 + 受影响测试**组合成单回调**（语法坏短路不跑测试），`conversation.py` 的
  `_make_verifier` 据 config 装配，**fs.py 不动**（复用现有 `_with_verify`）。config 加 `auto_affected_test`（默认关）+ `affected_test_runner`。
- 命名坑：原 `test_subject` 以 `test_` 开头会被测试收集器/pytest 误当用例 → 改名 `subject_of_test`。
- **真链路冒烟过**：build_registry + 组合校验器，写对的 add → 只「已写入」；写回 bug → 工具返回当场带「🧪 受影响测试未通过」+ traceback 回灌。
- **Windows 真机暴露并修掉的集成 bug（2026-06-23）**：`python tests/test_x.py` 跑时 `sys.path[0]` 是**测试文件所在目录**而非工作区根，
  裸 `from calc import add`（calc.py 在工作区根）→ `ModuleNotFoundError`（与代码对错无关，TC2 本应静默却报错）。修：跑测试的子进程注入
  `PYTHONPATH=工作区根(+src 若存在)` 让被测模块可 import；并加 `PYTHONDONTWRITEBYTECODE=1`（不污染用户工作区 `__pycache__`，
  且避免同秒重改命中 mtime 粒度的旧 `.pyc`）。新增 `_test_env`，+2 回归测试（工作区根 import / src 布局 import）。
- 自检：`tests/test_affected_tests.py` 24/24；全回归绿（Python 35 文件 + 前端 23）。
- **待 Windows 验**：开 `auto_affected_test: true`（本项目配 `affected_test_runner: python`），真跑改坏源码 → 看工具结果是否即时回灌受影响测试失败。

**FR-13.D（运行时值追踪 trace_run，新工具，已落待 Win 验）**——最对症「盲调看不到中间值」：
- 新工具 `tools/trace.py` `TraceRunTool`（dangerous 过 gate、registry 常驻、readonly 角色自动排除）。给一段驱动 `code`（import 目标+具体输入调用）+ 可选 `target` 聚焦；
  **子进程内 `sys.settrace` 记录工作区内函数每行的局部变量 + 返回值**，回传逐步中间值 + stdout + 崩溃前轨迹。
- **设计选择**：用户原议「往源码插 print 再还原」，改用 settrace——**零源码改动、无需还原、不会改坏文件、能拿全部局部变量**，更稳更全（目标一致）。
- 复用 `verify._test_env`（PYTHONPATH 注入根/src + 不写 pyc）；非工作区/site-packages 帧跳过；步数上限防爆量；异常被捕获连同崩溃前轨迹回传（trace 到崩点最有用）。
- 真实输出示例：循环里 `total` 逐步 1000→1080→1166→1259、`profit=259`、`↵ 返回 = 259`，一眼看出哪步算错。
- 自检 `tests/test_trace.py` 9/9（format 纯逻辑 + 端到端真追踪：中间值/崩溃前轨迹/target 聚焦/空code/坏import）。

**FR-13.A（复现优先，轻量 directive，已落）**：config.yaml 系统提示「跨工具协同」加一条**调试准则**——
①复现优先（固化现象+输入+期望/实际）②用 `trace_run` 看证据再定位（别盲改）③改完看受影响测试转绿；配 `update_notes` 记「已怀疑/已排除/证据」防重复死路。一条串起 A+D+C+F。

**FR-13.B（报错定位，已落）**：新 `src/agentcore/diagnose.py` 纯解析 traceback（`parse_traceback`/`pick_workspace_frame`/`enrich_traceback`）——
工具/命令输出含指向**工作区文件**的 traceback 时，挑最深一帧（真正崩的用户代码行）读盘摘 ±2 行上下文、带箭头标崩溃行 + 异常行，附加回灌。
接入 `run_shell`（主路径）+ FR-13.C 受影响测试失败输出。只读零行为改动、自身故障不影响工具结果。`test_diagnose.py` 10/10。
真机冒烟：崩溃脚本 → `📍 报错定位：buggy.py:3（in compute）` + 源码上下文。

**FR-13.F（调试便签，轻量按定范围）**：不另起表/工具，扩 `update_notes` 工具说明——明示 debug 时维护「## 调试便签」结构
（现象/假设/证据/已排除/下一步验证），跨轮不丢线索、不绕死路。配 config 调试 directive 强化。

**FR-13.E（失败固化 fixture，已落）**：新工具 `capture_fixture`（`tools/fixture.py`，dangerous 过 gate）——把触发 bug 的输入固化成
`tests/test_capture_<slug>.py`（标准头：现象/日期），**写完立刻跑一次确认「当前确实复现」**（现在应失败；当前就过会提示没复现）。
bug 变可复现 + 自动接入 FR-13.C 受影响测试闭环（修好转绿守回归）。坑：pytest 退出码 5（模块级 assert 全过、无用例收集）要当「通过」。`test_fixture.py` 6/6。
config 调试 directive 补「改前先 capture_fixture 固化复现」。

**本批全回归绿**：Python 38 文件（+test_trace/diagnose/fixture）+ 前端 23。feature 自测：C 24 / D 9 / B 10 / E 6。
**P5 进度**：第一波 A/B/C/F + 第二波 D/E 全部实现（=除第三波 G/H/I 外全落地）。打包 `hermes-dev-src-3.39.0dev-cdabfe-test.zip` 待 Windows 验。
**下一步**：Windows 验这批；第三波 G（debugger 子角色）/H（轻量诊断）/I（二分定位）按需再上。

## 2026-06-23 — 易用性优化 P4（Markdown 渲染增强 + 引用回复）（v3.37.0）

**阶段**：易用性优化 P4（用户圈定 2 项；「会话归档分组」讨论后不做、消息操作只取「引用回复」）。**状态**：✅ 已 Windows 真机验证通过（2026-06-23，**定版 v3.37.3**，含滚动回归修复）。

**Windows 验证 ✅**：markdown 表格/引用/任务列表/嵌套列表渲染、引用回复（assistant+user）均正常。**但暴露并修掉一个 WebView2 专属滚动回归**（详见 CLAUDE.md gotcha）：markdown 增强 CSS 让 WebView2 在复杂内容异步重排时把 `.chat` 的 `scrollHeight` 算塌→滚轮跳回顶部、几轮后自愈，Chromium 复现不出。用户精确隔离到「嵌套列表+hr」，逐版定位（3.37.1 去 :has + overflow-anchor:none、3.37.2 表格改 table-wrap div、3.37.3 hr 只改色 + 列表只用 padding）后 3.37.3 全好。教训：改对话区 CSS 必须真机滚长对话验。

**做了什么**：
1. **Markdown 渲染增强**（纯 CSS）：marked 用默认配置（gfm 已开能解析表格/任务列表），但 `.bubble` 下表格/引用块/分隔线/任务勾选框此前**无样式**。补：表格 border-collapse+描边+表头底色+偶数行斑马纹（`--hover-bg`）；blockquote 紫色左条+muted；hr 细线；嵌套列表间距。**任务列表坑**：本版 marked 输出无 class 的 `<li><input type=checkbox>`（实测确认），故用 `li:has(> input[type=checkbox])` 去项目符号+对齐勾选框（WebView2 为 evergreen Chromium 支持 :has）。
2. **引用回复**：`formatQuote(text,maxChars)` 纯逻辑（逐行加 `> `、末尾留空行、超 2000 字截断）。assistant 动作行加「引用」按钮（新 QUOTE_ICON，初版 lucide quote 在 14px 糊成「??」→换 reply 箭头）；用户消息悬停加引用按钮（`.msg-uquote` 在编辑按钮左侧）。`quoteToComposer` 把引用填入输入框（已有内容则追加空行后接）、聚焦移到末尾。

**自检**：前端纯逻辑 22→23（+formatQuote）；Python 34/34；Playwright 截图核对 markdown 富元素（表格/引用/任务列表/嵌套，深浅主题）+ 动作行三按钮图标。

**待 Windows 验**：表格/引用/任务列表渲染、引用按钮（assistant + user）把内容以 `>` 填入输入框并能续写。

## 2026-06-23 — 易用性优化 P3（@文件引用 + 跨会话搜索 + 会话置顶/草稿 + 三栏拖拽）（v3.36.0）

**阶段**：易用性优化 P3（4 项，逐段交付）。**状态**：Linux 全回归绿（前端 22/22 + Python 34/34）+ Playwright 截图自检；**待 Windows 真机验证**。

**做了什么（按段）**：
1. **@ 文件引用**：输入框打 `@` → 弹工作区文件树补全（新 `#mention-menu`，复用 `.slash-menu` 样式 + 同款 ↑↓/Enter/Tab/Esc 键盘交互）。纯逻辑 `findMentionQuery(text,caret)`（光标前连续 @token 才激活、@ 前须行首或空白防邮箱误触）/`matchFileMentions`/`flattenTreeFiles`。**方案 A**：选中插入 `@相对路径` 文本，agent 用自带 read_file 读最新版（轻、不撑上下文）。补全文件列表按 cid 懒加载缓存，`refreshWorkspace` 时失效（新文件可见）。
2. **跨会话全局搜索**：后端已有 `store.search_messages`（recall_history 同源），仅新增 `Api.search_messages` 桥接（空查询/异常安全、limit 30）。前端侧栏搜索框 ≥2 字防抖(200ms)查询，结果列在 `#msg-search`（标题+角色+命中片段高亮），点击 `selectSession` 跳转。
3. **会话置顶**：DB sessions 加 `pinned` 列（`_migrate` 轻量迁移）+ `set_session_pinned`（不动 updated_at），`list_sessions` 改 `ORDER BY pinned DESC, updated_at DESC`；前端会话项加 📌 按钮（置顶常驻显示、未置顶灰钉 hover 出现）。
4. **切会话保留草稿**：view 加 `draft` 字段，`mountView` 切走前存当前输入、切入时还原（内存态、各会话隔离）。
5. **三栏宽度可拖拽**：`#drag-left`/`#drag-right` 分隔条（flex 子项），`clampWidth` 夹取上下限 + flex-basis + localStorage；拖拽时 `body.col-resizing` 屏蔽 iframe 事件防丢；工作区收起时右条隐藏。

**自检**：前端纯逻辑 18→22（+findMentionQuery/matchFileMentions/flattenTreeFiles/clampWidth）；Python 34/34（+test_search_messages_api、test_session_pinned_ordering）；Playwright 截图核对 @菜单/全局搜索结果/置顶图钉（深浅主题）。

**待 Windows 验**：@ 补全弹出与插入、跨会话搜索跳转、置顶排序与持久、切会话草稿还原、三栏拖拽调宽与记忆（拖到 iframe 上不丢）。

## 2026-06-23 — 易用性优化 P2（浅色主题/字号 + 快捷键面板 + 工具输出折叠 + 累计用量）+ 浏览器穿透 GBK 修复（v3.35.0）

**阶段**：易用性优化 P2（4 项，逐段交付）。**状态**：✅ 已 Windows 真机验证通过（2026-06-23，**定版 v3.35.0**）。

**做了什么（按段）**：
1. **浅色主题 + 字号**：设置面板加「🎨 外观」特殊项（沿用 Provider 中心的左列表+右详情骨架）。主题=跟随系统/深色/浅色，字号=小/中/大；`pure.js` 加 `resolveTheme`/`normFontSize` 纯逻辑，`applyAppearance()` 立即应用（避免闪烁）+ `matchMedia` 监听系统明暗。CSS 把硬编码表面色收敛成变量（`--bg-deep/--code-bg/--code-fg/--hover/--border-strong/--active/--font-base`），加 `html[data-theme=light]` 覆盖块 + `html[data-font=*]` 字号档位。
2. **快捷键帮助面板**：`?`/`Ctrl+/` 开、`Esc`/遮罩关；`isHelpKey` 纯逻辑（打字时 `?` 不触发的输入框判断在 app.js）。分组照实列现有快捷键（改键时记得同步 `SHORTCUT_GROUPS`）。
3. **超长工具输出折叠**：`foldToolOutput(text,maxLines,maxChars)` 纯逻辑（默认 20 行/2000 字）；`renderToolResult` 折叠时显示预览+`…`，开关置于结果框外（不被 `max-height` 滚动区盖住），展开态才加 `tr-expanded` 滚动上限。
4. **会话累计用量芯片**：view 加 `usage` 字段，`accumulateUsage` 逐轮累加，顶栏 `#usage-chip` 显 `Σ tok ≈ $`，切会话刷新。成本 `estimateCostUsd` 用 `MODEL_PRICING`（USD 公开价**粗估**、按模型名子串匹配、未知模型不显成本、缓存读按输入价 10% 折算），UI 明确标「≈/粗估」。**局限**：累计为「本次打开会话以来」，重启 app 重置（历史回放不带 usage）。
- **顺带修**：浏览器穿透安装的 `subprocess.Popen` 加 `encoding="utf-8", errors="replace"` + 按 `\r/\n` 切行——修 Windows 中文环境 GBK 解码崩溃（启用失败）并让进度可显示。

**关键决策**：代码块/工具结果等「类终端」表面在浅色主题下**刻意保持暗底**（配 vendored github-dark 高亮），仅给固定浅色字 `--code-fg` 保证可读；行内代码改用随主题的 `--panel-2`（深字浅底）。成本估算选「前端价目表粗估 + 诚实标注」而非接后端账单（厂商计费 CNY、口径各异，粗估够用且零后端改动）。

**自检**：前端纯逻辑 node:test 12→18（+resolveTheme/normFontSize/isHelpKey/foldToolOutput/accumulateUsage/estimateCostUsd）；Python 34/34；Playwright 渲染深/浅双主题 + 快捷键面板 + 折叠态 + 用量芯片截图核对（抓到并修了浅色对比度 bug、折叠按钮被滚动区盖住 bug）。

**Windows 验证 ✅ 通过**（2026-06-23）：外观切换/记忆/跟随系统、? 面板、折叠展开、用量芯片悬停、浏览器穿透真能装上不再 GBK 报错。折叠功能用户复核：默认收起+滚动已足够、超长 dump 折叠更省 DOM，保留不改。

## 2026-06-22 — 产品化③：设置面板重构为 Provider 中心（「完善建议」第 2 条收官）

用户给参考图（Cherry Studio）+ 指出「内置模型档案没体现价值、同 provider 重复配 key/url」。重构为 provider
中心：provider 配一次 key/url/格式、下挂多个模型。**分 4 步、每步本机真验**：
① 预设 + `expand_provider_profiles`（provider 配置 → 扁平档案、喂 build_provider 不动核心）+ test_providers；
② load_config 接入（providers.yaml 展开合并）+ `get_providers` / `save_provider` API + 真验；
③ provider 中心 UI（左列表 + 右详情：key/url/格式/模型勾选/+自定义），**用 Playwright 渲染 preview 截图
   自检**、对标参考图达标；
④ 迁移收口：config.yaml 内置 models 清空、active_model→`volcengine-ark/kimi-k2.6`、providers.yaml 不存在时
   默认启用火山方舟（开箱即用）；**真跑 kimi 确认 provider 展开档案能真调通**（回复"你好"）。

5 个内置预设：火山方舟（ARK_API_KEY，下挂 kimi-k2.6/deepseek-v4-pro/doubao/glm/minimax）/ Anthropic /
OpenAI / DeepSeek / Kimi-Moonshot（api.moonshot.cn）。用户档案存 providers.yaml + user_models.yaml、不写
config.yaml，打包排除这俩（运行时生成）。⚙ 按钮去常驻背景框。test_providers 8/8 + 全回归绿。

**第 2 条产品化至此收官**（去 key+key 配置 v3.28 / 模型档案 GUI v3.29 / Provider 中心 v3.30）；仅剩自动更新（按需）。
已 Windows 验通过（含 v3.30.1 真机修复批次，正式定版）：provider 列表 / 启用开关 / 填 key 即时生效 / 改 url / 模型勾选 / 加模型 / 加自定义 provider /
顶部下拉显示 provider·model / 切换可用。

## 2026-06-22 — 产品化第二步：模型档案 GUI 增删改（「完善建议」第 2 条）

承接 key 配置。这步让用户在设置面板里加 / 改 / 删模型档案，不用碰 config.yaml。

写回策略（关键决策）：用户档案存独立 `user_models.yaml`，load_config 时与内置 config.yaml models 合并
（用户覆盖同名）。这样不碰 config.yaml 的大量注释 / system_prompt、无新依赖、内置档案只读——比改 yaml
结构化 models 段（保留注释难）干净得多。

后端（本机完整真验）：config.py 加 merge_models / load_user_models / save_user_models（纯函数）；api.py 加
get_model_profiles（列档案 + 标内置 / 用户）/ upsert_model_profile（校验 ModelConfig → 写 user_models →
重载合并的 models 即时生效）/ delete_model_profile（内置不可删；删的若是当前主模型则回退）。test_model_profiles
3/3；真跑验证：写用户档案 → load_config → my-test 出现在 models（字段全对）、内置保留。

前端（写好、待 Windows 验）：设置面板「模型档案」区 = 列表（内置 / 自定义标记）+ 编辑 / 删除 + 「添加自定义
模型」表单（provider 下拉 + 各字段 + vision 勾选）；保存调 upsert、删调 delete，之后刷新列表 + 顶部模型下拉
（抽出 refreshModelDropdowns 复用、pywebviewready 也改用它）。表单校验纯逻辑 validateModelProfile 抽进
pure.js 单测（前端 12/12）。

待 Windows 验：添加自定义模型 → 顶部下拉出现可选用 / 编辑 / 删除 / 内置档案只读无删除钮 / 校验拦非法输入。

**设置面板 UI 重设计（同批，并入 3.29.0）**：用户验功能 OK 但反馈旧版（单列堆叠）太丑。改为对标主流
（Claude Desktop / Linear）的**左 tab 导航 + 右内容**布局：API Key / 模型档案两个 tab；key / 模型卡片化、
状态用 pill、表单两列网格对齐、input focus 高亮、统一按钮；沿用 hermes 深色主题 + 紫 accent + 留白 + 轻
投影。**关键做法：用 Playwright 渲染 preview 页截图自检视觉**（开发机无显示，靠截图确认对齐 / 配色 / 层次，
不再盲改 CSS）——顺带印证第 1 条「前端可测性」的延伸价值：连 UI 都能在开发机先看到效果。

**第 2 条仅剩自动更新**（ROI 低、坑多，按共识放最后、待用户定要不要做）。

## 2026-06-21 — 产品化第一步：API Key 配置面板 + 去内置 key（「完善建议」第 2 条）

让 hermes 从「自用工具」往「可分发产品」走的硬门槛之一：配 key 靠改 .env、打包含真实 key（外发会泄露）。
这步做「设置面板填 key + 分发包不带真实 key」。

后端（本机完整真验）：config.py 加纯函数 `collect_key_requirements`（从模型档案收集需要的 env 名、去重并
关联模型）、`upsert_env_line`（按行写回 .env、保留注释，纯函数）、`mask_key`（掩码预览、不回传明文）；
api.py 加 `get_api_key_status`（列各 key：用途模型 / 是否已设 / 掩码）、`set_api_key`（写回 exe 旁 .env +
即时更新 os.environ、无需重启）。复用现有 os.getenv←load_dotenv 机制。test_config_keys 7/7；真实
config.yaml 上验证：5 个 env 正确分组，ARK_API_KEY 显示 set=True、掩码 ark-…6f6b（不泄露明文）。

前端（已 Windows 验通过）：topbar 加 ⚙ 设置入口 + 模态面板（每个 key：env 名 / 用途模型 / 已配置状态 /
密码输入框 / 保存）；保存调 set_api_key 即时生效；首次启动所有 key 未配置时自动弹面板引导（纯逻辑
`needsKeySetup` 抽进 pure.js 单测）。前端纯逻辑测 11/11、语法 OK。

去内置 key：pack.py 加 `--dist` 模式——分发包的 .env 用 .env.example 空模板占位（已验证：分发包无真实
key、自用包仍含 key 对照）。**打包约定更新**：给别人的用 `python pack.py --dist`（去 key、可安全外发），
自用仍 `python pack.py`（含 key、省得手填）。

待 Windows 验：设置面板开关 / 填 key 保存即时生效 / 掩码显示 / 首次引导 / 留空清除；分发包首次启动引导。
**下一阶段（第 2 条剩余）**：模型档案 GUI 增删改、自动更新。

## 2026-06-21 — 前端补可测性：抽 pure.js + 首组前端单测（「完善建议」第 1 条）

背景：复盘 hermes 最脆弱处＝GUI 层（这次会话两次栽 pywebview 跨线程），而前端**零自动化测试**、纯逻辑
埋在 app.js 的 DOM 渲染函数里测不了，每个 GUI bug 都要 Windows 真机往返。第 1 条＝给前端补可测性。

做法（渐进、低风险，不引入 node_modules/构建）：新建 `web/pure.js` 收纳**可脱离 DOM 的纯逻辑**（UMD：
浏览器先于 app.js 加载挂全局、Node 可 require），`tests/web/pure.test.js` 用 node:test 零依赖单测，
`node --test tests/web/*.test.js` 纳入「全回归」（CLAUDE.md 已记）。**优先抽出过 bug 的逻辑**：
sessionRowClasses（会话行 running/awaiting/unread 优先级）、isBusyState、composerState（运行中按钮态）、
computeTaskProgress、sessionTitleMatches（会话搜索）、matchSlashCommands / parseSlashInput（命令菜单），
共 9 个纯函数、10 个用例全过；app.js 对应 7 处重构成「纯决策 + 薄 DOM 应用」。语法 OK、Python 全回归绿。

**方向决策（与用户确认）**：不上 Playwright e2e——hermes 的 GUI bug 多是 pywebview 跨线程特性，普通
浏览器 e2e 复现不了、却重且维护贵；继续做厚纯逻辑单测 ROI 更高。**待 Windows 验**：动了 app.js/index.html，
跑一下确认 UI 没坏（工具块摘要/转义、会话行状态、运行中按钮、命令菜单、任务进度照常）。

## 2026-06-20 — 深度调研 Phase B：researcher 升级为深度调研员 + 浏览器下钻（本机真跑通过）

承接 Phase A（接通 Playwright MCP）。Phase B **不造引擎**，复用 hermes 已有的 researcher 角色 +
delegate 并行 + crazy 循环 + notes 证据载体，缺口只在「下钻方法论」和「让 researcher 能用浏览器」：
- **researcher directive 升级为深度调研方法论**：拆子问题→逐层下钻不止步一级页面（聚合→明细→分布→
  定性）→优先用 browser_* 站内导航（先 snapshot 看带 ref 结构、再 click 下钻）→多源交叉印证→综合带
  来源。config.yaml 主 Agent 准则也加了对应「深度调研」段。
- **放行浏览器工具**：Role 加 allow_browse + _BROWSE_TOOLS 白名单（导航/点击/输入/翻页/截图等浏览类，
  按去 server 前缀的基名匹配、server 名可任意），排除 evaluate/run_code_unsafe/file_upload/fill_form
  等高风险；仅 researcher 放行。加单测，test_delegate 18/18、全回归全绿。

**本机端到端真跑（kimi + Playwright MCP，非 mock）**：给「列表页第一条作者→点进 about 详情页→读出生
信息」的下钻任务，kimi 工具序列正是 navigate → snapshot（看 ref 结构）→ click(ref=e15) → snapshot
（读详情），答出 Albert Einstein, Born March 14 1879 in Ulm, Germany——证明「方法论 + 浏览器工具」真能
驱动逐层下钻、不浮于一级页面。命题成立。**已 Windows 真机验通过（①②③⑤ 全过），定版 v3.27.0**。

## 2026-06-20 — 深度调研能力 Phase A：接 Playwright MCP（已 Windows 真机验通过）

**背景**：用户指出包括 hermes 在内的多数 agent 调研「浮于表面」——只能搜 + 读一级页面，无法像人一样
顺着目标在平台内逐层下钻（如「抖音日用瓷哪品牌哪款销量最好」要在蝉妈妈逐层钻品类→品牌→单品→评价）。
联网调研结论：Deep Research 类（公开网页迭代深挖）已成标配但跨不进登录/交互式垂直平台；
browser-use / computer-use（WebVoyager 89%、Claude OSWorld 44%）是突破口但脆、反爬/CAPTCHA 仍是墙
（OpenAI Operator 2025-08 因此关停）。关键发现：**Playwright MCP 本身是个 MCP server，而 hermes 已有
MCP 客户端**——近乎零核心改动就能让 Agent「逛网页」。

**Phase A 选定「先接 Playwright MCP 验证可行性」并本机端到端真跑**（Linux，node v24）：hermes 的 mcp
客户端连 @playwright/mcp → 列出 23 个工具（navigate/click/snapshot/fill_form/type/take_screenshot…）→
真实导航 example.com 拿到标题 → browser_snapshot 返回**带 ref 的无障碍结构树**（`link "Learn more"
[ref=e6]`），Agent 据此决定点哪个 ref 往下钻、且非视觉模型也能用。可行性证实。

**顺手修**：`manager.py` 的 `from mcp import …` 原在 try 之外，SDK 没装时不报错而是卡到 connect_timeout
超时（误导）。改为 start() 先一次性探 SDK 友好跳过 + _serve 的 import 纳入 try。

**配置落地**：config.yaml mcp.servers 加 Playwright 示例（装浏览器命令、--browser chromium、trust 取舍、
登录平台用 storage-state 复用登录态的提示）。默认仍关。全回归全绿。

**待 Windows 真机验**：装 node + `npx playwright install chromium`、解注释开 browser server、跑一个真实
下钻案例。**下一步（Phase B）**：研究循环 + 下钻方法论提示 + 证据工作台（边查边落盘 + 交叉验证），把
「能逛网页」升级成「像人一样有目标地深挖 + 综合」——这块我这边纯后端能 headless 真跑验证。

## 2026-06-20 — 修关程序转圈不关（v3.26.2）

**bug**：v3.26.1 的「拦截关闭 + 遮罩 + 后台 `window.destroy()`」里，`destroy()` 跨线程在 pywebview 上
不生效 → 窗口没关、遮罩一直转，用户等 1min+ 还卡在「请稍候」；几句话的新会话也卡——根因不是整理慢，
是窗口压根没关。又栽在 pywebview 跨线程 GUI（无显示环境测不到）。

**修**：弃用跨线程 destroy。窗口正常关（移除 closing 拦截 + 遮罩 evaluate_js），`start()` 返回后再后台
整理 + `join(5)` 封顶，然后 `api.close()`。不卡、不丢（upto 成功才推进，超时/被杀下次切换会话补），
代价是关程序时不再显示「整理中」提示（窗口已关，提示无处可放）。

**记忆整理为什么慢 / 答用户**：整理 = 抽取（把整段对话喂模型读+生成）这一次 LLM 调用，用的是主对话大模型
kimi；固化触发时再加一次归纳 LLM。切换会话整理同理慢，但走后台 daemon、程序还开着、不阻塞操作；多会话各自
线程整理互不阻塞，但共用同一 kimi API，并发多了 API 侧会排队。主流 agent（Claude Code）不做自动 LLM 整理
（靠人工 CLAUDE.md + 主动 /compact），所以关得快——hermes 的自动记忆是差异化特色，代价就是这点 LLM 时间。
**下一步优化**：整理改用快/小模型档案（复用 summary_model 思路，新增 `MemoryConfig.capture_model`，
空=当前模型），抽取/固化都是轻提炼任务、不需要大模型。

## 2026-06-17 — 修关程序卡 + 不丢记忆 + 隐藏发送（v3.26.1）

**用户反馈（都点中要害）**：
1. 运行中「发送」按钮多余——能输入就默认能发，只留「停止」。
2. v3.26.0 的关程序卡（同步整理阻塞退出），且第一次久后面快——后面没触发整理吗？
3. 超时强制关会丢记忆吗？有更好方案吗？

**原理解释**：
- 第一次久后面快 = **正确机制**：整理只在「有新消息没抽取」(`total > extracted_upto`) 时触发；后面没聊新内容
  → 跳过 → 快。不是漏整理。
- v3.26.0 卡：close 同步跑 capture_sync（LLM）阻塞退出。
- 超时丢记忆：`extracted_upto` 在抽取**前**占位，超时被杀/失败 → 进度推进了但没抽完 → 丢。

**做了什么**：
- **优雅关闭**：close 不再同步整理；app.py 加 `window.events.closing` 钩子——关窗时后台 capture_sync +
  前端 `__memoryFlushing` 遮罩（spinner，LLM 无精确进度）+ 完成/超时(20s)`window.destroy()`。不卡。
- **不丢（方案 A）**：`extracted_upto` 改为**抽取+固化成功后才推进**；防并发改用 Resources `capturing` 标志
  （finally 必清，防失败卡死）。超时/失败 → 进度不动 → 下次重试。
- **隐藏发送**：updateComposerButtons 运行中 `sendBtn.hidden = running`（Enter 仍可发 steering）。

**自检**：core 全回归绿（capture 逻辑）；js node --check 过。**app.py 的 closing 流程 + 前端遮罩 GUI 待 Windows
真机验**（开发环境无 webview、无显示，跑不了）。

---

## 2026-06-17 — 停止立即响应 + 关程序整理记忆（v3.26.0）

**两个用户问题**：
1. 停止不立刻——主流点停止基本立即停。
2. 离线记忆整理触发时机：直接关程序会整理吗？还是必须切换会话才整理？

**原理（核实）**：
- 停止「回合间生效」：loop 只在每回合边界（line 69，max_steps 循环顶）检查 cancel，模型流式（line 81）和
  工具执行进行中不中断，要等当前这步跑完。
- 记忆整理：`capture_async` 只挂在 `_leave(capture=True)`，即新建/切换会话时触发；`close`（关程序）没调它，
  且 capture 是 daemon 后台线程、关程序会被杀——所以**直接关程序、没切换过会话，最后一段对话不整理、记忆丢**。

**做了什么**：
- 停止立即：loop 的 stream_chat 循环内加 `cancel.is_set()` 检查 → 立即 break 断流；保留已输出文本、不执行
  本轮残缺工具。（边界：执行中的长命令仍需等结束，中断进程需 kill，未做。）
- 关程序整理：新增 `capture_sync`（同步版抽取+固化，不起线程）；`api.close` 开头先 `self.active.capture_sync()`
  flush 一次（try 保护、不挡退出）。

**自检**：import + 全回归绿。

**验证状态**：待 Windows——① 模型吐字时点停止立即停；② 直接关程序（不切换会话）后，最后一段对话也进了记忆。

---

## 2026-06-17 — 固化按主题聚类 + UI 去排队（v3.25.2）

**背景**：用户指正——主题聚类属于**记忆结构设计**，该提早做（过程中还能观察模型聚类效果），不是「规模大才做」的优化。
另：确认离线记忆整理是自动的（capture_async 抽取后挂 _maybe_consolidate，离开会话自动跑）；用户建议去掉「排队」按钮。

**做了什么**：
- **主题聚类**：build_consolidate_request 的 prompt 改为「先按主题归类（模型自拟主题）→ 每主题提炼一条原则」，
  content 带【主题】标注。利用模型擅长归类的能力（而非代码层聚类算法），结构清晰且可观察。
  真机验证：10 碎片 → 模型自拟【会话状态同步】【测试真实性】【模型选型】【crazy 自主模式】【授权与权限】5 主题，
  各一条融合该主题多碎片的原则——聚类合理、可观察。
- **UI 去排队**：updateComposerButtons 发送按钮文案运行中由「排队」改为统一「发送」；运行中发的消息仍走
  steering（纳入当前任务），只是不再用「排队」措辞吓退用户。对标主流：进行中只有「停止」。

**自检**：全回归绿 + 真机端到端；js node --check 过。

**记忆系统至此**：写入→固化(按主题聚类+重算替换去重老化)→读取(框架原则常驻/碎片相关性/recall_history 原文兜底)，
三层递进、框架按主题组织。深化方向基本做完。

---

## 2026-06-17 — principle 去重/老化：重算替换（v3.25.1）

**背景**：用户认同的记忆深化①——principle 去重/老化，防框架层膨胀。原 `_maybe_consolidate` 是「add 新
principle 累积」，多次固化会越堆越多、重复。

**做了什么**：改为**重算替换**——固化产出新原则后（build_consolidate_request 已传旧原则、让模型参考合并去重），
删掉所有旧 principle 再存新的精简集。principle 数稳定、自动去重、不再相关的旧原则被自然淘汰（老化）。
（顺序安全：先确认有新原则才删旧，模型没产出就不动。）

**真机验证**（真 kimi）：threshold=6，第1批 6 fact → 6 principle；再加 6 fact（共 12）触发第2次 → **仍 6
principle**（替换非累积到 12），且 6 条融合了两批经验（记忆分层/任务形态/风险兜底/跨层状态/上限续命/headless 真跑）。

**自检**：全回归绿 + 真机端到端。

**深化②（固化按主题聚类）评估**：当前碎片量（几十条）全量喂模型一次归纳没问题；聚类是**碎片规模很大
（几百条超 token）时**的优化，非当务之急，待真有规模再做。

---

## 2026-06-17 — recall_history：原始对话检索（类人记忆 L3，v3.25.0）

**背景**：用户补充思路——细节的终极来源是**无损的原始对话记录**，模型需要时去搜；记忆手段应**递进**：
框架(principle)+碎片(fact)解决不了，再进一步搜聊天记录。hermes 已把会话持久化在 SQLite，缺「可检索 + 工具」。

**做了什么**：
- `store.search_messages(query, limit)`：跨会话关键词检索 messages（content LIKE 任一词，JOIN sessions 带标题），
  提取纯文本片段返回。
- `tools/recall.py` `RecallHistoryTool`（只读、parallel_safe）：description 强调**递进/最后兜底**——
  先用已注入记忆，不够才搜原文；query 取关键术语。
- registry 加 `history_search` 参数 + 注册；conversation `_build_registry` 传 `store.search_messages`（主 Agent）。

**记忆分层（递进下钻，对标人脑）**：L1 框架原则常驻(索引) → L2 碎片按相关性检索(中层) → L3 recall_history
搜原始对话(无损精确细节)。框架做索引、原文做召回，细节不再依赖有损抽取。

**自检**：test_conversation 72/72（跨会话检索命中 + 工具格式化含标题原文 + 空结果兜底）+ 全回归绿。

**下一步（用户认同的两个深化）**：① principle 去重/老化（防框架层膨胀）；② 固化触发更智能（按主题聚类而非全量碎片）。

---

## 2026-06-17 — 记忆固化：碎片→框架，类人记忆闭环（类人记忆 第2步，v3.24.0）

**背景**：用户指出 agent 缺「提炼归纳结构化」——人脑记框架、据框架重建细节。三环：写入(已有)→固化(碎片→框架)
→读取(框架常驻+细节检索)。原型已验证「固化」可行，v3.23.0 做了读取端。本版做写入端固化、闭合三环。

**做了什么**：
- `principle` kind（memory KINDS + longmem label）；`MemoryConfig.auto_consolidate / consolidate_threshold`；
  Resources `consolidated_facts` 记账（防重复固化）。
- `build_consolidate_request`（longmem 纯逻辑）：碎片+已有原则 → 让模型归纳框架原则（参考去重）。
- `_maybe_consolidate`（conversation）：capture_async 抽取后挂载——fact 攒够 threshold 且较上次新增够 →
  离线让模型固化成 principle 存入（原 fact 保留作细节）。
- `_recall_memories` 的 pinned 加 `principle`：框架原则优先常驻。

**真机验证**（真 kimi，临时记忆库）：塞 10 条碎片 → 触发固化 → 产出 7 条高质量框架原则（如「测试注入与端到端
验证必须贴近真实入口…绕过核心流程或纯模拟均会掩盖链路缺陷」正是这次 enqueue 坑的提炼）；召回 query 优先返回 principle。

**真跑又暴露一个集成 bug**：`parse_memories` 要 `{"memories":[...]}` 包装，固化 prompt 原让模型输出裸数组 →
`isinstance(obj, dict)` 失败、解析全丢、principle 空。改 prompt 匹配既有格式后通。再次印证：**真跑才暴露集成层坑**。

**自检**：test_conversation 71/71（框架原则常驻 + 固化请求构造 + 分层召回）+ 全回归绿；两段真机端到端跑通。

**整体**：记忆三环闭合——写入(抽取)→固化(碎片→框架，离线)→读取(框架常驻+细节检索)。这是「越用越聪明」的基础设施，
也是主流 agent 普遍没做好的方向。下一步可选：principle 也参与去重/老化；固化触发更智能（按主题聚类而非全量）。

---

## 2026-06-17 — 记忆分层召回（类人记忆 第1步，v3.23.0）

**背景**：用户提出 agent 记忆短 + 缺「提炼归纳结构化」（人脑记框架、据框架重建细节）。方案分三环：写入(已有)→
固化(碎片→框架)→读取(框架常驻+细节检索)。原型已用真 kimi 验证「固化」可行（10 碎片→5 条高质量框架原则）。
本版做读取端第 1 步。

**现状**：`_effective_system` 注入记忆用 `memory.list(最近 N 条全量)`，不按任务相关性；`search` 是整句
LIKE，拿整条消息几乎命中不了。

**做了什么**：`_recall_memories(query, limit)` 分层召回——user/preference 稳定事实常驻(pinned) + 其余按
**Python 层词重叠相关性** top-k（不引入向量/外部依赖，回退最近 N 条）；`_budget` 用最近一条 user 消息当 query
喂进去。改注入那一处，不动存储。

**自检**：test_conversation 69/69（新增「相关项召回 + 用户事实常驻 + 不相关不注入」）+ 全回归绿。

**下一步（第 2 步：写入端固化）**：攒够 N 条 → 离线让模型把碎片归纳成「框架原则」存为高优先级 kind →
`_recall_memories` 让框架优先常驻。原型逻辑已验证可直接接入。

---

## 2026-06-17 — crazy 期间补充走 steering 的根因修复（v3.22.2，真机验出）

**关键过程**：用户要求「直接用 kimi 真实跑一轮验证」。先证实——核心 agent loop 可 **headless 真跑**
（load_config + Api(cfg, emit) + run_autonomous；ARK_API_KEY 有效、Bash dangerouslyDisableSandbox 能出网；
之前误以为「跑不了真实模型」是把 GUI 限制错误泛化了）。

**真机暴露的根因**：核实 enqueue 时发现——`busy = _running_turn.is_set()`，而 `_running_turn` 只有 worker 设；
crazy 用 run_autonomous 直接调 send_message、不经 worker，故 crazy 期间 `_running_turn` 为空 → enqueue 判「空闲」
→ 补充被**排队 + 另起 worker 并发**（而非 steering 注入）。**v3.22.1 改的 _take_injects 路径在真实 crazy 里压根
没被走到**（之前单测手动塞 _inject 绕过 enqueue，误导性地全绿）。

**修复**：`busy = self._running_turn.is_set() or self.crazy_mode`。

**真机验证**（真 kimi，临时工作区）：crazy 做命令行通讯录，第1轮跑着时 enqueue「加 export 子命令」→
打印 `steering: True`（修复生效）；产物 contacts.py 顶部含 export、代码 import csv（补充被并入）；crazy 跑到
第2轮 pytest 验证才 goal_reached（没被补充打断夭折）。kimi 还自己处理了 pytest 没装。

**自检**：test_conversation 68/68（新增「crazy 期间 enqueue 走 steering、不排队」）+ 全回归绿。

**教训**：单测绕过真实入口（手动塞 _inject）会掩盖路径级 bug；关键交互要 headless 真跑端到端验。

---

## 2026-06-17 — crazy 下用户中途补充不再误判完成（v3.22.1）

**问题（用户实测）**：crazy 跑时（上一轮还有 python scanner.py 长命令在跑），用户排队补充了两个问题；agent
回复完补充就以为工作结束、crazy 提前收工，原任务（含那条长命令的结果）被丢下。

**根因**：用户补充经 `_inject`→`_take_injects` 作为 steering 注入成 `[用户追加] xxx`。crazy 下模型把它当
对话插话，回复后输出 `[[DONE]]`，外层误判 goal_reached。本质是 steering 的「对话感」与 crazy 自评打架。

**做了什么**：① `_take_injects` 在 crazy_mode 时把补充包装成「这是对自主任务的追加需求，并入目标继续干到底、
不要因为答复了就 [[DONE]]」，并置 `_last_turn_had_inject`；② run_autonomous：`verdict==done` 仅在 `not hit
and not had_inject` 时才收工；本轮有补充（或撞上限）即便 DONE 也强制续一轮确认。每轮开头重置 had_inject。

**自检**：test_conversation 67/67（新增「本轮有用户补充+DONE→续到第2轮才真收工」）+ 全回归绿。

**验证状态**：待 Windows——crazy 跑时中途补充需求，确认它把补充并入继续干、不会"回一句就收工"。

---

## 2026-06-17 — crazy 撞步数上限强制续命（v3.22.0）

**问题（用户实测）**：crazy 跑到「⚠ 已达到最大步数上限（40），已基于已收集信息收尾」后，**没输出 [[DONE]]
也直接结束、报告目标达成**。

**根因**：run_autonomous 解析每轮末尾标记决定续命/收工，但**不区分本轮是不是撞上限被截断的**。撞上限会触发
强制收尾（v3.21.0），收尾总结若含/被解析成 `[[DONE]]` → 误判 goal_reached 收工，而撞上限其实＝被步数截断、
没干完。且 send_message 的主循环没把 loop.hit_max_steps 暴露给外层。

**做了什么**：① send_message 主 loop.run 后存 `self._last_turn_hit_max = loop.hit_max_steps`；
② run_autonomous：`verdict==done and not hit` 才收工；撞上限（hit）的轮即便有 done 也强制续命
（nxt 缺省为「上一轮被步数截断、继续推进剩余工作」）。这样撞上限后**必进下一轮**，直到模型主动收尾或触预算。

**自检**：test_conversation 66/66（新增「撞上限+收尾DONE→强制续到第2轮才真收工」）+ 全回归绿。

**验证状态**：待 Windows——crazy 撞步数上限后，确认它继续干下一轮（而非误报达成结束）。

---

## 2026-06-16 — 运行中改标题空闲后自动补同步工作区名（v3.21.6）

**背景**：用户用控制台日志自查发现——「跳过：会话正在执行一轮」，即运行中改标题会跳过文件夹重命名（设计：
避免占用/丢文件）。但此前是**静默跳过**，用户以为"改名失效"，且不手动再改一次文件夹名就永久和标题对不上。
用户选了"自动补同步"。

**做了什么**（跨层、6 处）：
- conversation：worker 收尾(队列空) + run_autonomous finally(crazy 结束) → emit 内部事件 `ws_settle`；
- api：`_pending_ws_renames` dict 记 sid→title；`_rename_session_workspace_dir` 跳过分支记 pending；
  `_emit` 拦截 `ws_settle`（不转前端）→ `_sync_pending_ws_rename` 在会话确实空闲时补做重命名。
- 效果：运行中/crazy 期间随便改标题，**任务一结束自动把文件夹名同步上**，用户无感、不必手动再改。

**自检**：test_conversation 65/65（新增「运行中改→记 pending→空闲补改」全链路）+ 全回归绿。

**验证状态**：待 Windows——运行中改标题，任务结束后看文件夹名是否自动变成新标题。

---

## 2026-06-16 — 修 crazy 期间改标题搬工作区致丢文件（v3.21.5）

**问题（用户实测 + 共同诊断）**：crazy 模式开发电子宠物，干到一半"工作区被重置"——之前创建的文件全没了、
list_dir 空。用户自己想到原因：首次工作区是数字名，他中途改了标题 → 文件夹被改名 → agent 找不到了。

**根因（代码印证）**：`_rename_session_workspace_dir` 改标题时会 `cur.rename(new_path)` 把 data/workspaces/<id>
重命名成标题名。有两道保护：① `_running_turn` 置位（运行中）跳过移动；② 移动后 `live.set_workspace(new_path)`
同步更新运行中 conv。**但 crazy 是后台线程、跨多轮**：`_running_turn` 只在单轮内置位，用户在**两轮空隙**改标题时
它为空 → rename 判定"空闲"去搬目录；叠加后台/主线程对 workspace 的并发，导致自主任务看到空目录、像被重置。

**修复**：rename 的跳过条件从「`_running_turn.is_set()`」扩为「`_running_turn.is_set() or crazy_mode`」——
**crazy 自主任务整个运行期间都锁住工作区目录**，标题照改、目录不搬，等 crazy 结束下次再改。

**自检**：test_conversation 64/64（新增「crazy 期间轮间改标题不搬目录」）+ 全回归绿。

**验证状态**：待 Windows——crazy 跑长任务期间改标题，确认文件不丢（目录留在原数字名、crazy 结束后再改名）。

---

## 2026-06-16 — 修子 agent 访问授权目录（v3.21.4）

**问题（用户实测）**：委派子任务调研授权目录（D:\…\workspaces\2）时，子 agent 报「工作区为空」——读成了
工作区、没去读授权目录。

**根因**：v3.20.2 只给主 agent 的 _effective_system 注入了授权目录，**漏了子 agent**。子 agent 的
_subagent_registry 其实传了 extra_dirs（工具能力 OK、能读），但 _subagent_system 没告知授权目录的存在/路径
→ 子 agent 不知道有它 → 默认读工作区（空）。与主 agent v3.20.2 的 bug 同源。

**做了什么**：_subagent_system 补上和 _effective_system 一样的 [额外授权目录] 注入块。

**自检**：test_conversation 63/63（新增「子 agent system 注入授权目录」断言）+ 全回归绿。

**验证状态**：待 Windows——委派子任务读授权目录时，确认子 agent 用完整路径去读、读到真实内容（而非报工作区为空）。

---

## 2026-06-16 — 任务清单引导：勤于更新 + 动态调整（v3.21.3）

**用户实测 + 追加**：v3.21.2 后子任务发现数据不全时，主 agent **会自行补充了**（强命令式标注 + 判断起作用）；
但**不会动态调整任务清单**（只打勾）。用户要求在清单引导里补「动态调整」，并指出**不限委派场景**——主 agent
自己执行有新发现也该调整，对标 Claude 的「勤于更新」。

**做了什么**：改 build_task_block（注入 system 的清单块）引导：从偏重「打勾推进」改为「**勤于更新** + 边做边维护 +
**任何新发现改变计划（自己执行 or 子任务结果）就 update_tasks 增删/重排、而非只打勾**」。通用、不绑委派。

**诚实**：又一句 prompt 引导——能提高 kimi 重规划的概率，但「勤不勤」上限仍是模型（Claude 的 TodoWrite 习惯
是训练出来的）。机制（动态整份替换、注入 system 抗压缩、delegated 状态）早已对标 TodoWrite、不输主流，差距在模型。

**自检**：全回归绿（build_task_block 是纯文本组装、改文案不破坏断言）。

**验证状态**：待 Windows——委派调研任务时看任务面板：新发现时清单是否**增删/重排**（而非纹丝不动只打勾）。

---

## 2026-06-16 — 「子任务未完成」标注改强命令式（v3.21.2）

**问题（用户实测）**：v3.21.1 给了「⚠未完成」温和标注，但主 agent 仍**没补充、直接把不完整子任务结果当完整
总结输出**。印证 v3.21.1 的诚实边界——机制给了信号，kimi 没用，是判断力问题。

**做了什么**：把 run_subagent 的未完成标注从温和建议改成**强命令式**：「不完整、不可直接当最终答案，必须
①查缺 ②补全 ③再给结论，禁止直接总结/判定完成」。

**诚实评估**：这仍是**治标**——加强信号能提高 kimi 照做的概率，但保证不了（模型判断力天花板，Claude 更可靠）。
实际缓解还可：调高 `agent.subagent_max_steps`（让调研类子任务少撞上限）。根治仍指向接强模型。

**附带澄清**：用户观察到 crazy「更主动思考」——核实 _CRAZY_DIRECTIVE 自 v3.19.1 未改，是模型输出随机波动，
非 prompt 优化（没贪功）。

**自检**：全回归绿。

**验证状态**：待 Windows 看强命令式标注后，主 agent 是否更倾向先补充再总结。

---

## 2026-06-16 — 委派子任务撞上限标注「未完成」（v3.21.1）

**问题（用户追问）**：撞上限后子任务拿回的信息不充分，主 agent 能判断并补充吗？现状：v3.21.0 已让子任务撞
上限也产出摘要（含「未完成部分」描述），但**没有结构化标注**——主 agent 只看到一段文本、可能当成完整结论。

**做了什么**：loop 暴露 `hit_max_steps`（run 开头重置、撞上限 else 分支置 True）；run_subagent 取摘要后，
若子循环撞了上限，给摘要**加显式前缀**：「⚠子任务未完成：撞步数上限，以下为部分成果，请判断是否够用、
不够就补充/换策略/自己接着做，别当最终结论」。让主 agent 拿到明确信号。

**诚实边界**：机制只负责给信号，**补不补最终取决于主 agent（模型）判断力**——kimi 不一定主动补，Claude 更
可靠。这和「撞上限强制收尾（v3.21.0）」配套：一个保证有产出、一个标注产出不完整。

**自检**：test_delegate 17/17（新增「撞上限置 hit_max_steps / 正常收尾不置」）+ 全回归绿。

**验证状态**：**待 Windows**：委派调研任务撞上限时，确认主 agent 收到的子任务结果带「未完成」标注、且会据此判断补充。

---

## 2026-06-16 — 撞步数上限强制收尾产出（v3.21.0）

**问题（用户实测）**：委派子任务调研时，子 agent 搜了 60+ 次（kimi 不收敛、换关键词搜同一件事）撞 max_steps，
loop 的 for...else 直接 emit「已达上限」终止——此时 messages 最后一条是 tool_result、无 assistant 文本，
extract_summary 取不到摘要 → 「子任务已结束，但没有产出文本摘要」→ 主 agent 裸退串行重做，搜索成果全废。

**两层问题**：① 机制——撞上限无强制收尾产出（本版修）；② 模型——kimi 不会收敛刹车（Claude 会更早主动总结，
这也是「主流靠模型自律」的底气）。

**做了什么**：loop.run 的 for...else（撞上限分支）加强制收尾：把收尾指令并入最后那条 user 消息（撞上限时
它一定是 tool_result，避免两条连续 user 破坏交替）→ 再调一轮模型、**tools=[] 禁用工具** → 强制基于已收集信息
产出文本总结 → 作为 assistant 追加。cancel 时不收尾；收尾异常不影响已有结果返回；token 照常累加。

**关键决策**：机制兜底补模型自律的缺失——比 Claude Code「纯靠模型自律」更鲁棒，适配 kimi 这种弱收敛模型。
（另：日志里大量 web_fetch「失败」是目标站反爬/超时，属另一问题，未处理。）

**自检**：test_delegate 16/16（新增「撞上限→最后是 assistant 总结、extract_summary 非空」）+ 全回归绿。

**验证状态**：**待 Windows**：再跑一次委派调研类任务，确认子任务撞上限时也能回灌一段有用摘要（而非空）。

---

## 2026-06-16 — add-dir 授权目录注入 system（根治，v3.20.2）

**现场信息定位到真因**：用户授权 `F:\小林屡战屡败` 后，① "读授权目录内容"→ agent 调工具但报「空目录」
（其实读成了工作区）；② "读 F:\小林屡战屡败"→ agent **没调工具**、直接文字臆测「不在授权范围」。

**真因**：授权信息只在后端 `_extra_dirs`，**从没告诉模型**——agent 不知道授权了哪个目录、也不知道要用完整
路径去读，于是读错（工作区）或臆测拒绝。后端 resolve 其实支持（绝对路径在授权目录内会放行，已复测）。

**修复**：`_effective_system` 注入已授权目录列表（每轮重新生成、含最新 add/remove），告知模型「用完整绝对
路径调 read_file/list_dir、在授权范围不会被拒、别臆测无权限、先实际调用工具」。这才是 add-dir 的最后一块拼图
——前面 cid 路由（v3.20.1）保证授权落到对的会话，这一版保证模型真的会去读。

**自检**：test_conversation 62/62（新增 system 注入断言）+ 全回归绿。

**验证状态**：**待 Windows**：授权 F:\… 后让 agent「读 F:\…\某文件」，确认它用完整路径调工具并读到内容
（若仍读成空目录，可能是 list_dir 对中文 Windows 路径的单独问题，回报再查）。

---

## 2026-06-16 — 修 add-dir cid 路由 + 自主模式停止按钮（v3.20.1）

**用户报三点**：① crazy 是否影响其他功能；② add-dir 当前会话授权后读不了、新会话才行；③ 自主模式没停止按钮。

**① 隔离**：crazy 是会话级状态（crazy_mode/gate._allow_all/_ask._auto 都按会话独立），run_autonomous 的
finally 必定 set_crazy_mode(False) + 恢复 self.emit——不影响其他会话/功能。已确认，无需改。

**② add-dir**：**后端复测无 bug**——脚本验证授权后同会话工具立即能读（extra_dirs 与 _extra_dirs 同一引用、
append 实时生效）。bug 在交互/路由层：api.add_dir 用 self.active，若与前端当前会话不一致就授权给错会话。
改为按 cid 路由（add_dir/remove_dir/get_extra_dirs + 前端传 v.cid）；/add-dir 反馈列全部已授权目录便于核对。
若仍复现需用户给现场细节（授权目录、agent 读时的工具与路径、报错）。

**③ 停止按钮**：crazy 走 start_autonomous 不经前端 send()，v.streaming 没置位。加独立 v.crazyRunning 态：
crazy_start→true、crazy_done→false，updateComposerButtons 的 running 纳入它；停止按钮点击判断也加 crazyRunning。
停止链路：stopBtn→stop_conversation→conv.stop()→_cancel.set()→run_autonomous 回合间退出（已通）。

**自检**：全回归绿（61/61 + 其余）；js 过。

**验证状态**：**待 Windows 真机**：① /crazy 运行时停止按钮出现且能中止；② add-dir 授权后同会话即时可读（若仍不行回报现场细节）。

---

## 2026-06-16 — crazy 模式护栏（无人值守安全，v3.20.0）

**背景**：crazy 三机制已真机验通（自评循环/多轮/委派），转做无人值守的安全护栏。用户拍板做三件：
预算上限、危险操作黑名单、防空转。

**做了什么**：
- 预算护栏（run_autonomous）：轮数 + 墙钟时间（crazy_max_seconds，deadline）+ 累计 token
  （crazy_max_tokens，期间临时包裹 self.emit 统计 usage 事件的 input+output）；任一超限回合间停，reason 细分。
- 危险黑名单（gate.py）：模块级 is_destructive(正则匹配 rm -rf/del /s/format/mkfs/dd/fork bomb/关机/强推/硬重置)；
  confirm 在 `_allow_all` 判断前拦——**免确认态下毁灭性命令仍 deny**。直接 return False，不走 _emit 权限通道
  （那会误触 awaiting 态卡住）。
- 防空转（run_autonomous + _turn_used_tools）：连续 crazy_stall_rounds 轮新增消息无 tool_use → stalled 停。
- 前端：crazy_done 的 reason 映射补 stalled/time_budget/token_budget。

**关键决策**：黑名单只在免确认态（crazy/全部允许）生效——正常确认流程用户自己把关，不改其行为；
黑名单只收毁灭性命令（几乎不会是正当开发命令），不做脆弱的全量 shell 路径围栏。

**自检**：test_conversation 61/61（新增 token 预算 / 空转 / 黑名单拦截 3 个；现有循环测试加「禁用空转」隔离）+ 全回归绿；js 过。

**验证状态**：护栏控制逻辑已单测；**真机串起来（超时/超 token 真停、rm -rf 真拦、纯文字打转真判空转）待 Windows 验**。

---

## 2026-06-16 — crazy 加委派引导 + 三次压测复盘（v3.19.1）

**三次真机压测结论（重要，修正判断）**：日志分析器 → roguelike×2 → minilang 解释器，kimi **全部一轮串行
搞定、带测试、能跑**，质量不低。连续打脸我「瓶颈在 kimi / 接 Claude 必需」的判断：kimi 对结构清晰、训练
充分的经典 CS 任务（roguelike、解释器都是教学经典）工程产出能力强，成熟模板化一气呵成。**委派/多轮始终
没触发 = 没触发条件**（kimi 自认一轮能搞定就串行干完，不自发拆/续命；委派对模型反直觉、默认串行）——非
机制坏。**结论：接 Claude 从「必需」降为「抬上限」，kimi 下限远高于先前判断。**

**做了什么**：给 `_CRAZY_DIRECTIVE` 加一句委派引导（任务多独立子系统时用 delegate 拆给子 agent 并行、自己
集成；同「别停在规划」思路），让真大任务能用上委派。靠模型遵守。

**自检**：全回归绿（directive 是常量、测试 mock send_message 不受影响）。

**待验（Windows 真机）**：① 加引导后跑 minilang，对比 v3.19.0 看 delegate 是否被触发；② 临时把
`agent.max_steps` 调小（如 8）跑 minilang，逼单轮截断 → 验证多轮 `[[CONTINUE]]` 续命真的 work。

---

## 2026-06-16 — crazy 自主模式骨架（无人值守，v3.19.0）

**背景**：用户看到 36kr 报道 Codex `/goal`（AI 自写目标、自派 agent、Ralph 循环 18h 无人值守），问 hermes/CC
能否做、能否要个 crazy 模式。分析：hermes 零件基本都有（loop / delegate / auto_test 验证闭环 / 规划），
差「自动目标 + 外层目标循环 + 全自动模式」。诚实结论：机制能搭，但价值 100% 绑模型——kimi 跑长自主循环
会跑偏/空转/hack，接 Claude 才真可用。用 ask_user 让用户拍板，选了「现在就搭框架」。

**做了什么（第一段：后端骨架 + 命令入口）**：
- `_CRAZY_DIRECTIVE`：引导模型先写可判定 GOAL、干到底、每轮末尾自评 `[[DONE]]` / `[[CONTINUE: 下一步]]`。
- `run_autonomous`（同步外层循环，复用 send_message 跑单轮 + 解析自评标记续命 / 收工，预算 crazy_max_rounds
  兜底、_cancel 可停）；`start_autonomous`（后台线程异步）；`_parse_crazy_verdict`（纯函数）。
- `set_crazy_mode` 联动：gate `_allow_all=True` 免确认 + AskUserBinding `set_auto(True)` 自动放行；收尾复位。
- 配置 `agent.crazy_max_rounds=20`；前端 `/crazy <目标>` 命令 + crazy_round/done 事件以 sys-line 展示。

**关键决策**：复用刚建的斜杠命令承载 crazy（重型低频走命令）；外层循环靠模型自评标记驱动（而非另调 LLM 判完成），
简洁可测；安全 = 工作区围栏 + 轮数预算 + 随时停；诚实标注「接 Claude 才真可用」。

**自检**：test_conversation 58/58（新增 5 个：标记解析 / 模式联动 / 循环到 DONE / 预算耗尽 / 中途停）+ 全回归绿；js 过。

**验证状态**：后端控制逻辑已单测；**真自主跑（goal 生成质量、是否跑偏、前端 /crazy 与进度展示、停止时机）
待 Windows 真机 + 接强模型后验**。

---

## 2026-06-16 — 斜杠命令机制 + 规划引导 ask_user（v3.18.0）

**背景**：用户质疑 add-dir 做成常驻按钮不合理——它低频，且按钮越来越多没统一管理。核实主流（Claude Code）：
低频操作走斜杠命令（/add-dir 等）、高频才上 UI，界面克制；hermes 之前全靠按钮、无命令机制。用 ask_user 让
用户在三方向间拍板，选了「建斜杠命令机制、移除 add-dir 按钮」。

**做了什么**：
- 前端斜杠命令机制：输入 / 唤起命令提示菜单（↑↓/Enter/Tab/Esc），数据驱动 SLASH_COMMANDS；首批
  /add-dir <目录>、/help；结果以 .sys-line 系统提示行显示。移除 ws-add-dir 按钮及 add_dir_dialog 绑定。
- 规划引导：ask_user 加进 _PLAN_TOOLS（否则规划模式过滤掉、调不了），directive 增一句——方向性取舍用
  ask_user 给选项、别纯文字问。
- 工具栏再做减法：移除「刷新」按钮（文件树本就自动刷新、用户从不点）；「折叠/展开」的 ×/📁 emoji 换成
  统一矢量 chevron（收起 »/拉出 «）。工具栏现仅剩一个折叠按钮，彻底告别 emoji 与字符混搭。

**关键决策**：低频走命令、高频留按钮；命令机制可扩展（加命令一行）。UI 克制——能自动的不放按钮、低频的走命令。

**自检**：test_conversation 53/53（新增「ask_user 在规划模式可用」断言）+ 全回归绿；js node --check 过、无残留引用。

**验证状态**：规划引导后端已验；**斜杠命令/菜单/sys-line 前端交互、/add-dir 真授权待 Windows 真机验**。

---

## 2026-06-15 — 引导 shell 不绕过工作区边界（v3.17.1）

**背景**：用户测 add-dir 时发现"授权前 run_powershell 就能读工作区外"，且"之前会提示没办法读"。核实：
add-dir 只约束文件工具（read_file 等的 resolve），**shell 工具一直能跑任意命令访问任意路径、不经 resolve**
（add-dir 没碰 shell——核实 shell.run 只用 self.workspace 当 cwd、不调 resolve/extra_dirs）。用户"之前
不能"是那次模型用 read_file 被拒、这次模型改用了 run_powershell——**是 kimi 换了工具，非限制变松**。

**做了什么**：给 RunShellTool 的 description 加引导：读文件用 read_file/list_dir（受工作区+授权约束）、
别用 shell 的 type/cat/Get-Content 读文件或访问工作区外。

**关键**：这是**引导**（靠模型遵守，kimi 未必每次听）；shell 的**硬控制**仍是权限确认（dangerous、默认
每次确认，别对 shell 点"全部允许"就能拦）。不做 shell 命令路径围栏（命令形态太多、脆弱，CC 也不这么做）。

**自检**：全回归绿。

---

## 2026-06-15 — ask_user 结构化提问（v3.17.0）

**背景**：用户指出 agent 问确认细节时只能纯文字让用户打字答；Claude Code 能给选项勾选 + 补充其他。
对标 Claude Code AskUserQuestion 的真实体验差距。

**做了什么**：`tools/ask.py`——AskUserBinding（阻塞桥：emit 事件 + threading.Event 等 resolve，同 gate 模式）
+ AskUserTool（question+options，run 阻塞等用户选）。registry 注册（ask_user_binding，只给主 Agent）；
Conversation `_ask`（emit "ask_user"）+ resolve_ask_user + stop 时 reset；api resolve_ask_user（按 cid）；
前端 renderAskUser（选项按钮 +「其他」输入框）。description 引导何时用（规划/方向性取舍、小事别问）。

**关键决策**：复用权限 gate 的成熟阻塞/resolve 模式；只给主 Agent（子 Agent 不直接问用户）。

**自检**：test_p3 18/18（ask 阻塞/resolve/空参报错）+ 全回归绿。

**验证状态**：后端已验；**前端选项勾选/补充交互待 Windows 真机验**。

---

## 2026-06-15 — 流式输出智能粘底（v3.16.1）

用户反馈：流式输出时往前翻看历史会被拽回底部，长会话体验差。根因：`scrollChat` 每个 chunk 无条件
`scrollTop=scrollHeight`。改：智能粘底——`stickBottom` 标志（chat scroll 监听更新），`scrollChat` 只在
已在底部时滚、往上翻就不拽；主动发送 / 切换对话用 `scrollChatForce` 强制到底 + 恢复粘底。对标主流
（Claude/ChatGPT 都这样）。纯前端，js OK，**待 Windows 验交互**。

---

## 2026-06-15 — 额外授权目录 add-dir（v3.16.0）

**背景**：用户问"能不能读别的文件夹"。核实（基于事实）：hermes `Tool.resolve` 硬限工作区
（`workspace not in p.parents` 拒），无 add-dir 机制；Claude Code 默认限 cwd 但支持 `/add-dir` 授权额外
目录。这是真实差距——跨文件夹场景（参考隔壁项目、读外部共享配置）做不到。

**做了什么**：`Tool` 加 `extra_dirs` 类属性 + resolve 放宽到"工作区或任一已授权目录"；build_registry
注入共享 extra_dirs 引用（add/remove 原地改、实时生效、无需重建 registry）；Conversation `_extra_dirs` +
add_dir/remove_dir/get_extra_dirs，主 + 子 registry 都传；api `add_dir_dialog`（复用 open_project 的
FOLDER_DIALOG）；前端 ws-header「＋📁」按钮。

**关键决策**：默认仍严格限工作区（安全不变），额外目录要**显式授权**（点按钮选）——对标 Claude Code 的
"有边界的跨目录"，不是无限制乱读。

**自检**：test_p3 17/17（未授权拒 / 授权可读 / 其它目录仍拒）+ test_conversation 52/52（add/remove 实时
生效）+ 全回归绿。

**验证状态**：后端已验；**前端「＋📁」选目录授权交互待 Windows 真机验**。

---

## 2026-06-15 — auto_test 失败类型区分（v3.15.2）

**背景**：用实跑验证 hermes 复杂工程能力——给 TODO 库加优先级 + 改测试 + auto_test 闭环，kimi **41s
完成、验证闭环真实有效**（失败→自修→通过、测试真过）。但实跑暴露：第 1 次 auto_test 失败根因是
`python` 命令在该环境找不到（只有 python3）、不是代码 bug，模型却去**系统造 python→python3 符号链接**
hack 环境。→ auto_test 不该把"命令没跑起来"当测试失败让模型瞎修。

**做了什么**：`_run_test_command` 带回 returncode；`_is_launch_failure`（rc 127/126/9009/-1 或输出含
command not found / not recognized / no such file）判"命令没跑起来"。`_auto_test_loop` 遇命令失败 →
emit config_error + error 提示用户、不进修复循环；只有断言失败才回灌修。前端 toast 区分「⚠ 命令没跑起来」。

**自检**：test_conversation 51/51（含命令错不进修复循环用例）+ 全回归绿。

**意义**：这是"先实跑再优化"的产物——真实任务暴露的真问题，不是脑补。比对标分析更扎实。

---

## 2026-06-15 — 修 .gitignore BOM（v3.15.1）

v3.15.0 的 gitignore 读用 utf-8，Windows 记事本存的 .gitignore 带 BOM → 第一行模式失效（用户实测
`debug.log` 仍被 grep 到，T3 未过）。改 `utf-8-sig` 自动去 BOM，加 BOM 回归用例（test_ignore 5/5）。
又一次 BOM 坑（`.env` 也踩过）——以后读 Windows 用户可能用记事本编辑的文本文件，一律 utf-8-sig。

---

## 2026-06-15 — 对标 Claude Code 4 项优化（v3.15.0）

**背景**：用户列 8 个对标 Claude Code 的潜在优化点。这次**吸取前两次"脑补 Claude Code 形态"的教训**，
逐项核实代码事实 + 客观判断主流实际，只做真差距：
- 真差距：① 工具并行（Claude Code 同轮多工具确实并发）、⑦ gitignore（grep/glob 默认尊重）；
- 小功能：⑥ 会话搜索、⑧ 快捷键；
- **对标不准/已不弱，砍掉**：② Apply 按钮（Cursor/IDE 功能非 Claude Code）、④ shell 流式（已有后台兜底）、
  ⑤ 记忆去重（hermes 自动抽取已比 Claude Code 手动 CLAUDE.md 更自动）；③ 项目级预批（用户评估没必要）。

**做了什么**：① 只读工具加 `parallel_safe`（复用 ThreadPoolExecutor）；⑦ 新增 `ignore.py` 轻量 gitignore
matcher，叠加到 build_tree+grep+glob；⑥ 会话列表搜索框+过滤；⑧ Ctrl+N/Shift+P/K 全局快捷键。

**关键**：先核实再动手——②④⑤ 核实后发现对标不准/已不弱就不跟，没再栽"脑补形态"的坑（这点是进步）。

**自检**：test_p3 16/16 + test_ignore 4/4 + 全回归全绿。

**验证状态**：✅ 全部 Windows 验证通过（并行 / gitignore / 会话搜索 / 快捷键；含 v3.15.1 的 .gitignore BOM 修复）。

---

## 2026-06-15 — auto_test 项目级 test_command（v3.14.1）

**背景**：v3.13.0 的 auto_test 用全局 test_command，用户在不同技术栈项目间切换时不灵活（这个 pytest、
那个 npm test）。**做了什么**：`config.read_project_config` 读工作区根 `.hermes.yaml`；
`_effective_test_command` 项目级优先、全局兜底——各项目工作区放 `.hermes.yaml` 写自己的 test_command 即可。
**自检**：test_conversation 50/50（含项目级覆盖/回退）+ 全回归绿。**✅ 已 Windows 验证通过（多项目各用各的 test_command）。**

---

## 2026-06-15 — 规划结构化呈现 + 面板默认折叠（v3.14.0）

**背景**：用户提两个体验点——① 改动区/检查点随内容增多挤压预览区；② 规划结果是大段文字、想要更清晰
的呈现（甘特图/脑图？）。讨论结论：不做审批闭环（取消规划模式即审批）；甘特图不适合 agent 规划（无
真实工期，会编造时间）；脑图/流程图更合适；任务清单联动很值；图先看效果再定去留。

**做了什么**：
- 点1 面板折叠：右侧「改动」「检查点」两区默认折叠（localStorage 记忆），点标题展开；「全部回退」按钮
  stopPropagation 不误触折叠。CSS `.collapsed .ws-chg-row{display:none}`。
- 点2 规划呈现：强化 `_PLAN_DIRECTIVE`——update_tasks 列有序步骤为主体、回复附 mermaid 图（mindmap/
  flowchart 按任务性质自动选）、关键决定写 notes。**复用已有 mermaid 渲染 + 任务栏，零新渲染设施**。

**关键决策**：发现 plan mode 早就引导用 update_tasks，只是 kimi 倾向堆文字——所以是"强化引导"而非新建。
图先实测再定：kimi 产出 9 项清单 + 合格 flowchart（自动选对类型、语法对、结构合理）→ **判定图理想，保留**。

**自检**：全回归全绿 + js OK；真模型实测规划产出（任务清单 + mermaid 质量均达标）。

**验证状态**：✅ 全部 Windows 验证通过（面板折叠 + 规划 mermaid 渲染 + 任务清单）。

---

## 2026-06-15 — 内联 diff + 验证闭环 + 模型身份修复（v3.13.0）

**背景**：对标 Claude Code 评估 hermes 下一步，结论是功能清单已追平，差距在体验深度 + 模型。用户选了
两件：A 对话流内联 diff（手感）、B 验证闭环（工程核心）。顺带修了"问模型身份答 Claude"的 bug。

**做了什么**：
- **A 内联 diff**：write/edit/multi_edit 产出本次 diff（`make_diff_block`/difflib），走 `ToolOutput` 的
  `type=diff` 块；`loop._emit_result` 提取给前端、`_exec_calls` 回灌时过滤——**diff 只给前端不喂模型**
  （省 token）。前端 `renderDiffBlock` 复用 `.diff-line` 配色内联展示。
- **B auto_test（FR-11.2c）**：`auto_test`+`test_command`+`test_max_iters` 配置；send_message 主轮后改过
  文件就 `_auto_test_loop`——跑测试、失败回灌输出 + 复用同一 loop 续跑修，限次、通过即止。沿用 auto_verify
  失败回灌 + auto_review 收尾时机。
- **模型身份**：`_effective_system` 注入当前档案真实 model id，避免按训练语料瞎答。

**关键决策**：① diff 不回灌模型（它自己知道改了啥，回灌冗余 + 耗 token）；② ToolOutput 加
`__eq__/__contains__/__str__` 按 text 比较，向后兼容旧测试而非改一堆断言；③ auto_test 命令来源用 config
配置（不自动探测，避免乱跑命令）、失败自动迭代修（限次，真闭环）——用户拍板 ①A + ②A。

**自检**：test_p3 15/15 + test_conversation 49/49 + 全回归全绿。

**验证状态**：✅ 全部 Windows 验证通过（内联 diff 渲染 + auto_test 闭环 + 模型身份）。

**遗留**：auto_test 续跑用主轮 system（不重新压缩），修复轮很长时可能超预算（修复轮通常短，可接受）。

---

## 2026-06-15 — 前端配置模型入口（v3.12.0）

**背景**：子 agent 已支持按角色/subagent_model 配模型，但选模型一直要改 config.yaml。用户提"该有前端
入口、体验升级"。确认范围（用户选 B）：主/子模型前端选 + 持久化，不碰 api_key / 档案 CRUD。

**做了什么**：顶部加「委派模型」下拉（含"跟随主模型"）。后端 `set_subagent_model`（内存即时生效，委派
读 `cfg.agent.subagent_model`）+ `set_active_model` 加持久化 + `get_models` 带 subagent。持久化
`persist_model_selection`：**按行正则替换**只动 active_model/subagent_model 两行，保留注释与多行
system_prompt（不整文件 yaml.dump）。

**关键决策**：持久化按行替换而非 yaml.dump——config.yaml 有大量注释 + 长 system_prompt，dump 会全毁。
用户专门担心影响全局 prompt，实测 diff 证明只动 2 行、system_prompt 零改动。

**自检**：test_model_select 5/5 + test_conversation 44/44（set_subagent 内存生效 + 校验）+ 全回归全绿；
真实 config.yaml 未被测试污染（persist 在测试中 patch 成 noop）。

**验证状态**：✅ 全部 Windows 验证通过（后端 + 持久化 + 前端两个下拉选择/持久化/重启保留）。

**遗留**：新增/编辑模型档案（provider/model/api_key/max_tokens）仍需改 config.yaml + .env（本次只做选择）。

---

## 2026-06-15 — 对话体验三连：steering 注入 + 排队竞态 + 改名联动工作区（v3.11.0~3.11.2）

**背景**：v3.11.0 把"对话排队"做成执行中可继续发消息。用户指出真实场景是"干活中途想起补充"，
而独立排队把补充当新任务、与当前任务割裂——Claude Code 的做法是 steering（在工具边界注入当前任务，
让模型重估调向）。遂升级。

**做了什么**：
- **v3.11.0 steering 注入**：`AgentLoop.run(take_injects=…)` 在工具结果回灌时把用户追加的纯文本附进
  *同一条* tool_result user 消息——模型下一轮即看到「工具结果+补充」据此调方向；带附件/空闲发=排队成
  新一轮；任务结束未消费的追加→兜底排队。同轮还做：委派触发词修复、简单咨询快速通道、max_steps 25→40、
  eval 扩 6 任务。
- **v3.11.1 修排队竞态**：「任务已结束、再发消息却提示已排队」——`enqueue` 在 put+启 worker *后* 才查
  state，新 worker 抢先把 state 改 running 致误判。改用 `_running_turn` 在 put 前 snapshot。+ 改会话标题
  联动重命名 `data/workspaces/<id>` 文件夹（纯标题、撞名加 id、非法字符转义；外部绑定/运行中不动）。
- **v3.11.2 修改名在 Windows 未生效**：避让判断 `is_busy()`→`_running_turn`（前者含 queued/awaiting/
  队列非空，过宽，会整个跳过改名）；rename 失败不再静默吞，加 stderr 诊断 `[rename_ws sid=…]`+对话提示。

**关键决策**：steering 用"附进 tool_result 同一条 user 消息"而非新增 user 消息——不破坏 user/assistant
交替、实现最稳。"是否在跑"统一用 `_running_turn`（精确），不用 is_busy（过宽会误判）。

**自检**：AgentLoop 注入×2 + enqueue 路由 + drain + stop + 空闲不误报 + 改名×5（搬移/撞名/外部不动/
非法字符/运行中不动）；全回归 43/43 绿。**steering 真模型实测**：任务"逐个读文件"跑动中追加"读完统计
总词数"，kimi 后续轮纳入并正确给出 9（担心的"kimi 中途处理不好"未发生）。

**验证状态**：✅ **Windows 全部验证通过**——steering S1/S3/S4、排队竞态（任务结束后发消息不再误提示
排队）、改名联动（关掉锁住目录的资源管理器后即生效；文件夹未改名是资源管理器占用的外因，非代码缺陷）。

**遗留**：未做"单独取消某条挂起消息"（停止清空全部）；改名联动只对今后改标题生效（现有数字文件夹不动）。

---

## 2026-06-13 — system_prompt 精简（对标 Claude Code 工程纪律）

**背景**：讨论"agent 自测前端"时引出"不该污染 prompt"的原则，回头审视 hermes 自己的全局
system_prompt——92 行/8248 字符，约 2/3 是"逐个工具的用法说明"，与每个工具的 description 重复
（双花 token + 长 prompt 稀释指令）。这是一路加 FR、每个 FR 往 prompt 塞一段引导堆出来的，
正是"功能堆叠"在 prompt 上的体现。

**决策**：遵循 Claude Code 纪律——工具怎么用靠 description；system_prompt 只放"跨工具的行为准则 +
协同策略"；项目特定靠 hermes.md。删掉读写/git/联网/进程/update_tasks/update_notes/delegate 角色清单/
记忆的逐工具教学；保留：身份、开发准则（先读后改/最小改动/贴合风格/可测/如实报告/简洁/确认/
hermes.md 优先）、几条 description 覆盖不到的协同策略（复杂任务先规划、多个独立大块并行委派
researcher、联网时机、长进程后台、git 先开分支、记忆分层、自动检查点告知）、可视化输出。

**做了什么**：config.yaml system_prompt 从 8248 → 1328 字符（砍 84%）。

**验证（关键：行为不能退化）**：eval 套件 4 任务**精简前后对比**——
bugfix/feature_git/comprehend/parallel **4/4 → 4/4 全保持**；两个"靠 prompt 引导的行为"都没掉：
parallel 仍自发并行委派 2 子任务、feature_git 仍守 git 礼仪自己开分支（且工具调用 19→14 更利落）。
全回归 32 套绿。证明砍掉的确实全是与 description 重复的冗余，能力零损失、每请求省 2000+ token。

---

## 2026-06-12 — P12 检查点重构：自动打点（方案A，对标 Claude Code/Cursor）

**阶段**：P12（检查点重构）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.10.0，P12 收官**。
Linux 全回归 32 套绿（test_conversation 34/34、test_checkpoint 3/3）+ 真模型端到端。
收尾两个 UI 微调（用户反馈）：删手动「＋ 存检查点」按钮（自动打点已覆盖）、「回到此处」改图标按钮
（回拨箭头+悬浮提示，宽度固定不再抖）。FR-12.2 诊断升级评估后撤销（ROI 低，详见 PRD「4.X 路线」）。

### 背景与决策
用户质疑 checkpoint/plan 的价值、问主流怎么做。盘点：plan mode 主流有（Claude Code Shift+Tab），保留；
checkpoint 主流也有但是**自动每步打点 + rewind**，hermes 旧设计"模型手动调工具"两个弱点（靠模型自觉常忘、
与 git/台账重叠）。定**方案A**：改自动打点。

### 做了什么
- 删模型 `checkpoint` 工具（tools/checkpoint.py、CheckpointBinding、registry 注册、system_prompt 指引）。
- `AgentConfig.auto_checkpoint`（默认 true）；`Conversation._on_change` 替换原 change_tracker——回合内
  每个文件**首次**改动前把旧内容累加进**同一个**检查点（`_turn_snap` 累计 + `_upsert_turn_checkpoint`
  首建后 update；任务/笔记定格于回合首改）；主/子 Agent 注册表共用此钩子；send_message 回合开始重置。
- store `update_checkpoint` + `prune_checkpoints`（自动留最近 30）；前端自动打点静默刷新、手动存才弹 toast。

### 自检（Linux）
- test_checkpoint 3/3（capture/restore、store 往返+级联、prune 留最近）；test_conversation 34/34
  （多文件回合一个检查点；回退撤销整回合＝新建文件删除 + 已有文件还原；模型无 checkpoint 工具）；全回归 32 套绿。
- **真模型端到端**：模型 edit 给 util.py 加 sub()，**全程没调用任何 checkpoint 工具**（已删），系统在
  edit 前自动打点（label="改动前 · <用户消息>"），用户 restore 精确还原到改动前原文。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.10.0）
- [x] 模型改文件→面板自动出现"改动前·<你的话>"（无需模型操作、无 toast）；「回到此处」撤销整回合
  （新建删除+已有还原）；多轮各有检查点；模型无 checkpoint 工具；删会话不残留。
- 收尾 UI（删手动存按钮 + 图标化回到此处）随定版一并发出。

---

## 2026-06-12 — P12 立项 + FR-12.1：provider 韧性（自动重试/退避）

**阶段**：P12（工程深度，FR-12.1，首攻）
**状态**：✅ 定版 v3.9.0（2026-06-12）。纯后端容错逻辑、跨平台一致；test_retry 8/8 离线覆盖
确定性逻辑 + 真模型冒烟正常路径无回归——**用户决定跳过 Windows 真机验证**（瞬时网络错误无法主动复现，
平台无关）。Linux 全回归 31 套绿。

### P12 立项依据（P11 收官实测）
kimi 驱动 hermes-dev 从零做表达式求值器：147s/22 步/8 文件，模块分离专业（递归下降 parser），
**我的独立对抗测试 28/28 全过**（优先级/负数/浮点/变量自引用/6 类错误）；唯一瑕疵：除零抛原生
ZeroDivisionError 而非领域异常（REPL 层已兜底，体验无碍）。结论：harness/工程闭环层已与成熟工具
同档、不再是瓶颈；剩余差距=模型 × 工程深度 × 生态。据此立项 P12 工程深度（PRD「4.X 路线」）：
12.1 provider 韧性 + 12.2 诊断升级（对标 Claude Code 复用环境 linter，不自造、不内嵌 LSP）。

### FR-12.1 做了什么
- `providers/base.py`：`is_transient_error`（status 408/409/425/429/5xx/529 + 异常名匹配
  APIConnectionError/RateLimitError/… + 消息启发式 timeout/overloaded/rate limit）；
  `backoff_delay`（指数退避 base*2^n + [0.5,1) 抖动、封顶 20s）；`retry_stream`（**仅在还没 yield
  任何事件时**重试瞬时错误，MAX_RETRIES=3，stderr 打重试日志）。
- openai_p：抽出 `_stream(kwargs)`（出错直接抛），stream_chat 用 retry_stream 包裹。
- anthropic_p：统一循环——cache_control 不被接受/开局即挂 → 摘缓存重试（不计退避预算）；
  瞬时错误 → 退避重试；吐内容后失败照常报错。与 FR-10.4b cache 降级、FR-11.8 usage 共存。

### 自检（Linux）
- `test_retry` 8/8（瞬时判定含 429/连接错/超时消息、非瞬时不判；退避平均递增且封顶；重试后成功；
  吐内容后不重试；非瞬时不重试；用尽抛出；anthropic 瞬时重试 3 次成功；cache 降级与瞬时重试共存且
  断点正确摘除）。全回归 31 套绿。真模型冒烟：正常调用 usage 照常（cache_read 7424），不受影响。

### 验证：跳过 Windows 真机（用户决定，2026-06-12）
纯后端容错、平台无关；瞬时网络错误无法主动复现；正常路径无回归已 Linux 真模型冒烟确认；
重试时机/退避/吐内容后不重试/cache 降级共存均由 test_retry 8/8 确定性覆盖。

---

## 2026-06-12 — P11 FR-11.8：用量可观测（P11 收官段）

**阶段**：P11（FR-11.8，最后一段）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.8.0，P11 重型任务工程化全部收官**。
Linux 全回归 29 套绿（test_p3 +2=11/11）+ 真模型冒烟。

### 决策
- provider 在 done.meta 带 usage：anthropic 全量（input/output/cache_read，实测方舟含缓存）；
  openai 尽力而为（自然带 usage 才取，不强加 stream_options 以免打挂不支持的端点）。
- AgentLoop 累加一轮各步 usage + 步数，回合末发 usage 事件（全 0 不发）；步数 ≥80% 上限发一次
  step_warning。**不内置美元定价表**（价格多变易过时）——只给客观 token/步数，成本用户按单价自算。

### 做了什么
- anthropic_p `_usage()` 规范化；openai_p 捕获 chunk.usage（prompt/completion_tokens）。
- agent/loop.py：total/steps 累计、usage 事件、step_warning。
- 前端 EV.USAGE/STEP_WARNING + renderUsage（usage-note 脚注）+ 预警 toast。
- cli.py：usage 进 --json 输出与 stderr 提示。

### 自检（Linux）
- test_p3 +2=11/11（两步累加 input 230/output 28/cache 50 + 步数/max_steps；端点静默不发 usage；
  step_warning 一次）；全回归 29 套绿 + node --check。
- **真模型冒烟**（CLI --json，ark-kimi）：input 4507 / output 231 / **cache_read 11136** / 3 步——
  token、缓存命中、步数全部如实回传。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.8.0，P11 收官）
- [x] 对话区用量脚注（token/缓存/步数）；接近步数上限弹预警 toast（临时调小 max_steps 复现）；
  CLI --json 含 usage；长会话缓存命中 >0；不回传 usage 的模型优雅留空。

---

## 2026-06-12 — P11 FR-11.7：CLI / headless 入口

**阶段**：P11（FR-11.7）
**状态**：✅ 定版 v3.7.0（2026-06-12）。Linux 全回归 29 套绿（新增 test_cli 7/7）+ 真实模型四模式实测。
验证期用户反馈 `python -m agentcore.cli` 未安装时 ModuleNotFound → 补根级 `run_cli.py` 免安装入口
（自动把 src 加进路径）；CLI 跨平台逻辑一致、与已验内核同源，其余模式跳过逐项重测。

### 决策
- 把评测 harness 产品化为 `hermes-cli`（`agentcore/cli.py` + console 脚本）。复用 GUI 同款内核
  （Api/Conversation），事件流打到终端。Api 加可选 `emit` 钩子替代 evaluate_js。
- 默认自动批准危险操作（gate._allow_all），**config deny 规则仍拦截**（gate 中 deny 优先于 _allow_all）；
  `--plan` 只读规划态最稳。助手文本→stdout、工具活动→stderr；`--json` 结尾一行结构化结果；退出码 0/1。

### 做了什么
- `bridge/api.py`：`Api(config, emit=None)`——注入则 Resources 用它推事件（无头）。
- `agentcore/cli.py`：argparse（prompt 位置/`-`/管道、-w/-m/--plan/--json/--quiet/--max-steps）+
  `_read_prompt` + `run`（构造 Api(emit) → set_plan_mode 或 _allow_all → send_message → 收集
  chunk/tool_use/subagent/error → 人类或 JSON 输出 → 退出码）。无头适配：shell 按平台、
  关 auto_conventions/screenshot。pyproject 加 `hermes-cli`；README 加「命令行/无头模式」。

### 自检（Linux）
- `test_cli` 7/7（prompt 解析；run 人类模式 stdout/stderr 分流+自动批准；JSON 单行；plan 置标志
  不自动批准；error 退 1；空 prompt 退 2）；全回归 29 套绿。
- **真实模型四模式实测**：①人类模式（答案进 stdout、工具进 stderr、退 0）；②--json（stdout 单行
  JSON 可解析）；③stdin 管道 + --plan（bad.py 未被改）；④非 plan 修改型（把 bad.py 补全右括号、退 0）。

### 入口可用性修复（验证期）
- ✗ 用户 Windows 跑 `python -m agentcore.cli` 报 `No module named agentcore.cli`——`-m` 形式要求
  agentcore 已在导入路径（pip install -e . 之后），未装时在项目根找不到（agentcore 在 src/ 下）。
- ✅ 修复：补根级 `run_cli.py`（对称 GUI 的 run.py，运行时把 src 插入 sys.path），免安装即可
  `python run_cli.py "任务" -w 项目`；README 明确三种入口前提（run_cli.py 免装 / hermes-cli 装后 /
  -m 装后）。CLI 各模式逻辑跨平台一致、与已验内核同源，跳过逐项 Windows 重测。

---

## 2026-06-12 — P11 FR-11.6：检查点/任务级回滚 + 子 Agent 重试（含委派引导改善）

**阶段**：P11（FR-11.6）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.6.0**。
Linux 全回归 28 套绿（新增 test_checkpoint 3/3、test_conversation +2=33/33）+ 真模型端到端。

### 决策
- 检查点 = {本对话经文件工具改过的文件当前内容 + 任务清单 + 工作笔记}快照存 DB（git 无关、与
  ledger 同口径）。模型用非危险工具 `checkpoint(label)` 在里程碑创建、前端「存检查点」也可手动建；
  **回退只由用户经前端确认**（模型无 restore 工具，防自抹成果）。
- 子 Agent 失败自动重试一次（11.6b）：子循环异常→附失败原因重试一次，仍失败才回灌（配置错不重试、
  取消不重试）。

### 做了什么
- db.py `checkpoints` 表 + add/list/get + 删会话级联；`checkpoints.py`（capture_files/make_payload/
  restore_files 纯逻辑：收集 ledger 文件当前内容、回写，新增文件回退=删除、幂等）。
- `tools/checkpoint.py`（CheckpointBinding + CheckpointTool 非危险只创建）；build_registry 注册
  （仅主 Agent）。Conversation：create/list/restore_checkpoint、run_subagent 加重试循环。
- Api：get_checkpoints/create_checkpoint/restore_checkpoint（restore 不进模型注册表）。
- 前端：工作区面板「检查点」区（列表 + ＋存检查点 + 回到此处 confirm + checkpoint_created toast）。
- config system_prompt 加检查点指引；**附带**改善 delegate 引导（多个独立大块→主动并行委派）。

### 自检（Linux）
- `test_checkpoint` 3/3；`test_conversation` +2=33/33（Api 建/回退还原文件+任务+笔记、模型无 restore；
  子 Agent 抛异常重试一次后成功）；全回归 28 套绿 + node --check。
- **真模型端到端**：模型给 calc.py 加 sub() 后**自发 checkpoint('加了sub')** → 文件被改坏 →
  api.restore 一键还原（含 sub、去掉坏内容）。
- **委派引导改善实测**：同一"三块独立调研"任务，改 system_prompt 后从 0 委派（主 Agent 串行 377s）
  变为 3 个 researcher 并行（222s，约 -40%）；中小任务仍 0 委派（10s）。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.6.0）
- [x] 模型里程碑建检查点 + 手动「存检查点」；「回到此处」一键还原文件+任务+笔记；
  模型无回退能力；删会话检查点不残留；委派体感（三块调研自发并行）；改动台账/git/规划/权限回归正常。

---

## 2026-06-12 — P11 FR-11.5：Plan mode（只读规划态）

**阶段**：P11（FR-11.5）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.5.0**。
Linux 全回归 26 套绿（test_conversation +1=31/31）+ 真模型端到端。
（验证反馈：规划按钮样式与 📎/发送不一致，随后统一为矢量图标按钮——见下条。）

### 决策
- 规划模式＝对话级运行时开关（不持久化、重启回默认关）。开启时本对话发消息走 `_PLAN_TOOLS`＝
  `_READ_ONLY_TOOLS`（复用 FR-9.5 限权）∪ {update_tasks, update_notes}——只读勘察 + 写计划，
  屏蔽写/命令/截图/记忆写/delegate/git 写/mcp。system 追加 `_PLAN_DIRECTIVE`。关掉＝转执行（全量工具）。

### 做了什么
- conversation.py：`plan_mode` 标志 + `set_plan_mode`；send_message 按 plan_mode 用
  `registry.filtered(in _PLAN_TOOLS)`；`_effective_system` 在 plan_mode 下插入 `_PLAN_DIRECTIVE`。
- `Api.set_plan_mode` 转发活动对话。前端：composer 加「📋 规划」按钮（按 cid 存 view.planMode、
  切会话在 mountView 同步、发送按钮文案随之变「规划」、body.plan-on 顶部提示条）；style 高亮+提示条。

### 自检（Linux）
- `test_conversation` +1=31/31（_PLAN_TOOLS 过滤：只读+update_tasks/notes 在、write/edit/multi_edit/
  run_bash/delegate/git_commit/screenshot 不在；set_plan_mode 切换 + system 注入与移除）；
  全回归 26 套绿 + node --check。
- **真模型端到端**：规划模式让模型"给 app.py 加 argparse" → 它只 list_dir/glob/read_file/git_status
  勘察 + update_tasks/update_notes 出计划、**app.py 零改动**；关闭规划模式说"按计划执行" →
  write_file 落地 argparse、跑 bash 验证。只读规划→确认→执行闭环成立。

### 待 Windows 验证
- [ ] 点亮「📋 规划」给一个改造需求：顶部出现规划提示条、发送按钮变「规划」；模型只勘察并产出
  任务清单/工作笔记、**不动任何文件**。
- [ ] 关掉「📋 规划」说"按计划执行"：正常改文件、跑命令。
- [ ] 开着规划模式时它若想写文件——写工具不可用（它会说改不了/继续规划）。
- [ ] 多对话：A 开规划、切到 B 默认关、切回 A 仍是规划态（按会话独立）。
- [ ] 回归：普通对话/任务清单/笔记/权限/git 正常。

---

## 2026-06-12 — P11 FR-11.3：上下文工程升级（工作笔记 + 可重读引用）

**阶段**：P11（FR-11.3）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.4.0**。
Linux 全回归 26 套绿（新增 test_notes 5/5）+ 真模型端到端。

### 决策（含对原②的重诠释）
- 11.3a 工作笔记外置：`update_notes`（整份替换，对标 update_tasks），存会话级、注入 system、
  抗压缩跨重启。任务清单=待办，工作笔记=已确认事实/决定/进展/坑，二者平行。
- **原②「清单项完成时主动压缩」重诠释**：不做脆弱的精确阶段切割（易破坏 tool 配对、与现有压缩
  重叠），改为「模型把阶段结论写进笔记 → 旧往返被压缩丢弃也不丢结论」，以更稳的方式达成目标。
- 11.3b 可重读引用：瘦身大 read_file 结果时标注来源文件 + "可用 read_file 重读"。

### 做了什么
- db.py `session_notes` 表 + set_notes/get_notes（删会话级联，与 session_tasks 同模式）。
- `tools/notes.py`：NotesBinding + UpdateNotesTool（整份替换、8000 字上限、空=清空）+
  build_notes_block 纯函数。build_registry 注册（仅主 Agent，子 Agent 注册表不含）。
- Conversation：注入 notes_binding、`_effective_system` 拼「[工作笔记]」（接在任务清单后）、
  `get_notes`、`Api.get_notes`、emit `notes_updated`。
- context.py：`_read_sources`（扫 tool_use 建 id→read_file 路径映射）；`_slim_old_tool_results`
  截短时若来自 read_file 则在标记里加"可用 read_file 重读 <路径>"。
- config.yaml system_prompt 加工作笔记指引（记事实/决定、整份替换、抗压缩、可重读说明）。

### 自检（Linux）
- `test_notes` 5/5（build_block 空判/存取与级联/工具整份替换与校验/注册/截短 read 来源标注且
  非 read 不带、原对象不改）；全回归 26 套绿 + node --check。
- **真模型端到端**：让模型把项目约定（Python 3.12 / DB 路径 / 缩进）用 update_notes 记下 →
  落库为结构化 Markdown → 关闭重建 Api、load_session 同会话 → `_effective_system()` 含
  「[工作笔记]」且含具体内容（3.12）。压缩与重启后结论不丢的闭环成立。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.4.0）
- [x] 模型用 update_notes 记决定；多轮后追问仍答准（注入生效）；重启同会话仍记得（跨重启）；
  删会话后笔记不残留；任务清单/记忆/git/联网/权限/评测套件回归正常。

---

## 2026-06-12 — P11 FR-11.4：细粒度权限 allowlist

**阶段**：P11（FR-11.4）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.3.0**。
Linux 全回归 25 套绿（新增 test_permissions 8/8）+ 真模型实测。

### 决策（对标 Claude Code permissions）
- 规则 `工具名` / `工具名(glob)`，glob 匹配工具「主体」（run_* 取 command、文件类 path、web url；
  fnmatch）。config `agent.permissions.allow/deny`；**deny 优先于 allow，也优先于 _allow_all**
  （硬拦截不被绕过）。确认条「总是允许这类」把推导规则加入本会话 allow（重启不留）。

### 做了什么
- 纯逻辑 `permissions.py`：tool_subject / parse_rule / rule_matches / evaluate（deny>allow>None）/
  suggest_rule（命令→首词通配、路径→父目录通配、url→同站通配、否则裸工具名）。
- `gate.py`：confirm 先 evaluate（deny 直接拒、allow/_allow_all 免弹），否则带 suggest emit；
  新决定 ALLOW_RULE 把 suggest 追加本会话 allow。`PermissionsConfig` + `AgentConfig.permissions`；
  Conversation 构造 gate 注入 config 规则。前端确认条「总是允许 <规则>」按钮 + perm-rule 样式；
  config.yaml permissions 段示例。

### 自检（Linux）
- `test_permissions` 8/8（解析/主体/匹配/deny 优先/推导/gate 集成四态）；全回归 25 套绿 + node --check。
- **真模型实测**：config `allow:["run_bash(git *)"]`、`deny:["run_bash(rm *)"]` 下，模型连跑
  git init + git status —— 权限请求 0 次（规则放行生效）。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.3.0）
- [x] config allow 规则放行该类操作不弹；deny 规则直接拒；「总是允许这类」本会话同类免确认、异类仍弹；
  会话临时规则重启不残留；无 permissions 配置行为同 3.2.0。

---

## 2026-06-12 — P11 FR-11.2：验证闭环（写入后自动校验 + 收尾自动评审）

**阶段**：P11（FR-11.2）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v3.2.0**。
Linux 全回归 24 套绿（新增 test_verify 6/6、test_conversation +1=30/30）+ 真实模型端到端。纯后端，前端不改。

### 决策
- 11.2a 写入后零成本校验**默认开**：.py/.json 标准库校验（必可用）、.js/.ts node --check（尽力）；
  失败信息并入工具返回回灌模型（改坏当步暴露）。11.2b 收尾评审**默认关**：每次多一次模型调用，
  按需开；本轮改过文件才触发，纯对话/只读/取消零开销。

### 做了什么
- 新 `verify.py`：`detect_kind`（py/json/node/空）、`verify_text`（ast.parse / json.loads 纯函数，
  含行号）、`make_verifier(workspace)`（读盘校验 + node 子进程，自身异常不抛）。
- `tools/fs.py`：三个写工具加 `verifier` 注入 + `_with_verify`（落盘成功后附加校验结果）。
- `build_registry(verifier=...)`；`AgentConfig.auto_verify`(默认 true)/`auto_review`(默认 false)；
  `Conversation` 主/子注册表注入 verifier、`_changed_files_this_turn` + `_maybe_auto_review`
  （扫本轮写工具调用 → 拿 diff → 派 reviewer，失败不影响交付）；config.yaml 两开关。

### 自检（Linux）
- `test_verify` 6/6；`test_conversation` +1=30/30（auto_review 仅写轮触发/纯对话不触发/取消不触发/
  关闭不触发）；全回归 24 套绿。
- **真实模型端到端**：让模型写出缺冒号的 py → write_file 返回当场带「⚠ 语法错误（util.py 第 1 行）」
  → 模型立即 edit 修正 → 最终 py_compile OK。验证闭环在真实任务里生效（改坏在当步暴露）。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.2.0）
- [x] 改 .py/.json 改坏时工具结果当场报语法错、模型自我修正。
- [x] auto_review:true 时改文件轮收尾出现 reviewer 子任务块；纯对话轮不触发。
- [x] 正确代码无告警、非代码文件不校验；写文件/台账/git/联网/评测套件回归正常。

---

## 2026-06-12 — P11 FR-11.1：联网检索（web_search + web_fetch）

**阶段**：P11（FR-11.1）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12，含外链修复二轮），**定版 v3.1.0**。
Linux 全回归 23 套绿（新增 test_web 6/6、test_conversation +1=29/29）+ 真实模型端到端。

### 关键实测（先实测端点再定设计）
- Bing `www.bing.com/search` 200 可解析（`b_algo` 块，真链 `u=a1<base64>`）；DDG lite 200 可解析
  （真链 `uddg=`）；DDG html 版 202 反爬不可用。Bing 国内外均可达 → **auto = Bing 优先、DDG 兜底**。

### 决策
- 免 key、零新依赖（urllib + html.parser + 正则）；解析器纯函数可离线金标准单测；
  页面改版解析失败自动换源、全挂聚合可读错误。
- 两工具只读、不过 gate、进只读角色白名单；`web.enabled:false` 不注册（回退 3.0.0 行为）。
- 允许抓 localhost（配合 FR-10.3 自测 dev server 是特性）；下载 2MB / 输出 2 万字符截断带标记。

### 做了什么
- `tools/web.py`：`_http_get`（IO 集中、UA/超时/字符集处理）+ 纯函数 `parse_bing` /
  `parse_ddg_lite` / `bing_real_url` / `extract_text`（HTMLParser 去 script/style/noscript、
  块级换行、抓 title）+ `WebSearchTool`（auto 链路）/ `WebFetchTool`（HTML 转正文，JSON/纯文本直出）。
- `config.py` WebConfig + config.yaml `web` 段与 system_prompt 联网指引（先搜后答、附来源、
  时效事实别凭记忆）；`build_registry(web=...)`；Conversation 主/子注册表注入；
  delegate 只读白名单 + web_search/web_fetch。

### 自检（Linux）
- `test_web` 6/6：bing/ddg 金标准 HTML 解析、a1+base64 与 uddg 真链还原、坏 base64 兜底、
  extract_text 去脚本保标题、空 query 与 ftp/file URL 拒绝、auto 换源与聚合错误、
  disabled 不注册、角色白名单。
- 直连冒烟：bing 搜索出真实结果（真链已还原）、example.com 正文提取正确。
- **真实模型端到端**（无头 harness）：问"PEP 703 现状"——模型自发 4 次 search + 4 次 fetch
  （peps.python.org / docs.python.org 官方源）、交叉验证 PEP 779，结论准确（3.13 实验性、
  3.14 官方支持）且全部附来源 URL，77s。全回归 23 套绿。

### 首轮验证反馈与修复（2026-06-12）
- ✗ **对话里点击来源 URL 把整个应用窗口导航到外站、无返回**（WebView 默认行为）。**已修**：
  前端全局拦截 `<a>` 点击——http(s) 链接调新增 `Api.open_external`（webbrowser 开**系统默认
  浏览器**，应用窗口不动）、其余非锚点链接一律阻止导航（javascript:/file: 等也被挡）。
  `open_external` 仅放行 http(s)。test_conversation +1=29/29。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.1.0）
- [x] 来源 URL 点击 → 系统默认浏览器打开、应用窗口不动（外链修复生效）。
- [x] 文档类/时效性问题：自发 search→fetch、不弹确认、答案带来源。
- [x] researcher 子任务可用联网工具；web.enabled:false 两工具消失、回退正常；本地任务不乱联网。

---

## 2026-06-12 — P10 收官实测 + P11 立项 + FR-11.0 本地评测基准

**阶段**：P11（FR-11.0，首攻）
**状态**：✅ 已 Windows 验证通过（2026-06-12：test_eval 5/5、全量真跑 4/4、退出码语义、
不污染 data/），**定版 v3.0.0（P11 首个交付）**。Linux 全回归 22 套绿、真跑 4/4。

### P10 收官实测（P11 立项依据）
用无头 harness 驱动内核（真实模型 ark-kimi、权限预置 allow_all）跑 4 个真实工程任务，**4/4 全过**：
- bugfix（16s/8 工具）：读码→跑测→一次 multi_edit 原子修两处→复跑全绿；
- feature+git（48s/22 工具）：自发建任务清单、开分支、实现+补测、清 pycache、限定 paths 提交、复核树干净；
- 代码库理解（138s/22 工具）：103 文件语料，结论准确（含"模型输入压缩、历史存储不压缩"），引用到 文件:行号；
- 并行委派（178s/2 子任务）：两个 delegate 同轮发出、#2 先于 #1 完成（真并行）。
**差距重估**：骨架对齐度约 85~90%；剩余差距 = 模型 × 联网 × 权限粒度 × 生态；
重型任务失败大头是工程性"自伤"（丢上下文/不验证/跑偏不回头）→ 据此立项 P11（PRD「3.X 路线」，
攻坚顺序 11.0→11.1→11.2→11.4→11.3→11.5→11.6/7/8）。

### FR-11.0 做了什么
- `scripts/eval/harness.py`：无头驱动（Api._emit 换事件收集器、gate 预置 allow_all、
  shell 按平台自适应、临时库不碰 data/）；`EvalResult`（事件流/全文/耗时/计数）。
- `scripts/eval/tasks.py`：4 任务＝夹具常量 + prompt + **程序化判分**——bugfix（测试全绿且
  测试文件未被篡改）、feature_git（clear 实现/测试过/feature 分支有新提交/main 未动/树干净）、
  comprehend（关键标识符命中 ≥3/5，空话不得分）、parallel（事件序判真并行：第 2 个 start
  早于第 1 个 done，且全部 ok、有汇总输出）。
- `scripts/eval/run_eval.py`：一键跑分（--task/--model/--quiet，退出码可进 CI）。
- `tests/test_eval.py`：离线自检判分器与夹具（金标准修复/作弊检测/合成事件四态），进全回归。

### 真跑结果（Linux，ark-kimi）
bugfix PASS 26s ｜ feature_git PASS 38s ｜ comprehend PASS 102s（关键词 5/5）｜
parallel PASS 192s（真并行）→ **总分 4/4**。

### Windows 验证 ✅ 通过（2026-06-12，定版 v3.0.0）
- [x] 离线自检 test_eval 5/5；单任务/全量跑分 4/4；退出码 0/1 语义正确；--model 生效；
  评测不污染 data/（无垃圾会话）。

---

## 2026-06-12 — P10 FR-10.5：并行委派 + 自定义角色 + 任务联动（P10 收官段）

**阶段**：P10（FR-10.5，最后一段）
**状态**：✅ 已 Windows 真机验证通过（2026-06-12），**定版 v2.4.0，P10 工程闭环全部收官**。
Linux 全回归 21 套绿（test_delegate +3=15/15、test_tasks +1=10/10）。

### 决策（对标 Claude Code 并行 subagent / 自定义 agent 推定）
- **并行＝同一 assistant 回合内的多个 delegate 并发**：工具类标 `parallel_safe=True`（仅 delegate），
  loop `_exec_calls` 把同回合 ≥2 个 parallel_safe 调用丢进线程池（上限 4），其余工具照旧顺序执行
  （且与并行组并发）；tool_result 按原调用顺序组装回灌。前端无需改（子任务块按 sub_id 并存、
  权限条每请求一条、emit 已锁）；`_sub_seq` 加锁。停止级联沿用共享 `_cancel`。
- **自定义角色＝`agent.roles`**（dict，同名可覆盖内置）：label / directive / tools（白名单，
  **所列即所得**，省略=全工具）/ model（**按角色配模型**，优先级 role.model→subagent_model→
  当前对话模型）。`build_roles` 合并；DelegateTool 的 role enum 与描述动态生成；未知回退 general。
- **任务联动＝新增 delegated 状态**（🤖）：提示词引导"委派清单项标 delegated、收到摘要转
  completed"，不做易碎的自动挂钩（沿用 FR-9.1 工具驱动哲学）。

### 做了什么
- `agent/loop.py`：`_exec_calls`（并行组+串行组、按原序组装、`_emit_result` 抽出）；
- `tools/delegate.py`：Role 加 `tools/model` 字段与白名单 allows、`build_roles`、
  `resolve_role(name, roles)`、DelegateTool 动态 schema/描述、`parallel_safe=True`、
  DelegateBinding 带 roles；
- `config.py`：`RoleSpec` + `AgentConfig.roles`；`Conversation`：`_roles` 合并、`_sub_lock`、
  run_subagent 按角色选模型；`tasks.py`+前端 `TASK_MARK`：delegated 🤖、回执单列"已委派"、
  注入块指引；config.yaml：system_prompt 并行/自定义角色/delegated 指引 + agent.roles 注释示例。

### 自检（Linux）
- `test_delegate` +3=15/15：build_roles（白名单所列即所得/同名覆盖内置/空名跳过/label 缺省/
  按角色模型/resolve 回退）；DelegateTool 动态 enum 含自定义、无自定义时仍为内置四个；
  **并行**：假 provider 一轮发 3 个 0.3s 的 delegate，总耗时 <0.7s（串行需 0.9s+）、
  tool_result 按 c1/c2/c3 原序；单 delegate/普通工具顺序语义不变。
- `test_tasks` +1=10/10（delegated 归一/未知状态回退 pending/🤖 入块/回执"已委派"单列）。
- 全回归 21 套绿 + node --check。

### Windows 验证 ✅ 通过（2026-06-12，定版 v2.4.0，P10 收官）
- [x] 并行：两个 researcher 子任务块同时出现、同时滚动，摘要正确回灌、主 Agent 汇总。
- [x] 自定义角色 docwriter：子任务块显示「文档」、能读写；白名单外的 run_powershell 调不到（不弹确认条）。
- [x] 任务联动：清单项委派时显示 🤖、摘要回来后转 ✅。
- [x] 停止级联：并行子任务全部停止、无残留运行态。
- [x] 回归：单委派/写文件+台账/git 只读/后台进程，行为与 2.3.0 一致。

**阶段**：P10（FR-10.4）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v2.3.0**。
Linux 全回归 21 套绿（test_p6_context +3=10/10、新 test_cache 6/6）+ 方舟直连实测。

### 关键实测（先实测、再定设计）
- 方舟 coding 端点（kimi-k2.6）**支持 cache_control 且真实命中**：相同请求第二次
  `cache_read_input_tokens=3712~3840`、`input_tokens` 5286→1446；system 块/消息块/tools 三类
  断点均接受；请求小于缓存门槛（约 2000 tokens 内）时不命中但**不报错**。
- 据此决策：anthropic 协议**默认开**缓存断点；对不支持端点做"开局失败降级重试一次 + cache 字样
  错误记入 `_CACHE_UNSUPPORTED` 名单"的优雅降级（流中途失败不重试防重复输出）。

### 做了什么
- **10.4a**：`context.py`——`compress(summarize=...)` 注入点（None/异常回退启发式）、
  `build_transcript`/`build_summary_request` 纯函数（每条 800 字/总 2.4 万上限，支持增量合并）。
  `Conversation._compact_summarize`：摘要缓存 `(覆盖条数, 文本)`——切点不动直接复用（零调用）、
  切点前移旧摘要+新增段一次合并调用、失败 120s 退避；`_budget` 接线、`context_compressed`
  事件加 `summary: model|heuristic`；前端 🗜 提示标注。配置 `context.model_summary/summary_model`。
- **10.4b**：`anthropic_p.py`——`apply_cache_breakpoints` 纯函数（system 末块/tools 末项/最后
  一条消息末块，全部打在拷贝上，不碰 history 原对象）；stream_chat 拆出 `_stream`（出错上抛），
  外层按"未吐过事件才降级重试"处理。`ModelConfig.prompt_cache`（默认 true）经 build_provider
  透传两 provider（openai 端点自动缓存，仅收参兼容）。

### 自检（Linux）
- `test_p6_context` +3=10/10（注入替代启发式且拿到完整被丢段/None+异常双回退/请求构造含增量形态）。
- 新 `test_cache` 6/6（断点三处+str/blocks/空形态边界+原对象不可变；假 client：cache 错降级重试
  成功且记账、后续不再试；瞬时错重试但不记账；prompt_cache=false 不加断点）。
- 方舟直连冒烟：provider 流式 + 断点两次调用均正常返回。全回归 21 套绿 + node --check。

### Windows 验证 ✅ 通过（2026-06-11，定版 v2.3.0）
- [x] 长会话触发压缩：🗜 提示"已压成模型生成的摘要"；压缩后追问早期细节（虚构人物）答得出。
- [x] 切点不动再发数轮：无额外摘要调用延迟。
- [x] ark-kimi 多轮：第二轮起首 token 明显变快（缓存命中），全程无报错。
- [x] minimax 档案：正常对话、降级无感。
- [x] 常规工具/git/后台进程回归正常。
- 验证期教训：config.yaml 改完**必须重启**才生效（用户首测没重启以为没触发；
  `scripts/check_compression.py` 可直接体检生效配置与各会话是否会触发）。

---

## 2026-06-11 — P10 FR-10.3：后台命令/长进程（background + 进程三件套）

**阶段**：P10（FR-10.3）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v2.2.0**。
Linux 全回归 20 套绿（新增 test_procs 8/8、test_conversation +1=28/28）。纯后端，前端不改。

### 决策（按 PRD 既定范围 + 对标 Claude Code run_in_background/BashOutput/KillShell 推定）
- `run_powershell` 加 `background:true` 后台启动返回进程编号（启动＝执行命令，仍过 gate）；
  `list_processes` / `read_process_output`（**增量**：每次只回上次读取后的新输出）/ `stop_process`
  三件不过 gate（list/read 只读、stop 只能停本对话后台启动的进程，对标 KillShell）。
- ProcessManager **每对话一个**、跨工作区切换保留、主/子 Agent 共用；读线程收输出进环形缓冲
  （20 万字符上限、溢出丢最旧标记 trimmed；单次 read 返回上限 5 万）；并发上限 8/对话。
- **清理**：关窗（shutdown）与删除会话运行时必杀全部子进程；「停止」按钮不杀（dev server 是交付物）。
- **杀进程树**：Windows `taskkill /PID x /T /F` + `CREATE_NO_WINDOW`；POSIX `start_new_session`+killpg。

### 做了什么
- 新 `tools/procs.py`：ProcessManager（start/list/read/stop/kill_all，线程安全）+ 三工具。
- `shell.py` RunShellTool 加 background 入参（manager 未注入时可读报错，行为同 2.1.0）；
  `build_registry(process_manager)`；`Conversation` 持 `self.procs` 并注入主/子注册表、
  shutdown 杀全部；`Api.delete_session` 对被移除的运行时（含删当前会话的旧 active）调 shutdown；
  delegate 只读白名单加 list/read；config.yaml system_prompt 加后台命令指引。

### 自检（Linux，bash 验）
- `test_procs` 8/8：启动→增量读→exited(0)、二次读无新输出；长进程 stop 杀树（连 sleep 子进程）、
  停止幂等；缓冲溢出 trimmed 标记 + 单次 5 万上限；上限 8；未知 id 报错；list 状态；
  注册与 dangerous 标记；无 manager 行为回退；角色白名单（只读见 list/read、不见 stop）。
- `test_conversation` +1=28/28（shutdown 杀后台进程不残留）。全回归 20 套绿。

### Windows 验证 ✅ 通过（2026-06-11，定版 v2.2.0）
- [x] dev server background 启动：一次确认、返回进程编号、浏览器可访问、无黑窗闪烁。
- [x] read_process_output 增量：有新 GET 记录；二次读只给新增。
- [x] stop_process 杀树：端口立即失效；list 显示 exited。
- [x] 起着 server 直接关窗：任务管理器无残留子进程。
- [x] 普通前台命令无回归。

---

## 2026-06-11 — P10 FR-10.2：读写精度（read 行号/局部读 + multi_edit + edit 诊断）

**阶段**：P10（FR-10.2）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v2.1.0**。
Linux 全回归 19 套绿（新增 test_fs_rw 12/12）。纯后端（工具层），前端不改。

### 决策（按对标基准 Claude Code 与现有惯例推定，未另开会）
- read_file **默认带行号**（`行号+制表符+内容`，cat -n 风格）+ `offset`/`limit`（默认/上限 2000 行）；
  按行流式读，输出字符上限 20 万防灌爆上下文；**没读完明确提示"继续读用 offset=N"**（旧实现
  200KB 一刀切且不告知）；超长单行（>2000 字符）截断加标记。不加 config 开关。
- edit_file 加 `replace_all`；失败信息可操作：未命中时依次诊断"行号前缀带入 / 空白缩进不一致 /
  确实不存在"，多处匹配报次数并提示补上下文或 replace_all。
- multi_edit：edits 按序在**内存**应用（后面的编辑作用在前面之后的内容上）、**原子落盘**，
  任意一处失败报"第 i/n 处 + 原因 +（整个文件未改动）"。危险、过 gate、挂改动台账。

### 做了什么
- `tools/fs.py`：重写 ReadFileTool；EditFileTool 加 replace_all 与诊断；新增 MultiEditTool。
  匹配诊断 `diagnose_not_found`、多处应用 `apply_edits` 为纯函数（与工具层分离可单测）。
- `build_registry` 注册 multi_edit（与 write/edit 同挂 change_tracker）；config.yaml
  system_prompt 更新读写指引（多处改用 multi_edit、分段读、old_string 别带行号前缀）。

### 自检（Linux）
- `test_fs_rw` 12/12：行号格式/offset+limit+续读提示/越界 offset 报总行数/超长行截断/
  20 万字符上限中途停+续读提示/空文件；edit 三类未命中诊断/重复计数提示/replace_all；
  multi_edit 按序依赖/原子性（第 2 处失败第 1 处也不落盘）/校验/replace_all 计数/
  dangerous+台账挂钩；纯函数细节。
- 全回归 19 套绿（含 test_p3 旧用例不变：行号输出与包含断言兼容）。

### Windows 验证 ✅ 通过（2026-06-11，定版 v2.1.0）
- [x] 大文件分段读、"继续读用 offset=N"提示、读到文件末尾不吞尾。
- [x] multi_edit 一次确认完成多处修改；含失配编辑时整个文件不变、报第几处失败（原子性）。
- [x] edit 失配时按中文诊断自我纠正。
- [x] 常规读写/改动台账/git 面板/会话切换无回归。

---

## 2026-06-11 — P10 FR-10.1：Git 集成（2.X 首攻）

**阶段**：P10（FR-10.1，2.X 首段）
**状态**：✅ 已 Windows 真机验证通过（两轮，2026-06-11），**定版 v2.0.0（2.X 首个交付）**。
Linux 全回归 18 套绿（test_git 13/13、test_conversation 27/27）、`node --check` 过。

### 决策（已与用户确认，2026-06-11）
- **工具形态＝拆分**：git_status / git_diff / git_log 只读（非危险、不过 gate）；
  git_commit / git_branch（create/switch）危险（过 gate 逐次确认）。
- **仓库礼仪＝引导不硬拦**：system_prompt 写入（只在用户要求时提交；未明说就先开分支）；
  commit 结果显示分支名，默认分支（main/master）直接提交附 ⚠ 提醒但不拒绝。
- 走 **git CLI**（subprocess、cwd=工作区），不引入 GitPython；未装 git/非仓库给可读错误。
- 工具**常注册**（非 git 仓库时报错即可），避免"会话中途 git init 后工具不出现"。
- 面板「改动」区按工作区**动态判定**（根有 `.git` → git 模式，每次调用判定）：列**全部未提交改动**
  （暂存/未暂存/未跟踪、跨重启、含用户手改），diff 对 HEAD，回退=丢弃未提交改动；
  非 git 工作区沿用 FR-9.4a 内存台账兜底（行为与 1.6.0 一致）。

### 做了什么
- 新模块 `gitsupport.py`：`run_git`（超时 30s/未装 git/失败 → GitError 可读信息）+ 纯解析
  `parse_porcelain`（-uall；归并 added/modified/deleted，重命名取新路径）+ `is_git_workspace` /
  `has_head` / `current_branch` / `changes` / `status_summary`（分支+改动+本地分支列表）/
  `file_diff`（对 HEAD；未跟踪/无 HEAD 合成新增 diff，2000 行截断）/ `diff_text`（工具版：
  无 path 时未跟踪只列名防爆量）/ `log_text` / `revert_file`（tracked→checkout HEAD，
  新增→取消暂存+删除）/ `revert_all` / `commit`（add 全部或指定 paths、空提交可读报错、
  默认分支 ⚠）/ `branch`（switch -c / switch）。
- 新工具 `tools/git.py` 五件套（GitError→ToolError）；`build_registry` 常注册；
  只读三件加进子 Agent 只读角色白名单（`_READ_ONLY_TOOLS`）。
- `Conversation.changes_mode()` + `get_changes/get_file_diff/revert_file/revert_all` 按模式
  动态路由 git/台账（git 异常时返回空/False，不崩面板）；`Api.get_changes` 响应带 `mode`。
- 前端改动区按 `mode` 区分：标题「未提交改动·git (N)」/「改动 (N)」；回退确认文案 git 模式
  写明"恢复到最近一次提交、含非本对话改动、新增文件会被删除"。
- `config.yaml` system_prompt 加 git 工具指引 + 仓库礼仪段。

### 自检（Linux）
- `test_git` 10/10（porcelain 解析含重命名与中文/增改删状态/file_diff 与未跟踪合成 diff/
  diff_text 列名与带 path 展开/回退三态+revert_all/默认分支 ⚠ 与分支提交无 ⚠/paths 限定提交/
  空仓库（HEAD 未出生）/非仓库报错/工具注册与 dangerous 标记）。
- `test_conversation` +1=27/27（ledger→git 动态切换、git 模式 Api diff/回退、mode 字段）。
- 全回归 18 套绿；`node --check web/app.js` 过。

### 第一轮 Windows 验证（2026-06-11）：大部分通过，发现 1 个性能 bug + 1 个环境阻塞
- [x] 只读工具免确认 / git init 中途切 git 模式 / 面板列未提交改动（含手改）/ diff 着色 /
  单文件回退 / 跨重启 / 非 git 回归——**均通过**。
- ✗ **「全部回退」改动上百时卡 UI**：根因 `revert_all` 逐文件回退，每文件 2 次 git 子进程
  （status+checkout），100+ 改动＝200+ 次进程创建，Windows spawn 贵。**已修**：改批量——
  added 批量 reset（无 HEAD 退化 `rm --cached --ignore-unmatch`）+ Python 内删文件、
  tracked 分批（100/批防命令行超长）`checkout HEAD --`、个别失败退回逐个；返回值改为
  前后改动数之差。`test_git` 加 115 文件跨分批边界用例 + 空仓库混合用例。
- ✗ **commit 未测**：机器没配 git 身份。顺手把该报错转成中文可操作提示（user.name/email
  两条命令 + 可让 Agent 代配）；检测覆盖 "tell me who you are"/auto-detect/empty ident/
  no email(name) was given。`test_git` 用 `user.useConfigOnly=true` 复现（开发机会自动推断身份）。
- 自检：`test_git` +3=13/13，全回归 18 套绿。

### 第二轮 Windows 验证 ✅ 通过（2026-06-11，定版 v2.0.0）
- [x] 配置身份后：git_commit 弹确认、结果显示分支名、main 直接提交带 ⚠ 提醒。
- [x] 开分支再提交：git_branch 过确认、切到新分支、提交无 ⚠；git_log 可见记录。
- [x] 改动上百时「全部回退」不再卡 UI、列表清空（批量修复生效）。

---

## 2026-06-11 — P9 FR-9.4：改动评审/回退（9.4a）+ 上下文瘦身（9.4b）

**阶段**：P9（FR-9.4，P9 收官段）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.6.0，P9 全部收官**。
前后端实现完成、Linux 全回归 17 套绿（新增 test_changes 8/8、test_p6_context +2、
test_conversation +2=26/26）、`node --check` 过。

### 决策（已与用户确认）
- 范围＝9.4a 改动评审/回退 + 9.4b 上下文瘦身都做（a 先）；「协调式多文件编辑」并入 diff/回退安全网。
- 台账**内存级**（随对话运行时，重启即清；文件本身不受影响）——回退本来就是"刚改完反悔"的场景。
- 只追踪 write_file/edit_file（含子 Agent，共用台账）；run_powershell 改的不追踪（已知限制）。

### 做了什么
- **9.4a 后端**：`changes.py`——`ChangeLedger`：`snapshot(rel)` 改前记基线（同文件只记第一次、>2MB 不追踪）、
  `changes()` 算 added/modified/deleted（与基线相同=改回原样→不算）、`diff()` difflib 统一格式（2000 行截断）、
  `revert()/revert_all()`（新增文件回退=删除，成功后出账）。write/edit 工具加可选 `tracker` 回调；
  `build_registry(change_tracker=...)`；`Conversation._build_registry` 建台账并注入（**换工作区即重置**、
  子 Agent 注册表共用）；`Api.get_changes/get_file_diff/revert_file/revert_all_changes`。
- **9.4a 前端**：面板 ws-path 与文件树之间加 `#ws-changes`「改动」区：行=状态标记+路径+「回退」（悬停显），
  头部=计数+「全部回退」；点路径在预览区渲染**着色 diff**（+绿/-红/@@蓝）；随 refreshWorkspace 自动刷新；
  无改动隐藏。回退均 confirm；回退后清预览防看到旧内容。
- **9.4b**：`context.py` 加 `_slim_old_tool_results`（只动最近 keep_recent_turns 之前回合里 >600 字的
  tool_result，截短保留头部+「已截短(原N字符)」标记；复制受影响消息不改原对象、保持 tool 配对）；
  `compress` 超预算先瘦身、够了即返回（dropped=0），仍超才整回合丢弃。`CompressResult.slimmed`。

### 自检（Linux）
- `test_changes` 8/8（基线只记第一次/改回原样不算/新增回退=删除/全部回退/deleted 态/工具挂钩/无 tracker 兼容）。
- `test_p6_context` +2=7/7（瘦身优先且不丢回合不动最近回合不改原件；瘦身不够回落丢回合+摘要）。
- `test_conversation` +2=26/26（写入自动入账+Api diff/回退；换工作区台账重置+子 Agent 共用）。
- 全回归 17 套绿；`node --check web/app.js` 过。前端「改动」区交互只能 Windows 验。

### Windows 验证 ✅ 通过（2026-06-11，定版 v1.6.0）
- [x] Agent 改多个文件后「改动」区列出（＋/✎ 正确）；点文件 diff 着色准确；新增文件 diff 全 +。
- [x] 单文件「回退」恢复原内容、新增文件回退后消失；「全部回退」一键全恢复；改回原样自动出账。
- [x] 子 Agent（general）写的文件也入账可回退；切会话台账隔离。
- [x] 长会话读几个大文件后触发压缩：🗜 提示照常、回答仍连贯（瘦身优先，不再大段丢历史）。

---

## 2026-06-11 — P9 FR-9.2：代码库检索/索引（code_outline + find_symbol）

**阶段**：P9（FR-9.2）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.5.0**。实现为纯后端工具（前端不改）、Linux 全回归 16 套绿（新增 test_codeindex 8/8）。

### 决策（已与用户确认）
- 按需扫描、**不持久化**（无缓存/失效复杂度，中小项目够快）。
- 仅加只读工具，结果走工具块，**前端不动**（全部 Linux 可单测、交付快）。

### 做了什么
- 纯逻辑 `codeindex.py`：Python 用 `ast` 精确抽顶层函数/类/类内方法（签名用 `ast.unparse(args)` + 行号）；
  其它语言（JS/TS/Go/Rust/Java/C/Ruby…）用逐行正则兜底（class/function/const 箭头/func/fn/def 等）。
  `walk_outline`/`walk_find`（复用噪音目录跳过 + 文件 600/符号 1200/单文件 1MB 上限）；`format_*` 出文本。
- 工具 `tools/codesearch.py`：`code_outline(path=".")`（目录或单文件大纲）、`find_symbol(name,path?)`
  （先精确匹配、全无回退子串）。只读、限工作区内、非危险。`build_registry` 默认注册。
- 加进只读角色白名单 `_READ_ONLY_TOOLS`（researcher/reviewer/tester 也能用）；system_prompt 加指引。

### 自检（Linux）
- `test_codeindex` 8/8（ast 抽取含方法/签名/行号、语法错误安全、JS 正则、遍历跳 __pycache__/非源码、
  精确+子串查找、两工具、registry 注册）。全回归 16 套绿。
- 顺带修 `test_delegate` 的 researcher 注册表断言（只读角色现在也含 code_outline/find_symbol）。

### Windows 验证 ✅ 通过（2026-06-11，定版 v1.5.0）
- [x] `code_outline` 对大目录出结构大纲（含 web/app.js 等非 Python 文件）；`find_symbol` 定位定义比 grep 准
  （精确一击 + 子串回退均验）。
- [x] 模型摸项目时会先用它们；只读子 Agent（researcher）也能调用这两个检索工具。
- [x] 大项目不超时/不撑爆：40 文件×40 函数（1600 符号）压测，几秒返回、按上限优雅截断并提示细看子目录；
  单文件再 outline 完整；find_symbol 命中过多也正常截断。

---

## 2026-06-11 — P9 FR-9.5：子 Agent 角色与工具限权（delegate role）

**阶段**：P9（FR-9.5，FR-9.3 增强；对标 Claude Code 自定义 agent）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.4.0**。前后端实现完成、Linux 全回归 15 套绿（test_delegate 11/11）、`node --check` 过。

### 背景（与用户讨论后立项）
用户提出"主 Agent 当 PM/架构师调度、子 Agent 专业分工"的设想。讨论结论：方向对，但**价值主要在
上下文隔离 + 并行只读 + 角色专精**，而非把耦合的写活并行成虚拟开发团队。对标 Claude Code：TodoWrite（≈FR-9.1）
是主 Agent 自己的计划、逐条自做；Task/子 Agent（≈FR-9.3）选择性卸载，且有"角色/类型"（自定义 agent =
定制提示 + 工具白名单 + 模型）。据此把"**角色 + 工具限权**"作为 FR-9.3 的增强先做。

### 决策
- 内置角色：general(全工具,默认) / researcher(只读) / reviewer(只读) / tester(只读+可跑命令)。
- 限权按"能力判定"而非硬编码工具名——只读名单 {read_file,list_dir,grep_search,glob_search,recall} +
  shell 按 `run_` 前缀判定（兼容动态 shell 名 run_powershell/run_bash…）。未知/缺省角色回退 general（向后兼容）。
- 模型：暂沿用 `agent.subagent_model` 或主模型（按角色配模型留待后续）。

### 做了什么
- `tools/delegate.py`：`Role`（name/label/directive/allow_all/allow_shell + `allows(tool_name)`）+ `ROLES` +
  `resolve_role`；`delegate` 加 `role` 入参（enum）；`DelegateBinding.runner` 签名加 role。
- `ToolRegistry.filtered(keep)` 按名过滤；`Conversation._subagent_registry(role)` 在排除 delegate/update_tasks
  之上再 `filtered(role.allows)`；`_subagent_system(role)` 追加角色职责；`run_subagent(task,context,role)`；
  `subagent_start` 带 role/role_label。config system_prompt 加角色选择指引。
- 前端：子任务块头显示角色（「🤖 子任务 · 调研」）；历史按 input.role 还原标签（ROLE_LABELS）。

### 自检（Linux）
- `test_delegate` 11/11（+角色回退/各角色工具权限/注册表按角色过滤）；全回归 15 套绿；`node --check` 过。
- 真实"只读角色绕不过写"只能 Windows 端到端验。

### Windows 验证 ✅ 通过（2026-06-11，定版 v1.4.0）
- [x] 模型按子任务性质选角色（调研→researcher、评审→reviewer、测试→tester、改代码→general）。
- [x] 只读角色**确实拿不到写/命令工具**：让 researcher"改个文件"做不到（工具不存在）、不弹写权限。
- [x] 子任务块头显示角色名；general（不传 role）行为与 v1.3.0 一致。

---

## 2026-06-11 — P9 FR-9.3：子 Agent / 委派（delegate 工具 + 可折叠子任务块）

**阶段**：P9（FR-9.3；复用 P8 运行时）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.3.0**（委派是新能力，按 SemVer minor 升）。前后端实现完成、Linux 全回归 15 套绿（新增 test_delegate 8/8 + run_subagent 假 provider 集成）、`node --check` 过。

### 决策（已与用户确认）
- 子 Agent 模型：默认＝当前主模型，可配 `agent.subagent_model`（想用更便宜/快的模型跑子任务就填档名）。
- 前端：**可折叠实时子任务块**（显示子 Agent 过程，完成收起留摘要）。
- 执行/隔离：子注册表**排除 delegate（防无限嵌套，深度=1）与 update_tasks（不碰主清单）**；
  共用本对话 gate（危险操作照常确认）与 `_cancel`（主对话「停止」连子 Agent 一起停，回合间生效）。

### 做了什么
- **后端（9.3a）**：`tools/delegate.py`——纯函数 `compose_task`（首条 user 消息）/`extract_summary`（取最后
  assistant 文本作摘要）/`SUBAGENT_DIRECTIVE`（子 Agent 角色指令）；`DelegateTool`（非危险，经 `DelegateBinding`
  转发到 runner）。`build_registry` 加 `delegate_binding`。`Conversation.run_subagent(task,context)`：起独立
  历史的 `AgentLoop`（`_subagent_registry()` 排除 delegate/update_tasks）、用 `subagent_model` 或主模型、
  共用 gate 与 `_cancel`，跑完取摘要回灌；子事件经 `subagent_start`/`subagent_event`/`subagent_done`（带 sub_id）
  路由。`_subagent_system()`＝基础提示+项目规范+角色指令（不含主任务清单/记忆）。config 加 `subagent_model`/
  `subagent_max_steps`、system_prompt 加委派指引。
- **前端（9.3b）**：可折叠「🤖 子任务」块（summary 头含任务+状态；body 含工具活动行 + 流式文本；done 后收起
  留摘要），按 sub_id 存 `view.subBlocks`；`subagent_*` 事件路由；**抑制 delegate 的通用工具块**（避免与子任务块
  重复）；`renderHistory` 用同套 ensure/finishSubBlock 回填历史委派摘要（重载可见）。CSS 一套子任务块样式。

### 自检（Linux）
- `test_delegate` 8/8（compose/extract 纯函数 + 工具转发/拒空 + 主注册表含 delegate、子注册表排除 delegate/update_tasks）；
  `test_conversation` +2＝24/24（假 provider 跑 run_subagent：发 start/event(chunk)/done、返回摘要；子注册表隔离）。
- 全回归 15 套全绿；`node --check web/app.js` 过。真实子 Agent 端到端只能 Windows 验。

### Windows 验证 ✅ 通过（2026-06-11，定版 v1.3.0）
- [x] 复杂任务模型会用 delegate 拆活给子 Agent；子任务块实时显示工具调用/输出，完成后收起、留摘要。
- [x] 子任务里的危险操作有权限确认；主对话点「停止」能连子 Agent 一起停。
- [x] 主上下文只进摘要（不被子任务中间步骤撑大）；重载历史能看到委派摘要。
- [x] 简单任务不乱委派；子 Agent 不会再委派（不嵌套）。

---

## 2026-06-11 — P9 FR-9.1：任务规划与拆解（update_tasks 工具 + 顶部任务面板）

**阶段**：P9（FR-9.1，v1.2 首攻；建在 P8 运行时之上）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.2.0**。前后端实现完成、Linux 全回归 14 套绿（新增 test_tasks 9/9）、`node --check web/app.js` 过。

### 决策（已与用户确认）
- 机制＝**工具驱动**（对标 Claude Code TodoWrite）：模型自行判断何时拆解、边做边更新状态，不做额外"自动规划一趟"模型调用。
- 执行＝**追踪式**：模型在正常 agent 循环里推进、勾状态；后端自动驱动子任务/子 Agent 留到 FR-9.3。
- 面板＝**对话区顶部可折叠条**（每对话一份，随会话切换）。

### 做了什么
- **后端（9.1a）**：`store/db.py` 加 `session_tasks` 表（session_id 主键、整份替换式存 JSON、删会话级联）+
  `set_tasks/get_tasks`。新工具 `tools/tasks.py`：纯函数 `normalize_tasks`（校验/归一，非法 status→pending、
  上限 50、空 content 拒绝）/`summarize_tasks`（回模型的进度回执）/`build_task_block`（注入 system 的清单块）；
  `UpdateTasksTool`（非危险、不过 gate）持 `TaskBinding`（store + `session_getter` + `emit`），run 落库 + 发
  `tasks_updated` 事件 + 回摘要。`build_registry` 加 `task_binding` 注册；`Conversation._build_registry` 注入
  `TaskBinding(res.store, lambda:self.session_id, self.emit)`；`_effective_system` 追加「[当前任务清单]」块
  （**抗上下文压缩**）；`Api.get_tasks()`/`Conversation.get_tasks()` 供前端取活动对话清单；config system_prompt 加指引。
- **前端（9.1b）**：`index.html` 加 `#task-bar`（topbar 与 chat-area 之间）。`view.tasks` 按 cid 存；
  `renderTaskBar()` 渲染进度 + 列表（✅/🔄/⬜，completed 删除线、in_progress 强调色），折叠态存 localStorage；
  `refreshTasks()` 从 `get_tasks` 拉权威清单；`tasks_updated` 事件路由（活动→刷新、后台→标未读）；
  `mountView` 挂载会话时刷新。CSS 一套任务条样式。

### 自检（Linux）
- `test_tasks` 9/9（normalize 正反例 / summarize / build_block / store 往返 + 删会话级联 / 工具落库+发事件 /
  无会话报错 / registry 按 binding 注册且非危险）。全回归 14 套全绿；`node --check web/app.js` 过。
- GUI/模型真实建清单只能 Windows 验。

### Windows 验证 ✅ 通过（2026-06-11，定版 v1.2.0）
- [x] 给一个较复杂任务，模型会用 update_tasks 建清单；顶部「任务清单」条出现、随进展更新勾选与进度。
- [x] 切到别的会话看各自的清单；后台对话更新清单时该会话行标未读。
- [x] 重启后或对话很长（触发压缩）时，模型仍按既有计划推进（清单注入 system 生效）。
- [x] 简单一两步任务不乱建清单；折叠/展开正常、空清单不占位。

---

## 2026-06-11 — P8 FR-8.3：后台权限路由 + 停止运行中任务 + 优雅收尾

**阶段**：P8（FR-8.3，收尾 P8）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.1.1**（用户决定按补丁定版，沿用 1.1.x；P9 仍规划 1.2）。
前后端实现完成、Linux 全回归 13 套绿（含 +4 新单测 22/22）、`node --check web/app.js` 过。
**范围**：必做（权限按 cid 路由 + awaiting 态 + close 优雅收尾）+ 停止/取消运行中任务；**不做并发上限**（用户定）。

### 做了什么
- **后台对话权限按 cid 路由（修 bug）**：`permission_request` 早已带 `cid`，但 `Api.resolve_permission`
  原固定调 `self.active.gate.resolve(...)`——各对话 gate 的 `req_id` 都从 1 起会**跨对话撞号**，后台对话的
  确认会被错解到当前对话。现 `resolve_permission(req_id, decision, cid)` 按 cid 路由到对应 `Conversation.gate`；
  gate 触发确认时进入 `awaiting` 态、发 `state` 事件。前端：该会话行橙色脉冲点、非活动时全局 toast，切过去看确认条。
- **停止/取消运行中任务**：`AgentLoop.run` 加 `cancel: threading.Event`，**每回合开始前检查**置位即停
  （不打断当前回合内已在进行的模型流，回合间生效）。`Conversation.stop()`：置 `_cancel` + 清空尚未开始的
  排队任务 + `gate.reset()` 解除可能的权限等待；被停时 `send_message` 发 `stopped`（已生成部分照常落库、不再生成规范）。
  `Api.stop_conversation(cid)`。前端：运行中输入区以「停止」按钮替代「发送」，点击调用。
- **优雅收尾**：`Conversation.shutdown(timeout)` 置 `_stop`/`_cancel`、`gate.reset`、`join` 带超时；worker 循环在
  取任务前后两处检查 `_stop` 立即收尾。`Api.close()` 先对所有对话 `shutdown(2s)` 再关 mcp/store——带运行中/
  等权限的任务关窗也不卡死。

### 关键决策
- 取消只在**回合边界**生效（PRD 既定）：不强行打断 provider 流，避免半截网络/解析状态；长单轮需等该轮结束。
- 取消标志清除放在 `enqueue`（用户新发送）而非 `send_message` 开头：避免"stop 后 worker 恰好取走排队任务又被
  清标志"的竞态——新一轮发送才解除取消，被停的任务即便被取走也立即停。
- `stopped` 与 `done` 区分事件：前端给"⏹ 已停止"轻提示，且停止不触发自动生成规范。

### 自检（Linux）
- `test_conversation` +4 = 22/22：cid 路由不撞号（两对话各 req_id=1 分别解）/ stop 清队列+置 cancel /
  loop 取消即停（一回合不跑）/ 带运行中 worker 的 `close()` 在超时内返回不卡。
- 全回归 13 套全绿；`node --check web/app.js` 过。
- GUI/真实并发与关窗只能 Windows 验。

### Windows 验中修复
- **后台等权限时会话行显示紫点而非橙点**（2026-06-11，纯 CSS）：触发权限会同时给该行加 `awaiting`（橙）
  与 `unread`（紫），而 `.unread:not(.running)` 选择器优先级 (0,4,1) 高于 `.awaiting` 的 (0,3,1)，紫色覆盖橙色；
  切过去 `unread` 清掉才显橙。修法：未读规则加 `:not(.awaiting)`，让等待权限的橙点优先于未读紫点。

### 待 Windows 验证
- [x] 后台对话触发危险操作：该会话行出现橙色脉冲点 + 全局提示；切过去看到确认条，允许/拒绝/全部允许都正常。
- [x] 运行中点「停止」：当前回合结束后停下、出"⏹ 已停止"，可继续发新消息；卡在权限确认时点停止也能解开。
- [x] 关窗：有对话正在跑 / 正等权限时关闭应用，能干净退出、不卡死不崩。
- [x] 单/多对话其它行为无回归（发送↔停止按钮切换正确）。✅ 全部通过（2026-06-11，定版 v1.1.1）

---

## 2026-06-11 — 启动加速：修 pywebview 序列化 Api._window 导致的递归 + 提速（定版 v1.1.2）

**阶段**：1.1.x 维护 / 启动性能
**状态**：✅ 已 Windows 真机验证通过、**定版 v1.1.2**——`导航开始→pywebviewready` 从 ~930ms 降到 ~330ms、日志无报错。

### 背景
用户反馈：启动时 UI 块已出，但"等模型加载"时快时慢；该期间点击会卡。从代码确认启动链路**不碰网络**
（`get_models` 只读 config 模型名；provider 客户端发消息时才建；MCP 默认关、`start()` 直接返回），
`Api.__init__`（开 SQLite、读配置）在建窗口**之前**跑完。初判耗时主要在 WebView2 建桥（`pywebviewready`）
冷启动，时快时慢是其典型特征，`debug=True` 再加一点开销。

### 做了什么
- **启动计时探针**：`app.py` 打印 `load_config` / `Api.__init__` 耗时 + "交给 WebView2"标记；
  新增桥方法 `Api.client_log(msg)` 让前端把 `导航开始→pywebviewready` / `get_models` / `refreshSessions`
  / 初始化总计 等耗时上报到**同一终端 stderr**（便于一处看全）。前端用 `performance.now()`（自导航起）计时。
- **桥就绪前禁用输入**：`input.disabled=true`/`sendBtn.disabled=true` 直到 `pywebviewready` 完成再开放，
  避免桥未就绪时点击发送/下拉卡住。

### 定位结论（2026-06-11，用户贴回数字）
- `load_config=16ms`、`Api.__init__=30ms`（我们 Python 侧共 **46ms，非瓶颈**）；
  `get_models=20ms`、`refreshSessions=8ms`（桥调用很快）；
  **`导航开始→pywebviewready=933ms`（占全程 967ms 的 96%）= 瓶颈**：WebView2 加载页面 + 建桥。
- 期间 pywebview 报 `maximum recursion depth exceeded`（栈 `window.native.AccessibilityObject.Bounds.Empty.Empty…`）
  + 一串 `CoreWebView2 can only be accessed from the UI thread` COM 错误。
- **先误判为 `debug=True` 触发**（关 debug 后递归依旧，证伪）。**真因**：pywebview 序列化 js_api 对象时会
  遍历其**公有属性**，而我们在 `Api` 上存了 `self.window`（pywebview Window）→ pywebview 一路走进
  `window.native.browser.webview` / `AccessibilityObject` 的**原生 .NET/WebView2 COM 对象图**，
  `Rectangle.Empty` 自指→无限递归，跨线程访问 WebView2 → COM 报错。这些被 pywebview 捕获不致崩，
  但白白消耗时间、是"时快时慢"的来源之一。

### 修复
- **`Api.window` → `Api._window`（下划线私有）**：pywebview 跳过 `_` 开头属性，不再序列化它 → 从源头消除
  那串递归 + COM 错误。`app.py` 注入处与 `_emit`/`open_project`/测试同步改名。
- 附带：`debug` 默认关（`HERMES_DEBUG=1` 才开 devtools）——省 devtools 开销、生产更干净（与递归无关）；
  计时探针也收进 `HERMES_DEBUG`（普通启动安静、保留以便日后排查）；桥就绪前禁用输入防点击卡。

### 验证（用户 Windows，2026-06-11）
- `_window` 改名后：那串 `RecursionError` + `CoreWebView2 ... UI thread` COM 错误**全部消失**；
  `导航开始→pywebviewready` **933ms（debug开）/731ms（debug关）→ 326ms**，总计 342ms，启动快近 3 倍。
- 实锤：pywebview 之前在反复爬 `Api.window` 的原生 COM 对象图（递归 1000 层 + 多次 COM 异常栈）吃掉数百毫秒。
- 定版 v1.1.2，同步改 pyproject / CHANGELOG / DEVLOG。剩余 ~330ms 属 WebView2 冷启动本身，可接受、不再追。

---

## 2026-06-10 — P8 FR-8.2b：多对话并发 UI（前端按 cid 分视图 + 后端运行时注册表）

**阶段**：P8（FR-8.2b，v1.1）
**状态**：✅ 已 Windows 真机验证通过（2026-06-11），**定版 v1.1.0**。前后端实现完成、Linux 全回归 13 套绿（含并发/注册表单测）、app.js node --check 过。

### 做了什么
- **后端运行时注册表**（实现中发现的必需项——光改前端无法"切回后台跑着的对话"）：
  `Api.conversations: dict[cid, Conversation]` 保活所有运行时（含后台运行中的）。新增
  `switch_conversation(cid)` 直接切回已存在运行时（不重载、不丢状态）；`load_session` 优先复用仍活着的
  同会话运行时（返回 `live:true`），否则才冷加载建新运行时；`new_session/open_project/load_session/
  delete_session` 统一返回 `cid`/`active_cid` 供前端建视图；离开"空闲空草稿"时回收防堆积。
- **前端按 cid 分视图**：模块级全局（currentBubble/streaming/toolBlocks/thinking/working…）改为
  每对话一个 `View`（含独立的、可离屏渲染的 `.chat-view` 容器）。`__onAgentEvent` 按 `msg.cid` 路由到
  对应 View——活动视图实时渲染，后台视图照常渲染进离屏 DOM 并标未读；切会话 = 挂载该 View
  （后台跑着的对话直接"续看"流式）。session_id↔cid 用 `session_created` 建映射。
- **放开 streaming 全局封锁**：原来 `streaming` 是全局、跑着时不能切会话/发送；现按活动 View 判断，
  可随时开新对话/切会话，发送按钮按当前 View 是否在跑来启用。会话栏每行加运行中（脉冲点）/未读点标记（CSS）。

### 关键决策
- 离屏 DOM 而非事件缓冲回放：每对话保留自己的 `.chat-view`，后台事件直接渲染进去、切回挂载即续看，
  天然保住流式/工具块/思考块/滚动状态，省去重放的复杂度。
- 必须有后端注册表 + switch：否则点回后台运行中的会话会从 DB 重建新运行时，与那个还在跑的实例冲突。

### 自检（Linux）
- `test_conversation` 18/18（在 8.1/8.2a 基础上 +注册表登记/switch 复用/load 复用 live/空草稿回收/删除转草稿）。
- 全量回归 13 套全绿；`node --check web/app.js` 通过；后端公开方法面齐全（+switch_conversation）。
- GUI/真实并发流式只能 Windows 验。

### Windows 验证（整段 8.2）✅ 通过（2026-06-11）
- [x] 一个对话跑着时，点「+新会话」能立刻开新对话输入、不卡；旧对话后台继续跑。
- [x] 切回后台运行中的对话，能看到它的流式输出在继续；完成后会话行未读点亮、切过去看到完整结果。
- [x] 两个对话同时跑，输出各回各的视图、不串台；会话行运行中脉冲点正确。
- [x] 单对话从头到尾行为与之前一致（流式/工具/权限/思考块/工作区/记忆 toast）无回归。

> 定版 v1.1.0（2026-06-11）：同步更新 `pyproject.toml` 版本、`CHANGELOG.md`、`PRD.md` FR-8.2 状态。
> 下一步 P8 FR-8.3（后台权限路由 + `close()` 优雅停 worker + 可选停止/并发上限）。

---

## 2026-06-10 — P8 FR-8.2a 后端：每对话后台 worker + 非阻塞发送 + 事件 cid

**阶段**：P8（FR-8.2a，v1.1；FR-8.1 已 Windows 验证通过）
**状态**：✅ 后端实现完成、Linux 全回归 13 套全绿（含并发单测），前端（8.2b）待实现

### 做了什么
- `Conversation`：加 `queue.Queue` + 惰性启动/空闲退出的 worker 线程，串行消费发送任务（保回合
  顺序、复用同步 loop）；状态 `idle/queued/running`；`enqueue()` 入队即返回；单任务抛错不搞死 worker。
- 事件统一带 `cid`：每对话用绑定自身 cid 的 `self.emit`（闭包捕获 Resources.emit，避免自递归）；
  gate / loop 回调 / 各事件全部走 `self.emit`。
- `Api`：`send_message` 改为 `active.enqueue(...)` **非阻塞返回**；`_emit(event,data,cid)` payload 增 cid，
  并用 `_emit_lock` **串行化 evaluate_js**（多 worker 并发调用 WebView2 不保证线程安全）；cid 计数器分配。
- 前端**暂未改**：仍忽略 cid，单活动对话渲染照旧；`send()` 的 `await` 立即返回、流式仍由事件驱动，
  单对话行为无回归。并发 UX 放到 8.2b。

### 关键决策
- worker 用 `get(timeout)` + 空闲退出、`_worker_lock` 同时护住"启动"与"退出"，避免丢任务，也避免
  对话切换后留下常驻线程泄漏。
- evaluate_js 串行化（lock）是本段重点风险的对策；单测用"重入探测"的假 window 验证无并发重入。

### 自检（Linux）
- `test_conversation` +5 = 13/13：cid 唯一 / 入队非阻塞 / worker 串行保序且抗错 / send_message 异步 /
  事件带 cid 且 evaluate_js 无并发重入。全量回归 13 套全绿。

### 下一步
- FR-8.2b 前端：按 cid 分缓冲与路由、会话行状态角标、放开 streaming 全局封锁；交付后整段 8.2 一起 Windows 验。

---

## 2026-06-10 — P8 FR-8.1 抽出 Conversation 运行时（并发对话地基）

**阶段**：P8（FR-8.1，v1.1 第一段）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，行为与 1.0.0 一致）

### 背景
`Api` 原是单对话有状态单例（一份 session_id/history/workspace/registry/gate），`send_message`
同步跑完整个 agent 循环才返回——任务没结束开不了新对话，切会话踩共享状态。FR-8.1 先把
"每对话私有状态 + 逻辑"抽成独立运行时，为后续后台并发（8.2）铺地基。本段**不改对外行为**。

### 做了什么
- 新建 `bridge/conversation.py`：
  - `Resources`：跨对话共享的资源（config/store/memory/mcp/mcp_tools/limits/workspaces_root/
    per_session/emit）+ 跨对话账本 `extracted_upto`（记忆抽取进度）/ `conv_attempted`（已尝试生成
    规范的会话），按 session_id 记账、需在对话切换间存活，故放共享层。
  - `Conversation`：持 session_id/history/workspace/registry/gate/active_model/pending_workspace；
    承载 send_message 主循环、_budget、_effective_system、视觉预处理、自动生成规范、记忆自动抽取
    (capture_async)、工作区切换与只读预览。每对话**独立 gate**（_allow_all 即"本会话全部允许"）。
- 重写 `bridge/api.py`：`Api` 退化为**对话管理器**——持 `Resources` + 当前活动对话 `active`，
  公开方法（前端 js_api 面）全部转发到 `active`；`new_session/load_session/open_project` = 替换 `active`
  并触发旧对话 `capture_async`；`delete/rename/set_active_model` 行为与旧版逐一对齐。
- `bridge/__init__.py` 导出 `Conversation`/`Resources`。

### 关键决策
- 单活动对话语义、同步执行**完全保留**，行为与 1.0.0 一致——并发留到 8.2。
- `active_model` 移进 `Conversation`（为 8.2 各对话可用不同模型铺路），但 `set_active_model` 同步
  管理器默认值与当前对话，单活动场景下行为不变。
- 跨会话账本（extracted_upto/conv_attempted）放 `Resources` 共享层，保证 A→B→A 切换不重复抽取/生成。

### 自检（Linux）
- 新增 `tests/test_conversation.py` 8/8：两个 Conversation 的 history/workspace/gate/registry 互不串、
  allow_all 不跨对话泄漏、set_active_model 同步、new/load/delete/list 委派与隔离。
- 全量回归 **13 套全绿**（原 12 + 新增 1）；公开方法面 14 个全部保留；py_compile 通过。
- GUI / 真实模型 send_message 需 Windows 验（本机无 webview、无真 key）。

### 待 Windows 验证
- [ ] 流式对话、工具调用、权限确认条与"全部允许"行为与 1.0.0 一致。
- [ ] 新会话 / 切换会话 / 打开已有项目 / 删除会话 / 重命名 / 工作区面板均如常。
- [ ] 切会话后离开旧会话的记忆自动抽取仍触发（memory toast）。

### 遗留 / 下一步
- FR-8.2：每对话后台 worker + 非阻塞 send_message + `_emit` 带 conv_id + 前端按对话分缓冲/角标。
  ⚠ 多 worker 并发调 `window.evaluate_js` 线程安全性未知，8.2 要给它加锁串行化并重点 Windows 验。

---

## 2026-06-10 — P7 打包成 Windows exe（PyInstaller / onedir）

**阶段**：P7（FR-7.1，收尾 → 1.0）
**状态**：✅ 已交付，✅ 已在 Windows 真机构建 + 运行验证通过（2026-06-10，**定版 1.0.0**——首个正式版本）

### 背景
让用户免 Python 环境双击即用。难点：打包后路径全变——源码里 web/、config.yaml、.env、data/
都相对源码树，frozen 后失效。

### 做了什么
- **frozen-aware 路径** `paths.py`：`IS_FROZEN`/`BUNDLE_DIR`(只读捆绑，sys._MEIPASS)/
  `APP_DIR`(可写，exe 旁)。`config.py` 的 ROOT 改指 APP_DIR（data/config/.env 都在 exe 旁），
  `app.py` 的 web 改从 BUNDLE_DIR 取。`load_config` 首次运行从内置默认释放 config.yaml 到 APP_DIR。
- **打包配置** `hermes-dev.spec`（onedir）：datas 含 web/ + 默认 config.yaml + scripts/；
  hiddenimports 收全 webview/anthropic/openai/mcp/pydantic/pypdf + PIL（惰性导入/动态加载）；
  入口 `run.py`（把 src 加进路径再启动）；console=True（首构看报错，稳定后改 False）。
- **构建脚本** `build.ps1` + 指南 `docs/PACKAGING.md`（布局/首次运行放 .env/WebView2 前置/调试）。
- `.gitignore` 去掉 `*.spec`（我们的 spec 是源文件）。

### 关键决策
- onedir（非 onefile）：启动快、对 pywebview/WebView2 更稳；分发就压 dist\hermes-dev\ 文件夹。
- 只读资源进 exe、可写文件（config/.env/data）放 exe 旁——用户能改配置、密钥与数据持久。

### 自检（Linux，源码模式）
- `tests/test_paths.py` 3/3；路径解析 + 首次释放默认配置 冒烟通过；全量回归 12 套全绿
  （路径重构未破坏源码运行）。
- exe 实际构建/启动只能在 Windows 验。

### 构建期修复（Windows，2026-06-10）
- **exe 启动报 `The 'appdirs' package is required`**：pkg_resources/setuptools 的运行时依赖
  （appdirs / jaraco.* / packaging / more_itertools）PyInstaller 没自动收全，frozen 环境里缺失。
  修复：`hermes-dev.spec` 把 `pkg_resources` 加进 `collect_submodules`，并显式把
  `appdirs`、`jaraco.text/functools/context`、`packaging.version/specifiers/requirements`、
  `more_itertools` 补进 `hiddenimports`。
- 验证无误后把 spec 的 `console=True` 改 `False`，重打一次得发布版（无黑窗）。

### Windows 真机验证结果（2026-06-10，已通过 → 定版 1.0.0）
- [x] `.\build.ps1` 成功产出 `dist\hermes-dev\hermes-dev.exe`。
- [x] 双击运行：窗口正常、首次释放 config.yaml；放 .env 后能正常对话（ARK key 生效）。
- [x] 各功能在 exe 下可用：工具/会话持久化/打开已有项目/可视化/MCP（如配）等。
- [x] appdirs 报错已消除；console 改 False 后发布版无黑窗。

---

## 2026-06-10 — 打开已有项目文件夹（按会话绑定）+ 对话可选中复制

**阶段**：功能（让 hermes-dev 能在真实/已有代码库上开发，扛复杂软件的前置）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.5；含随后图标统一 +
顶部标题显示项目名/会话名两项 UI 优化）

### 背景
工具被沙箱限制在会话工作区内，粘绝对路径会被拒（实测）。要在已有项目上开发，必须让工作区
"就是"那个项目。用户要：左侧「+ 新会话」旁加 📂，以已有项目起新会话。

### 做了什么
- **DB**：sessions 加 `workspace` 列（绑定路径，NULL=默认 workspaces_root/<id>）；旧库 `_migrate()`
  自动 `ALTER TABLE ADD COLUMN`。`create_session(workspace=)` + `get_session_workspace()`。
- **bridge**：`open_project()` API——pywebview `create_file_dialog(FOLDER_DIALOG)` 选目录，像新会话
  一样清空、待落库，但工作区立刻指向所选项目（`_pending_workspace`），首条消息建会话时把路径写入
  workspace 列绑定。`load_session` 读绑定路径优先、否则默认隔离文件夹。`new_session` 清 pending。
  `delete_session` 不动外部项目文件夹（只删 DB+GC blob）。
- **前端**：左侧 `.sidebar-actions` 把「+ 新会话」与 `📂` 并排；`open_project()` 成功后清空视图、
  刷新会话与工作区面板。
- **顺带修**：对话内容可选中复制（`.chat/.bubble/...` 显式 `user-select: text`）。

### 关键决策
- 📂 = 起“新会话”绑定已有项目（而非改绑当前会话），与「+ 新会话」语义并列、避免孤儿空文件夹。
- 与"按会话隔离 / 缺 hermes.md 自动生成"互补：打开已有项目正好触发为其生成 hermes.md（若无）。

### 自检（Linux）
- test_p6_store +2（workspace 列、旧库迁移）= 7/7；绑定冒烟（建会话绑定/工具在项目内可读/默认会话
  用隔离文件夹/切回项目恢复绑定）；全量回归 11 套全绿；node --check + 编译通过。

### 待 Windows 真机验证
- [ ] 点左侧 📂 → 弹选目录框 → 选一个已有项目 → 面板显示其文件、路径栏为该项目。
- [ ] 让 Agent 读现有代码、edit_file 改、run_powershell 跑 → 都在该项目里生效。
- [ ] 切到别的会话再切回 → 回到该项目；新建空白会话仍是独立空文件夹。
- [ ] 删除该会话不会删掉你的真实项目文件夹。
- [ ] 对话内容可鼠标选中 + Ctrl+C 复制。

---

## 2026-06-10 — 修复 forget 后被自动抽取学回来（记忆"忘记墓碑"）

**阶段**：缺陷修复
**状态**：✅ 已修，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.4）

### 现象（用户报）
"记住我叫 X"跨会话生效（对）；但"忘记"后，当前会话说已忘记，**切换对话却仍记得名字**。

### 根因
`forget` 确实从 DB 删了、注入读实时 DB——但**离开会话的自动抽取**会从"你说过我叫X"的对话里
把名字**重新提取、重新 add 回记忆库**。forget 删一条，转头自动抽取又补一条。

### 修了什么
- MemoryStore 加 `forgotten` 墓碑表：`delete()` / 新增的 `forget_by_query()` 删记忆时，把被删内容
  （归一后）记入墓碑。`add()` 中 source 以 "auto" 开头（自动抽取）的，若命中墓碑则跳过——不再
  学回来；显式 `remember`（非 auto）不受限并会解除该内容墓碑。
- `forget` 工具支持 `id` 或 `query`：按关键词删除所有匹配记忆（"忘记我的名字"→ 一次清干净），
  避免多条残留。
- 测试：新增 `test_forget_tombstones_auto_recapture` / `test_forget_by_query`，更新按 id forget 的断言。

### 已知限制
- 墓碑按"归一后内容精确匹配"。若自动抽取换了措辞（如"用户的名字是X" vs "用户叫X"）仍可能漏挡；
  常见同措辞场景已覆盖。彻底方案需语义级匹配，暂不做。
- 对话历史本身仍含该信息：在原会话里模型仍可能从历史得知（无法抹除历史）；但其它会话不再被注入。

### 自检（Linux）
- test_p6_memory 10/10（含两个新用例）；全量回归 11 套全绿。

### 待 Windows 真机验证
- [ ] "记住我叫 X" → 新会话生效；"忘记我的名字"后，**切换/新建会话不再记得**。
- [ ] 再"记住我叫 X"能重新记上（墓碑被解除）。

---

## 2026-06-10 — 长期记忆只存"跨项目通用"事实（避免跨项目干扰）

**阶段**：体验/架构改进
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.4）

### 背景（用户提）
P6.3 长期记忆是一个全局库 + 自动抽取，问题在抽取口径——当初提示词明确要求记"项目目标/约束/
关键决定"，于是不同项目的项目专属事实都进了同一个全局库，跨项目互相干扰。用户洞察（与 Claude Code
一致）：全局记忆只该放跨项目通用的（用户/偏好/能力/特别强调）；项目相关的进各自 hermes.md。

### 做了什么（集中在提示词/口径，低风险）
- `longmem.py` `_EXTRACT_SYSTEM`：改为只抽"跨项目通用"事实（用户身份/称呼、长期偏好/工作习惯、
  反复强调的要求、技能能力倾向），**明确禁止记录任何项目专属内容**（归该项目 hermes.md）；
  只聊某具体项目时输出空。
- `tools/memory.py` `remember` 描述 + `config.yaml` system_prompt 记忆指引：同口径——项目专属
  写进 hermes.md，只有跨项目事实才 remember。
- 类别 `KINDS`：`project` → `skill`（能力/技能倾向）。旧库 kind="project" 的历史条目归一为 fact 显示，
  不自动迁移。`_KIND_LABEL` 同步。

### 关键决策
- 不做"按项目分库的记忆"（与 hermes.md 重叠、复杂）；用"全局只存跨项目 + 项目进 hermes.md"的
  分层，正好复用已隔离的工作区/hermes.md。
- 老库不自动迁移（无害；需要可手动清）。

### 自检（Linux）
- 更新 test_p6_memory 中用到旧 "project" kind 的两处用例；全量回归 11 套全绿；config 校验
  system_prompt 含跨项目指引。

### 待 Windows 真机验证
- [ ] 只聊某个项目（不涉及用户偏好）后切会话 → **不再自动记下项目类记忆**（toast 不弹或为空）。
- [ ] 说一句"记住我偏好 X"这类跨项目事实 → 正常进全局记忆并在新会话生效。
- [ ] 项目专属的东西模型倾向写进 hermes.md 而非 remember。

---

## 2026-06-10 — 工作区按会话隔离 + 缺 hermes.md 自动生成

**阶段**：架构改进（修复"生成规范卷入 hermes-dev 自身文件"的污染）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.3）

### 背景（用户在 Windows 实测发现）
手动生成 hermes.md 时，Agent 扫描了整个工作区——而默认工作区就是 hermes-dev 项目根，于是把
hermes-dev 自己的 CLAUDE.md/docs 都卷进生成结果。根因：所有会话共用同一个工作区（= 工具自身源码树），
项目之间没隔离。用户要求：每会话独立文件夹 + 缺 hermes.md 就按「全局标准+本会话项目」自动生成，去掉按钮。

### 做了什么
- **工作区按会话隔离**：`agent.per_session_workspace`（默认 true）+ `workspaces_root`（默认
  data/workspaces）。新会话首条消息建库时切到 `workspaces_root/<id>/`；切换会话切到其文件夹；
  未落库的新会话用 `_scratch` 暂存。bridge `_set_workspace` 重建文件类工具 registry（记忆/MCP 工具
  与工作区无关，复用），并 emit `workspace_changed`。显式 `agent.workspace` 则关闭隔离、固定用它。
  面板/规范注入本就读 self.workspace，自动跟随；前端顶部显示当前工作区路径。
- **缺 hermes.md 自动生成**：`agent.auto_conventions`（默认 true）。每轮结束后若工作区有内容但缺
  hermes.md 且本会话未尝试过 → 后台线程据「全局标准(system_prompt) + 本项目摘要」一次模型调用生成、
  写入工作区。新模块 `conventions.py`：`build_project_digest`（树+关键文件，带上限）、
  `build_generate_request`（强调只写本项目、不卷无关）、`clean_output`（去围栏）。
- **移除**之前临时加的「✨ 生成规范」按钮（按钮没必要）。

### 关键决策
- 隔离是根治：工作区 = 会话专属文件夹后，"扫描工作区"只扫本项目，污染自然消失。
- 自动生成走独立后台模型调用（不污染对话），与长期记忆抽取同模式；写到会话自己的隔离目录、
  不过权限 gate（应用自管的元数据）。
- 自动生成的 hermes.md 下一条消息即被项目规范注入读到并生效。

### 自检（Linux）
- `tests/test_conventions.py` 5/5（摘要含树+关键文件、上限、请求构造、去围栏）。
- 隔离冒烟：新会话工作区切到 workspaces_root/<id>、文件工具绑定到该目录；自动生成门槛
  （空→不触发、有内容缺规范→触发、已有→跳过）。全量回归 11 套全绿。

### 待 Windows 真机验证
- [ ] 新会话发首条消息 → 工作区变成独立文件夹（面板顶部路径显示 data/workspaces/<id>），
      hermes-dev 自身文件不再出现在工作区里。
- [ ] 让 Agent 在会话里建几个项目文件 → 该轮结束后自动生成的 hermes.md 出现（弹提示），
      内容只关于本项目、不含 hermes-dev 自身的东西。
- [ ] 切换/新建会话工作区随之切换；显式设 `agent.workspace` 时退回固定工作区。

---

## 2026-06-10 — UI 精修（Linear/Vercel）

**阶段**：体验增强
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.3）

> 注：本条曾临时加过「✨ 生成规范」按钮，随后按用户意见移除，改为「缺 hermes.md 自动生成」
> （见上一条 DEVLOG）。本条只保留 UI 精修。

### 做了什么
- **UI 精修（Linear/Vercel 精致暗色，纯 CSS）**：加设计 token（radius/shadow/accent-hover/
  hover-bg）；按钮分级（#send 主按钮填充强调色+投影+悬停微抬；其余 ghost 悬停淡底；图标按钮
  方形低调）；微交互（悬停淡染、按下回弹）；焦点改柔和辉光环（:focus-visible，鼠标点击不残留）；
  顶栏/输入区克制投影；气泡/卡片统一稍大圆角+细投影；权限“允许”更像主操作。不改结构/功能。

### 自检
- `node --check app.js` 通过；CSS 括号配平、无空规则/双分号；全回归不受影响（未动后端）。
- 实际观感需 Windows 验证（本机无显示）。

### 待 Windows 真机验证
- [ ] 按钮主次分明（发送醒目、其余低调）；悬停/按下/焦点的微交互自然；整体更精致不花哨。

---

## 2026-06-10 — 思考过程反馈（工作指示器 T1 + 推理流 T2）

**阶段**：体验增强（解决"长时间空白等待后回复突然弹出"的突兀感）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.2）

### 背景
用户反馈：发消息后常等很久、全程无反馈，然后回复突然弹出、吓一跳。根因：前端要等第一个
文本 token 才显示；而模型内部推理期/首 token 延迟期完全静默，provider 也没把推理流出来。

### 做了什么
- **StreamEvent 加 `thinking` 类型**（仅展示、不计入答案、不持久化）。
- **provider 接推理流**：OpenAI 读 `delta.reasoning_content`；Anthropic 由 `text_stream` 改为
  遍历原始事件、额外处理 `thinking_delta`（端点不产出则行为同旧、不报错）。
- **loop**：转发 thinking 事件（emit），不累加进 assistant_text。新增回归
  `test_agent_loop_thinking_emitted_not_in_answer`（thinking 转发但不入答案/历史）。
- **前端 T1 工作指示器**：发送即插入"思考中…"动画（跳动点）+ 已用时长计时；首个 chunk/
  thinking/tool 到达即隐藏；工具结束/上下文压缩/视觉完成等静默间隙重新显示；done/error 停。
- **前端 T2 思考块**：thinking 增量流入淡色可折叠「💭 思考过程」块（默认展开，答案/工具到来后
  自动折叠）；内容不持久化（重载不显示）。

### 关键决策
- T2 优雅降级：管线接上，模型/端点吐推理就显示、不吐也不报错，T1 始终兜底。
- thinking 不进历史/不入答案：它是过程不是结论。

### 自检（Linux）
- test_p3 9/9（含 thinking 用例）；全量回归 10 套全绿；`node --check app.js` 通过。
- GUI 实际观感需 Windows 验证。

### 待 Windows 真机验证
- [ ] 发消息后**立刻**出现"思考中… 已用 Xs"，不再全白等待。
- [ ] 用会思考的模型（如 deepseek-reasoner / 若 ark-kimi 产出 thinking）能看到「💭 思考过程」实时流。
- [ ] 工具往返之间也有"思考中"反馈；答案出来思考块自动折叠；报错/完成指示器消失。

---

## 2026-06-10 — 会话导航索引（右缘迷你刻度条）

**阶段**：体验增强（借鉴主流 Agent 的会话大纲/minimap）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.2）

### 背景
用户希望长会话里往前翻更方便——借鉴主流工具在滚动条一侧给一个"目录/索引"。

### 做了什么（纯前端，无后端改动）
- 布局：把 `<main#chat>` 包进 `.chat-area` 行容器，右缘加 `#chat-index` 刻度栏（在 chat 滚动条右侧）。
- `app.js`：`rebuildChatIndex()` 扫描 `.msg.user`，每条用户消息生成一个刻度；
  点击 `scrollIntoView` 跳转 + 目标短暂高亮（`.ci-flash`）；悬停用 JS 定位的浮动标签
  显示该条文字（避免被刻度栏裁切）。在发送 / `renderHistory` / 新会话三处刷新。
- 样式：刻度细条、悬停变 accent 色加宽；标签 3 行截断；空时刻度栏隐藏。
- 用户选了"迷你刻度条"样式（另一选项是文字目录）。

### 待 Windows 真机验证
- [ ] 多发几条消息后右缘出现刻度；悬停显文字、点击跳转并高亮。
- [ ] 切换会话 / 新会话索引正确刷新；长会话里翻找顺手。

---

## 2026-06-10 — 项目规范自动加载（hermes.md）+ 完善 system_prompt

**阶段**：体验增强（对标 Claude Code 的 CLAUDE.md 机制）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.2）

### 背景
用户希望用 hermes-dev 做开发时，不必每次重复交代要求——像 Claude Code 那样有：
全局标准（已有：config.yaml 的 system_prompt）+ 项目级规范文件（缺：自动读项目里的规范）。

### 做了什么
- **项目规范自动加载**：`workspace.read_conventions(root, name)`（纯函数，限工作区内、20000 字符上限）
  读工作区根的 `hermes.md`；bridge `_effective_system()` 改为组装 **基础 system + [项目规范] + 长期记忆**，
  每次发消息读最新（改 hermes.md 即生效）。`agent.conventions_file` 配置（默认 `hermes.md`，""=关闭）。
- **完善 system_prompt**：参考全局开发标准补「开发规范」段（先读后改/分段推进/最小改动/贴合风格/
  重视可测与质量/如实报告/沟通简洁/危险操作先确认），并声明工作区 `hermes.md` 优先。

### 关键决策
- 复用既有「注入 system」管线（与长期记忆同路）；规范属“规矩”放 system，事实仍走 memory。
- 每次读最新而非启动缓存：用户随时改 hermes.md 即时生效，符合直觉。

### 自检（Linux）
- `tests/test_workspace.py` +`test_read_conventions`（不存在/关闭/读到去空白/越界不读/超长截断）= 8/8。
- 桥冒烟：无 hermes.md 只注入通用规范；放 hermes.md 自动出现 [项目规范]+内容；改文件下条即生效。
- 全量回归 10 套全绿；config 校验 `conventions_file=hermes.md`、system_prompt 含开发规范。

### 待 Windows 真机验证
- [ ] 在某工作区放 `hermes.md`（写几条项目规则）→ 让 Agent 干活，观察它是否遵守该规范。
- [ ] 改 `hermes.md` 后下一条消息即生效；删掉则回到通用规范。

---

## 2026-06-09 — 右侧工作区文件预览面板（只读）

**阶段**：体验增强（参考主流 Agent 的三栏布局）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.1）

### 背景
用户希望像主流 Agent 那样：左=会话栏、中=对话、右=工作区文件预览。配合刚做的
「写 mockup.html 高保真原型」，右侧直接渲染预览即闭环。

### 做了什么
- **新模块** `workspace.py`（纯函数、可单测）：`build_tree`（目录树，跳过噪音目录、深度/数量上限）、
  `resolve_within`（路径解析 + 越界拒绝）、`read_file`（按类型读：text/html/image/binary，
  大文件截断；SVG 当图）。
- **bridge 只读 API**：`get_workspace_tree` / `read_workspace_file` / `open_workspace_file`
  （后者用 `webbrowser` 在系统浏览器打开）；都强制限制在 `agent.resolve_workspace()` 内。
- **前端**：第三列 `.workspace-panel`（HTML/CSS），上半文件树（目录可折叠）、下半预览区。
  预览：代码→hljs 高亮、图片→`<img>`、**HTML→`sandbox="allow-scripts"` iframe srcdoc 渲染**
  （可切「源码」、可「在浏览器打开」）。每轮 Agent `done` 自动刷新树；面板可折叠，状态存 localStorage。

### 关键决策
- **只读**：本期只预览不编辑（编辑/保存/冲突另算）。
- **HTML 用 sandbox iframe**：`allow-scripts` 让原型 JS 能跑，但隔离于主程序、不可同源/导航。
- 路径安全沿用工具的「限制在工作区内」思路，越界一律拒绝（已单测多种 `../` 逃逸）。

### 自检（Linux）
- `tests/test_workspace.py` 7/7（路径越界拒绝、树跳过噪音、文本/HTML/图片/二进制/截断/缺失）。
- 桥集成冒烟：树/HTML 预览正确；`../`、`../../etc/passwd`、`sub/../../` 等越界全被拒、正常文件可读。
- `node --check app.js` 通过；全量回归 10 套全绿。

### 待 Windows 真机验证
- [ ] 右侧出现工作区文件树，点代码/图片/MD 能预览。
- [ ] 让 Agent 写 mockup.html → 树自动刷新出现该文件 → 点开 iframe 渲染出原型 → 「在浏览器打开」可用。
- [ ] 折叠/展开正常（重启后保持）；点「源码/预览」切换。

---

## 2026-06-09 — 修复 max_tokens 截断导致的工具调用死循环

**阶段**：缺陷修复
**状态**：✅ 已修，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.1）

### 现象（用户报）
让 ark-kimi 写「待办应用高保真原型」到 `mockup.html`：UI 反复出现 `write_file {"path":"mockup.html"}`，
文件一直 0kb、循环不停。

### 根因
高保真 HTML 很长，写它的 `write_file` 入参（含整份 HTML 的 `content`）超过模型 `max_tokens`
（ark-kimi 当时 4096）被截断 → `tool_use` 的 JSON 入参不完整、`content` 缺失。但 agent 循环
收到 `done` 时**没看 `stop_reason`**，照常执行了残缺的工具调用 → write_file 写出空文件
（`content` 默认 ""）→ 返回「已写入 0 字符」→ 模型见空又重试 → 每次都同样被截断 → 死循环到 max_steps。

### 修了什么
- `agent/loop.py`：捕获 `done` 的 `stop_reason`；若为 `max_tokens`/`length`（截断），记下已生成
  文本、emit 明确错误并**停止**，不执行被截断的 tool_use。新增回归
  `test_agent_loop_truncated_tool_not_executed`（残缺 write_file 不执行、不写空文件、只跑一轮）。
- `providers/openai_p.py`：`finish_reason=="length"` 时如实上报 `stop_reason="length"`（此前被
  「有 tool_call 就报 tool_use」覆盖，同样会触发死循环）。
- `config.yaml`：`ark-kimi` `max_tokens` 4096 → 16384（让正常的高保真 HTML 能一次写完）。

### 自检（Linux）
- test_p3 8/8（含新截断用例）；全量回归 9 套全绿；config 校验 ark-kimi max_tokens=16384。

### 待 Windows 复测
- [ ] 重跑「写 mockup.html 高保真原型」：应一次写完、文件非空；若仍超 16384，会明确报「被截断，
      请调高 max_tokens 或分步写」而非死循环。

---

## 2026-06-09 — 可视化输出增强（SVG / Mermaid / HTML 稿）

**阶段**：体验增强（非编号阶段）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-10，定版 0.9.1）

### 背景
用户想让 Agent 在对话里直接产出 UI 稿 / 交互稿。现接的都是文本模型，不能生成位图；
但前端 markdown（marked + innerHTML）本就会渲染 HTML/SVG。于是不接文生图，而是打通
「文本可渲染成图」的几条路：SVG（直接渲染）、Mermaid（流程/交互图）、HTML 文件（高保真原型）。

### 做了什么
- 前端引入 `mermaid@10.9.1`（CDN，暗色主题，`startOnLoad:false` 手动渲染）。
- `app.js`：新增 `renderMermaidIn(el)`——把 `code.language-mermaid` 渲染成 SVG 图；
  在气泡定稿（`finalizeTextBubble`）与加载历史（`renderHistory`）时调用，**流式途中不渲染**
  （图未写完会报错）；失败保留原始代码块。代码高亮跳过 mermaid 块。离线无 mermaid 时降级。
- `style.css`：`.mermaid-diagram` 与内联 `<svg>` 的显示样式（限宽、底色、居中）。
- `config.yaml` system_prompt 加「可视化输出」指引：UI 稿用 `<svg>`（别放代码块）、流程/交互图
  用 ```mermaid、高保真原型用 write_file 写自包含 .html，并先问清平台/深浅色/尺寸/核心元素。

### 关键决策
- 不接文生图模型（重、需新后端）；优先打通文本→可渲染图（零模型成本、即时）。
- Mermaid 只在「定稿/历史」渲染，避开流式中途语法不全导致的报错与闪烁。
- SVG 走既有 marked HTML 渲染，无需额外处理，仅加样式。

### 自检（Linux）
- `node --check web/app.js` 语法通过；config 加载正常、system_prompt 含 svg/mermaid 指引。
- 后端全回归 9 套全绿（本次仅动前端 + config 注释/提示，未碰内核）。
- GUI 实际渲染效果需 Windows 验证（本机无显示）。

### 待 Windows 真机验证
- [ ] 让模型「用 SVG 画一个登录页 UI 稿」→ 对话里出现矢量图（非源码）。
- [ ] 让模型「用 mermaid 画下单流程的交互流程图」→ 渲染成图。
- [ ] 让模型「写一个待办应用原型到 mockup.html」→ write_file 落盘，浏览器打开可用。
- [ ] 流式过程中 mermaid 不报错/不闪；切走再切回会话，图仍在（历史渲染）。

---

## 2026-06-09 — P6.4 MCP 工具接入（作为客户端）

**阶段**：P6.4（FR-6.4）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-09，定版 0.9.0）

### 背景
接入 MCP（Model Context Protocol）生态：hermes-dev 作为客户端连接外部 MCP server，
把其工具自动接进 Agent 工具循环，复用现成能力而非自己重写。详见 ADR-0013。

### 做了什么
- **新包** `mcp_client/`（不叫 `mcp`，避免与 SDK 顶层包混淆）：
  - `tool.py`：`McpTool(Tool)` 适配器 + 纯函数 `convert_result`（MCP 内容→text/image 块/ok）、
    `qualified_name`（`服务名__工具名`）。外部工具默认 `dangerous=True`，trust 的 server 免 gate。
  - `manager.py`：`McpManager`——常驻后台线程跑 asyncio loop，`run_coroutine_threadsafe().result()`
    同步取结果；每 server 一个常驻 `_serve` 协程（同 task 进入/退出 async context，避开
    anyio cancel-scope 陷阱），`stop_event` 控制保活/收尾。
- **配置** `MCPConfig` + `McpServerConfig`（形状对齐 Claude Desktop 的 mcpServers：
  command/args/env/cwd/trust）。config.yaml 增 `mcp:` 段 + 注释样例（filesystem / git / echo）。
- **接入**：`build_registry` 加 `mcp_tools` 入参；`Api.__init__` 启动 manager、收集工具注册，
  失败不拖垮启动；`Api.close()` + `app.py` 在窗口关闭后收尾（终止子进程、关存储）。
- **新依赖** `mcp>=1.2`（SDK 仅在 manager 方法内惰性导入：未装且 enabled=false 时应用照常）。
- **零 Node 验证脚本** `scripts/mcp_echo_server.py`（FastMCP，echo/add 两工具），Windows 上
  不装 Node 也能端到端验 MCP。

### 关键决策
- 仅 stdio + 仅 tools（HTTP/SSE、resources/prompts 延后）。
- 外部工具默认危险、逐次过 gate；`trust:true` 免确认。延续项目安全姿态。
- 同步内核 × 异步 SDK：常驻后台 loop + 每 server 常驻 `_serve` 协程（cancel-scope 安全）。
- 故障隔离：坏 server 跳过、运行期调用失败转 ToolError 回灌模型，绝不拖垮 app。

### 自检（Linux）
- `tests/test_p6_mcp.py` 12/12：convert_result（文本/图片/错误/空）、命名、McpTool.run
  （文本/图片→ToolOutput/错误→ToolError/异常→ToolError）、MCPConfig 解析、registry 接入+危险标记。
- **真 server 端到端**（用 venv 的 python 跑 `scripts/mcp_echo_server.py`）：连接→list_tools→
  调用 echo/add→错误参数→close 全通过；另验「坏 server（命令不存在）被跳过、好 server 不受影响」。
- 「系统无 mcp SDK + mcp.enabled=false」下 `Api` 起停正常（证明禁用路径不依赖 SDK）。
- 全量回归 9 套 + 新 12 = 全绿；`py_compile` 通过。

### 验证结果（2026-06-09，Windows 真机）
- [x] `pip install -e .` 带上 mcp SDK。
- [x] 用 `scripts/mcp_echo_server.py`（零 Node）配 echo server，模型调用 `echo__echo`/`echo__add`
      出工具块、过权限 gate、结果正确。
- [x] 关掉应用时 MCP 子进程随之退出（无残留进程）。
- [x] `mcp.enabled=false` 时一切如常。

### 遗留
- 仅 stdio + tools；远程传输与 resources/prompts 后续。
- 启动时一次性拉工具列表，不处理运行期 list_changed。
- server 多/慢会拖慢启动（阻塞式逐个连，受 connect_timeout 约束）。

---

## 2026-06-09 — P6.3 长期记忆（跨会话）

**阶段**：P6.3（FR-6.3）
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-09，定版 0.8.0）

### 背景
P6.1 持久化、P6.2 压缩都只作用在「单个会话内」。跨会话反复出现的事实（称呼/偏好、
项目目标与约束、关键决定）每开新会话都要重讲，模型也记不住。做长期记忆——跨会话、
跨重启持久，并能在新会话自动被用上。详见 ADR-0012。

### 做了什么
- **新存储** `store/memory.py`（`MemoryStore`）：独立 SQLite 文件 `data/memory.db`，
  与会话库 `hermes.db` 解耦、自带连接 + 锁。条目 `{id, content, kind, source, ts}`，
  `kind ∈ {user, preference, project, fact}`，按 content 折叠空白去重。CRUD + search。
- **新纯逻辑** `longmem.py`（可单测、不碰网络）：`build_memory_block`（注入 system 的记忆块，
  带条数/字符预算）、`build_transcript`（对话展平成文本）、`build_extract_request`
  （抽取用 system+messages）、`parse_memories`（解析模型 JSON 输出，容错去围栏/截取）。
- **记忆工具** `tools/memory.py`：`remember / recall / forget`（非危险、不过权限 gate，
  作为工具块在 UI 可见）。`build_registry` 加 `memory_store` 入参注册之。
- **bridge 接入**：
  - 注入：`_effective_system()` = 基础 system + 记忆块，`_budget` 改用它（注入后再走 P6.2 压缩）。
  - 自动抽取：`new_session` / 切换 `load_session` 时，后台线程 `_capture_worker` 把刚结束的
    对话（仅自上次抽取以来的新消息）喂模型抽要点、去重入库，完成发 `memory_captured` 事件。
    `_extracted_upto[session_id]` 占位防并发重复；失败静默。
- **前端**：`memory_captured` 事件 → 浮层 toast（抽取发生在离开会话后，聊天区已切走，
  故用 toast 而非聊天气泡）。记忆工具调用复用既有工具块渲染。
- **配置** `MemoryConfig`：`enabled / auto_capture / db_path / max_inject /
  max_inject_chars / min_messages_to_capture`。config.yaml 增 `memory:` 段。

### 关键决策
- 记忆是全局事实 → 独立库，不与会话表混、不受删会话/GC 影响（ADR-0012）。
- 三路径并存：模型工具（主动）+ 离开会话自动抽取（省心）+ 每次发消息注入（让新会话生效）。
- 抽取触发点选「离开会话」这一自然边界，而非每轮——省成本、且要点已定。
- 召回先用「全量按预算注入 + 关键词 recall」，不引向量库（条数有限够用；保留升级空间）。

### 自检（Linux）
- `tests/test_p6_memory.py` 8/8：MemoryStore CRUD/去重/持久化、注入块预算、转录、
  抽取请求、解析多形态、记忆工具。
- 集成冒烟（无网络）：工具已注册、注入生效、`_budget` 带记忆、capture 守卫（消息太少/
  无 sid 不触发、占位防重复）全部 OK。
- 全量回归 7 套（P3/P4/P4vision/P5blobs/P5screenshot/P6context/P6store）+ 新 8 = 全绿。
- `py_compile` 全 src + 新测通过。

### 验证结果（2026-06-09，Windows 真机）
- [x] 模型按需调用 remember/recall/forget（工具块可见、结果正确）。
- [x] 新建/切换会话后弹出「🧠 已记入长期记忆 N 条」toast；重开应用后新会话能在回答里用上旧记忆。
- [x] `data/memory.db` 正常生成、跨重启保留。
- [x] 关 `memory.enabled` / `auto_capture` 行为符合预期。
- 注：自动抽取会多一次模型调用（用当前 active_model），需联网与有效 key。
- 附带产物：`scripts/check_compression.py`——体检每个会话是否会触发 P6.2 压缩
  （确认默认预算 32000 较高，日常聊天通常不触发；短消息因摘要≈原文压缩近乎无效）。

### 遗留
- 无 GUI 记忆管理面板（本期范围外，靠 recall/forget 工具）。
- 抽取占位在内存，进程重启清零；靠 add 去重兜底。
- 注入为「全量按预算」，非语义相关性排序。

---

## 2026-06-09 — P5.1 图像外置 blob 存储

**阶段**：P5.1（FR-5.1.1）
**状态**：✅ 已实现、Linux 自检通过、✅ Windows 真机验证通过（2026-06-09，定版 0.7.0）

### 背景
P6.1 把 image 块 base64 全量存进 DB；P5 让 Agent 会自动反复截图，DB 膨胀风险升级。
把图片移出 DB（详见 ADR-0011）。

### 做了什么
- **新模块** `store/blobs.py`（纯函数）：`dehydrate`（落库前 base64→blob 引用并写盘）、
  `rehydrate`（读出 blob 引用→base64）、`collect_refs` + `gc`（孤儿回收）。按 sha256 去重；
  引用块用 `source.type=="hermes_blob"` + `ref` 文件名（内部用，绝不发给 provider）。
- **Store 接入**：`add_message` 落库前 dehydrate、`get_messages` 读出后 rehydrate、
  `delete_session` 后 `_gc_blobs_locked()` 全量扫描引用删孤儿。blob 放 `data/blobs/`。
  `Store(..., externalize_images=bool)`。
- **config**：`StorageConfig.externalize_images`（默认 true）；config.yaml `storage` 段加注释。
  bridge 构造 Store 时传入开关。
- 内核 / provider 不变：始终只见完整 base64，转换全在存储层透明完成。

### 关键决策（详见 ADR-0011）
- 外置 blob + DB 存引用 + 读出 rehydrate（保真，重开会话仍能把图喂模型）。
- sha256 去重；GC 用全量引用扫描（本地小库够用、自愈，不上引用计数）。
- 递归转换覆盖「tool_result + image 并列」等结构；向后兼容旧库内联 base64。

### 自检（Linux）
- `py_compile` 通过。`tests/test_p5_blobs.py` **8/8**：dehydrate/rehydrate 往返、去重、
  并列块只外置 image、Store 往返且 DB 内无 base64 只有引用、GC 删孤儿留共享图、
  blob 缺失优雅降级、关闭开关退回内联、向后兼容纯内容。
- 全回归绿：test_p3 7/7、test_p4 9/9、test_p4_vision 9/9、test_p5_screenshot 5/5、
  test_p6_store 5/5、test_p6_context 5/5。

### 待验证（Windows 真机）
- [ ] 发含图/截图的会话 → `data/hermes.db` 不再随图膨胀、`data/blobs/` 出现文件。
- [ ] 关掉重开会话，图片完整恢复、能再次喂给 ark-kimi 识别。
- [ ] 删除会话后，该会话独有的图 blob 被回收、其它会话的图不受影响。

### 遗留 / 风险点
- GC 每次删会话全量扫描消息（当前规模可忽略；超大库再上引用计数）。
- 旧库已内联的 base64 不自动迁移（向后兼容不影响使用；需要可另写一次性迁移脚本）。

---

## 2026-06-09 — 已完成阶段查漏补缺（重命名入口 / temperature / 清理）

**阶段**：维护（补已完成阶段的遗漏项）
**状态**：✅ 已实现、Linux 自检通过、✅ Windows 真机验证通过（2026-06-09，定版 0.7.0）

### 背景
审计已完成阶段发现几处遗漏：① P6.1 会话重命名后端有、前端无入口；② 模型档案缺
`temperature`（不可配采样温度）；③ P4 的 `_debug_attachments` 开发诊断未清理。本次补齐。

### 做了什么
- **会话重命名前端入口**：会话项悬停显示 ✎，点击把标题换成输入框内联编辑，Enter/失焦提交
  调 `rename_session` 后刷新，Esc 取消。补上 `.session-ren` / `.session-rename-input` 样式。
- **temperature 可配**：`ModelConfig.temperature: float | None`，经 `build_provider` →
  `BaseProvider` → 两个 provider 的 stream_chat（设了才传，None 用 provider 默认）。
  config.yaml 在 minimax 档案加注释示例。
- **清理**：删除 `bridge._debug_attachments` 方法及其调用（每条消息往 stderr 打附件摘要）。

### 自检
- `py_compile` 全过；temperature 贯通验证（设 0.3 → provider.temperature==0.3，未设为 None）。
- 全回归绿：test_p3 7/7、test_p4 9/9、test_p4_vision 9/9、test_p5_screenshot 5/5、
  test_p6_store 5/5、test_p6_context 5/5。

### 同时确认 / 登记
- ✅ **历史遗留「图像端到端识别」已由 ark-kimi 原生视觉解决并验证**，正式划掉。
- 📌 **新登记 P5.1 存储优化**：图像 base64 全量存 DB 的膨胀问题，因 P5 起 Agent 会自动产图而
  升级；拟用「外置 blob 存储 + DB 存引用 + load 时 rehydrate」根治（方案 A）。优先级中，
  排在 P5 真机验证后。

### 待验证（Windows 真机，可与 P5 同批）
- [ ] 会话项悬停出现 ✎，点击能改名、列表刷新、重启后标题保留。
- [ ] 给某档案配 `temperature` 后行为符合预期。

---

## 2026-06-09 — P5 Agent 主动截屏工具（重定范围）

**阶段**：P5（FR-5.1，重定为「Agent 截屏工具」）
**状态**：✅ 已实现、Linux 自检 + 真模型端到端通过、✅ Windows 真机验证通过（2026-06-09，定版 0.7.0）

> 已知限制（验证中发现）：`agent.screenshot: false` 只移除专用 take_screenshot 工具，
> 模型仍可绕道 run_powershell 截屏（会弹其权限条）。修法已定（system 提示引导模型直接拒绝），
> 用户决定暂缓，列为 P5 遗留已知问题。

### 背景 / 范围调整
- 原 P5「全局热键截屏 + 区域选择 UI」与 Windows 自带 `Win+Shift+S` + 现有粘贴链路重复，
  价值有限。重定为：**放弃手动截图 UI，只做 `take_screenshot` 工具**，让 Agent 在循环里
  主动截屏看屏（自主任务）。详见 ADR-0010。

### 做了什么
- **工具框架扩展**：`ToolOutput(text, blocks)`——工具可返回富内容块（如 image）。普通工具
  仍返回 `str`，向后兼容。`Tool.run` 返回类型放宽为 `str | ToolOutput`。
- **截屏工具** `tools/screenshot.py`：`ScreenshotTool`（`take_screenshot`），Pillow `ImageGrab`
  截全屏、长边缩到 1568px、返回 image 块。`dangerous=True`（过权限 gate）；无显示/无授权
  时优雅抛 `ToolError`。`build_registry(..., screenshot=bool)` + `config.agent.screenshot` 总开关。
- **agent 循环**：`_exec_tool` 改返回 `(text, ok, blocks)`；把 `blocks` 作为**并列块**追加到
  本轮 tool_result 所在的同一条 user 消息（`content=[tool_result, image, ...]`）。
- **bridge**：build_registry 传入 `screenshot` 开关。**前端**：`tool_result` 事件带 `image` 时
  工具块内显示缩略图（`.tool-image`）。
- **依赖**：pyproject 加 `pillow>=10.0`。config.yaml `agent.screenshot: true`。

### 关键决策（详见 ADR-0010）
- **图片注入用「tool_result 同消息并列块」**：实测火山方舟端点**不解析 tool_result 内嵌
  image**（模型说看不到图）；把 image 作为并列块放进同一条 user 消息则可正常识图。
  这是本阶段最关键的技术结论，决定了 ToolOutput / 循环的注入方式。
- 截屏只做工具、不做手动 UI（与系统截图重复）。隐私敏感 -> 危险工具 + 总开关。

### 自检（Linux 开发环境）
- `python -m py_compile` 通过（base/screenshot/registry/loop/config/bridge）。
- `tests/test_p5_screenshot.py` **5/5**：工具开关、图片并列块注入、gate 放行后注入、
  gate 拒绝则无图、headless 优雅降级。回归 test_p3 7/7、test_p4 9/9、test_p4_vision 9/9、
  test_p6_store 5/5、test_p6_context 5/5 全绿。
- **真模型端到端**（ark-kimi，真实 AgentLoop + 真 provider，截屏桩返回测试图）：模型调用
  `take_screenshot` → 循环构造消息 → 端点接受 → kimi 准确描述截图（文字/形状/颜色）。

### 待验证（Windows 真机）
- [ ] 让 ark-kimi「截屏看看我屏幕」，弹权限确认条，授权后真实截到屏、模型能描述。
- [ ] 拒绝授权时不截屏、模型收到拒绝提示。
- [ ] `agent.screenshot: false` 时工具消失、模型无法调用。
- [ ] 非视觉模型（如 minimax）调用时的表现（预期：拿到看不懂的图，属已知限制）。

### 遗留 / 风险点
- 需视觉模型才有意义；非视觉主模型调用拿到看不懂的图（已知限制，未按模型过滤）。
- 截图 base64 随会话落库，长期 DB 膨胀（同 P6.1 图像存储问题）。
- 多显示器 / 含 Hermes 窗口自身等真机行为待验证。
- 后续若要区域裁剪，可在前端用 canvas 对已截全屏裁剪，无需新平台代码。

---

## 2026-06-09 — 新增火山引擎方舟（Volces Ark）集合模型

**阶段**：配置增补（无内核改动）
**状态**：✅ Linux 真连通验证通过（含 tool-use 完整往返）、✅ Windows 真机回归通过（2026-06-09，随 0.6.0）

### 做了什么
- `config.yaml` 加 5 个走 **Anthropic 协议**的模型档案，统一端点
  `https://ark.cn-beijing.volces.com/api/coding`（SDK 自动接 `/v1/messages`）、
  共用密钥环境变量 `ARK_API_KEY`：
  - `ark-doubao`（doubao-seed-2.0-pro）/ `ark-glm`（glm-5.1）/ `ark-minimax`（minimax-m2.7）
    / `ark-kimi`（kimi-k2.6）/ `ark-deepseek`（deepseek-v4-pro）。
- `.env.example` 加 `ARK_API_KEY` 占位；本地 `.env`（gitignore 内）写入真实 key。
- `vision` 全部按 `false`（未确认图像能力）；`active_model` 不变（仍 `minimax`），UI 下拉选用。

### 关键决策
- 无需新 provider：方舟 coding 端点兼容 Anthropic 接口，复用现有 `anthropic` 适配。
- 一端点多模型：5 档仅 `model` 字段不同，端点/key 共用，切模型零成本。

### 自检（Linux 开发环境，真连外网）
- 纯文本连通：5 个模型各发一条最小请求，**全部正常返回**。
- 单步 tool-use（项目真实 7 件工具 schema）：doubao / glm / minimax 均 `stop=tool_use`、
  正确发起 `list_dir`。
- 完整往返（ark-glm）：`tool_use` → 本地真执行工具 → 回灌 `tool_result` →
  `stop=end_turn` 给出基于结果的最终答复。**证实方舟端点完整支持 Anthropic tool-use 协议**，
  满足 `AgentLoop` 契约。
- config 解析正常，5 档全部加载，`ARK_API_KEY` 解析成功。

### 待验证（Windows 真机）
- [ ] Windows 那份 `.env` 补 `ARK_API_KEY` 后，下拉切到 `ark-*` 能正常对话。
- [ ] 用 `ark-*` 跑一轮带工具（读/写文件、run_powershell）的真实任务。

### 遗留 / 备注
- 这些模型的图像能力未测；若某档实测可识图，改其 `vision: true`。
- 与 P6.2 同包交付，等 Windows 一并回归。

---

## 2026-06-09 — P6.2 上下文 token 预算与压缩

**阶段**：P6.2
**状态**：✅ 已实现、Linux 自检通过、✅ Windows 真机验证通过（2026-06-09，定版 0.6.0）

### 做了什么
- **新模块** `context.py`：启发式 token 估算（ASCII≈4 字符/token、中文≈1 字符/token、
  图片块固定值，无新依赖）+ `compress()`：超预算时从最早回合整段丢弃直到 ≤ 预算、
  至少保留最近 `keep_recent_turns` 个回合，被丢弃内容压成摘要追加进 system。
- **裁剪点只落在「真实用户回合」边界**（`_is_user_turn`：role==user 且不含 tool_result），
  绝不从一次 tool-use 往返中间切断，保证 tool_use/tool_result 配对完整。
- **config**：`ContextConfig`（`enabled` / `max_input_tokens` 默认 32000 /
  `keep_recent_turns` 默认 6）；config.yaml 加 `context` 段。
- **bridge**：新增 `_budget()`；`send_message` 在进 loop 前压缩喂给模型的副本，
  **不动 `self.history`/DB**；改写写回逻辑——按「压缩后喂入条数 `n_in`」从 loop 结果切出
  本轮新增消息 extend 回完整 history 并落库（避免压缩污染历史或错位持久化索引）。
- **前端**：`context_compressed` 事件 → 一条 `.context-note` 提示（省略条数/前后 token/预算，
  注明完整历史仍在会话中）。

### 关键决策（详见 ADR-0009）
- 启发式估算而非引 tiktoken / 联网计数——只为预算决策，够用且零依赖。
- 截断式摘要（零模型调用），不做语义摘要——避免额外延迟/成本；语义摘要留作后续增强。
- 压缩只作用于喂模型的副本，持久化历史始终完整。

### 自检（Linux 开发环境）
- `python -m py_compile` 通过（context/config/bridge）。
- `tests/test_p6_context.py` **5/5**：token 估算、未超预算不压、超预算丢旧留近窗+摘要、
  tool 配对边界不被切断、单个超大回合不裁。回归 test_p6_store 5/5、test_p3 7/7、
  test_p4 9/9、test_p4_vision 9/9 全绿。
- bridge 冒烟：16 条 history（budget 调小强制触发）→ 喂模型 4 条；`self.history` 未被
  压缩改动；模拟 loop 写回后完整历史保留 + 本轮新增正确落位。

### 待验证（Windows 真机）
- [ ] 长会话（多轮累积）发消息时出现「🗜 已压缩」提示，模型仍能基于近窗 + 摘要正常回答。
- [ ] 关掉再重开，左栏会话历史仍完整（压缩不影响持久化）。
- [ ] 含工具调用的长会话压缩后不报 tool 配对错误。
- [ ] `context.enabled: false` 时行为同以前（不压缩）。

### 遗留 / 风险点
- token 为估算值，极端构成可能偏差；`keep_recent_turns` 是下限，最近回合本身超预算时
  实际输入仍可能超（已在 ADR 记为已知限制）。
- 压缩只在每轮进 loop 前做一次；单轮内多步工具往返的增长暂未压缩。
- 语义摘要（用模型压旧段）后续可作为可选增强；被裁内容未来可改为进 P6.3 长期记忆。

---

## 2026-06-09 — P6.1 会话历史持久化（SQLite）

**阶段**：P6.1
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-09）

### 做了什么
- **存储层** `store/db.py`：`Store` 封装 sqlite3（标准库，无新依赖）。`sessions` +
  `messages` 两表，content 以 JSON 存取；`check_same_thread=False` + 锁；CRUD +
  `make_title`（首条文本截断生成标题）。
- **config**：`StorageConfig`（enabled / db_path，默认 `ROOT/data/hermes.db`）；
  config.yaml 加 `storage` 段；`.gitignore` 加 `data/`、`*.db`。
- **bridge**：构造 `Store`；`send_message` 首条消息建会话、用户消息即时落库、
  整轮 assistant/tool 消息落库、`touch_session`。新增 `list_sessions`/`load_session`/
  `delete_session`/`rename_session`；`new_session` 置空 session_id；emit `session_created`。
- **前端**：左侧会话栏（+新会话 / 列表 / 切换高亮 / 悬停删除）；`renderHistory` 重渲染
  持久化历史（text→气泡、image→缩略图、tool_use/tool_result→静态工具块）；
  `done`/`session_created` 后刷新列表。整体改 flex 左右布局。

### 关键决策（详见 ADR-0008）
- SQLite + JSON content，与内核 content blocks 同构、零转换。
- 启动不建空会话，首条消息才建（避免空会话堆积）；消息即时落库防中途丢失。
- 持久化可由 `storage.enabled` 关闭（退化为纯内存）。

### 自检（Linux 开发环境）
- `python -m py_compile` 全部通过。
- `tests/test_p6_store.py` **5/5**：建会话/列表、含 blocks 的消息 JSON 往返、重命名、
  删除级联、标题截断。回归 test_p3 7/7、test_p4 9/9、test_p4_vision 9/9。
- bridge 冒烟（mock provider + 临时 db）：发 2 条消息 → 存 4 条 → 新建 Api 实例
  （模拟重启）→ list/load 恢复 4 条、标题正确 → 删除归零。

### 验证结果（2026-06-09，Windows 真机）
- [x] 发消息 → 关应用 → 重开 → 左栏有会话、历史完整恢复。
- [x] 多会话独立、来回切换正常。
- [x] 删除会话 → 列表移除、消息不再出现。
- [x] 标题自动取首条消息；切换后续写正常。

### 遗留 / 风险点
- 图像 blocks 仍按完整 JSON（含 base64）存，长期可能让库膨胀，后续可优化（截断/外存）。
- 会话重命名后端已支持（`rename_session`），前端暂未加入口，后续可补。

---

## 2026-06-09 — P4.1 视觉预处理回退（让 M2.7 也能"看图"）

**阶段**：P4.1
**状态**：✅ 实现交付（v0.4.0），机制真机验证通过（成功触发预处理、失败优雅回退）。
**默认关闭**：MiniMax `coding_plan/vlm` 需编程套餐凭证，用户的开放平台 key 调不通，
图像识别端到端**搁置**，待拿到可访问 vlm 的凭证或换视觉源再启用。

### 缘起
P4 排查确认 M2.7 不直接消费图像；用户指出 OpenClaw 能用 M2.7 识图，是其 minimax
插件先用 `MiniMax-VL-01` 把图转文字描述再喂 M2.7，并提供了端点契约。本阶段复刻该管线。

### 做了什么
- **视觉客户端** `multimodal/vision.py`：`describe_image`（urllib POST 到
  `https://api.minimax.io/v1/coding_plan/vlm`，body `{prompt,image_url}`，Bearer +
  `MM-API-Source`，**无新依赖**）；`preprocess_vision` 把 image block 换成
  `[图片描述：…]` 文本块，VL prompt 拼入用户本轮文本，失败回退不致命。
  payload 构造 / 响应解析拆成纯函数便于测试。
- **config**：`ModelConfig.vision: bool`；`VisionFallbackConfig`（enabled/endpoint/
  model/api_key_env/prompt/timeout）；`AppConfig.resolve_api_key_env()`。
  config.yaml：claude-*/gpt 标 `vision: true`，加 `vision_fallback` 段，**移除 minimax-vl**。
- **bridge**：`send_message` 在主模型 `vision=false` 且含图时调 `_maybe_preprocess_vision`
  （emit `vision_start`/`vision_done`，同步在工作线程跑）；`vision=true` 直发原图。
- **前端**：`vision_start`/`vision_done` 渲染"🔍 视觉预处理"折叠块（识别中→已转文字 + 描述摘要）。

### 关键决策（详见 ADR-0007）
- 按 `ModelConfig.vision` 自动切换：true 直发原图，false 走 VL 预处理。
- 视觉源复用 `MINIMAX_API_KEY`（VL 端点），用户无需额外视觉模型 key。
- 主模型看到的是文字描述（非原图），对看截图/报错够用；失败不中断对话。

### 自检（Linux 开发环境）
- `python -m py_compile` 全部通过。
- `tests/test_p4_vision.py` **9/9**：payload/parse 纯函数、业务错误码、image→描述替换、
  多图、失败回退、纯文本/无图原样、user_text 拼入 prompt。回归 test_p4 9/9、test_p3 7/7。
- bridge 冒烟（mock `describe_image`）：minimax 触发预处理、claude 保留原图，均正确。

### 验证结果（2026-06-09，Windows 真机）
- [x] 机制正确：主模型 vision=false 时成功触发预处理、emit 进度、单图失败优雅回退成
      `[图片识别失败：…]` 文本，不中断对话；文档/代码/文本附件不受影响。
- [ ] 端到端识图：用户的 MiniMax 开放平台 API key **无法访问** `coding_plan/vlm`：
      - Global 域 `api.minimax.io/...vlm` → "密钥无效"（CN key 打 Global host）。
      - CN 域 `api.minimaxi.com/...vlm` → 连接失败（CN 区无此端点）。
      判定：vlm 是 MiniMax **编程套餐**专属端点，普通开放平台 key 无权访问。

### 结论与处置
- OpenClaw/有道能用 M2.7 识图，靠的是编程套餐凭证体系（用户那边填的 key 属编程套餐），
  非普通开放平台 key。我们的实现正确且端点/key 全可配，缺的是可访问 vlm 的凭证。
- **默认关闭 `vision_fallback`**，避免每次发图触发失败刷屏。图像识别**搁置**。
- 重新启用路径：拿到可访问 vlm 的凭证（编程套餐 key）→ `enabled: true` + 对应 key；
  或把 endpoint/key 指向其它视觉服务；或主模型直接用 `vision:true` 的 Claude/gpt-4o。

### 风险/遗留
- 图像识别端到端待合适凭证补做（实现已就绪，开关默认关）。
- 描述保真度取决于 VL-01；信息损失是预处理方案固有代价。

---

## 2026-06-09 — P4 多模态输入（图片 + 文档）

**阶段**：P4
**状态**：✅ 已交付（v0.3.0）。文档附件（PDF/代码/txt）Windows 真机验证通过；
图像链路经诊断确认正确，**视觉识别待在支持视觉的模型上补验**（MiniMax 当前接口不支持图像）。

### 做了什么
- **多模态归一**（`multimodal/ingest.py`）：`build_user_content(text, attachments)`
  把附件转成统一 content blocks——图片→`image` block（mime 校验 + 大小限制），
  PDF→pypdf 抽文本→`<document>` 文本块（扫描件给提示），文本/代码→文档文本块，
  其它二进制→拒绝提示；无附件回退纯文本 str（P1 路径不变）。
- **config**：`MultimodalConfig`（max_image_mb / max_doc_chars / max_attachments），
  `config.yaml` 加 `multimodal` 段；`pyproject` 加 `pypdf>=4.0`。
- **provider 适配**：Anthropic 原生支持 image block 直传；`openai_p._messages_to_openai`
  把 image block 转 `image_url`（data URL），user 消息退化/数组两种形态。
- **bridge**：`send_message(text, attachments)` 经 `build_user_content` 构造首条 user 消息。
- **前端**：📎 按钮 + 隐藏 file input + 附件预览条；三种入口（粘贴 / 拖拽整窗 / 选文件）；
  FileReader 读 base64，预览缩略图 + 删除；用户消息上方渲染已发附件；新会话清空。

### 关键决策（详见 ADR-0006）
- 内部复用 Anthropic 风格 content blocks，不引入新格式。
- PDF 用 pypdf 抽文本（轻量省 token），不渲染成图片（已与用户确认）。
- 附件入口＝粘贴 + 拖拽 + 选文件三件套（已与用户确认）。

### 自检（Linux 开发环境）
- `python -m py_compile` 全部通过。
- `tests/test_p4.py` **9/9 通过**：无附件回退 str、图片块生成 + mime 校验、超大图片拒绝、
  文本文档包裹、二进制拒绝、PDF 解析路径、附件数上限、openai image→image_url 转换、
  纯文本保持字符串。`tests/test_p3.py` 回归 7/7。
- bridge `Api.send_message(text, attachments)` 构造 blocks 冒烟通过。

### 验证结果（2026-06-09，Windows 真机）
- [x] 文档附件：PDF（pypdf 抽文本）/ 代码 / txt 在 M2.7 上被正确读取并回答。
- [x] 附件 UI：粘贴 / 拖拽 / 选文件、预览条、删除、新会话清空均正常。
- [x] 图像数据链路：诊断输出确认 image block 正确构造并以 `image_url` 发出
      （`provider=openai`、base64≈119KB、`内容块 {'image':1,'text':1}`）。
- [ ] 图像端到端识别：MiniMax M2.7（无视觉）与 M2.5（经其端点）均无法识别图像；
      用户暂无视觉模型 key，**待用 Claude / gpt-4o 补做图像识别验证**。

### 验证中发现 & 排查（2026-06-09）
- **MiniMax-M2.7 不支持图像**：官方/NVIDIA 模型卡确认 M2.7 是文本/代码模型，
  仅接受文本（GitHub MiniMax-M2 #92 同问题）。文本/PDF/代码附件可用，图像不可用。
- **M2.5 经 `/anthropic` 端点看不到图**：用户用附件方式发图，M2.5 仍无法识别图像内容。
  怀疑 MiniMax 的 Anthropic 兼容端点不消费 image content block。
- **应对**：
  1. 新增 `minimax-vl` 档案，改走 **OpenAI 兼容端点** `https://api.minimaxi.com/v1`
     + `model: MiniMax-M2.5`（`openai_p` 已发标准 `image_url` data URL）。
  2. bridge `send_message` 加诊断输出（`_debug_attachments`）：在运行终端打印
     本次模型/provider/附件数/各附件 base64 长度/内容块构成，确认图像数据是否到达后端。
  3. 结果：`minimax-vl`（M2.5 经 OpenAI 兼容端点）发图，诊断确认图像数据正确送达，
     但模型仍无法识别 → 判定 **MiniMax 系列在当前接口下图像识别不可用**，实现侧无 bug。

### 结论
- **多模态实现正确**：图像/文档全链路（前端→bridge→multimodal→provider）经诊断与
  真机文档验证确认无误。图像能否被「看懂」取决于所选模型的视觉能力。
- **视觉模型建议**：需识图请用 `claude-sonnet`/`claude-opus`（配 ANTHROPIC_API_KEY）
  或 `gpt`(gpt-4o，配 OPENAI_API_KEY)；MiniMax 走文本/工具/文档场景。

### 遗留 / 风险点
- 图像端到端识别待在视觉模型上补验（实现已就绪，仅缺可用的视觉模型 key）。
- `bridge._debug_attachments` 为开发期诊断输出（打到运行终端），后续如不需要可移除。
- 扫描件型 PDF 抽不到文本，仅提示（如需 OCR/渲染另开 ADR）。
- 图片占上下文 token，压缩留待 P6。

---

## 2026-06-09 — P3 工具 + Agent 循环 + 权限 gate

**阶段**：P3
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-09）

### 做了什么
- **Provider 接口扩展**（`providers/`）：`StreamEvent` 加 `tool_use` 类型、`done` 带
  `stop_reason`；`stream_chat` 增 `tools` 参；`Message.content` 支持 `str | list[dict]`。
  - `anthropic_p.py`：透传 Anthropic 原生工具 schema，流结束从 `get_final_message`
    取 `tool_use` block。
  - `openai_p.py`：把内部 content blocks 与工具 schema 转成 function-calling，
    累积流式 `tool_calls` 分片，归一回 `tool_use` 语义。
- **工具系统**（`tools/`）：`Tool` 抽象 + 工作区路径沙箱；`fs.py`（read/write/edit/
  list）、`shell.py`（PowerShell，平台隔离）、`search.py`（grep/glob）、`registry.py`。
- **Agent 循环**（`agent/loop.py`）：plan→act→observe，`max_steps` 防死循环。
- **权限 gate**（`agent/gate.py`）：逐次确认 + 本会话「全部允许」，threading.Event 协调。
- **bridge**（`bridge/api.py`）：`send_message` 改走 AgentLoop；新增 `resolve_permission`；
  `new_session` 复位 gate。`config.py` 加 `AgentConfig`，`config.yaml` 加 `agent` 段。
- **前端**（`web/`）：工具调用折叠块（运行中/完成/失败）、tool_result 展示、
  权限确认条（允许/拒绝/本会话全部允许）。

### 关键决策（详见 ADR-0005）
- 工具 schema 内部统一用 Anthropic 原生格式，OpenAI 侧转换；调用方零感知。
- 危险操作权限交互＝逐次确认 + 会话级全允许（已与用户确认）。
- shell 默认 PowerShell（OQ-2 已确认）；工作区沙箱，越界路径拒绝。

### 自检（Linux 开发环境）
- `python -m py_compile` 全部 .py 通过。
- `tests/test_p3.py` 纯逻辑单测 **7/7 通过**：路径越界拒绝、write/read/edit 串替换、
  registry schema、gate 全允许短路 / 拒绝、agent 循环 tool_use↔tool_result 往返、
  危险工具被拒不执行。
- bridge `Api()` 构造冒烟通过，registry 默认 `run_powershell`。
- GUI / 真实模型 tool-use 无法在 Linux 跑，留待 Windows 验证。

### 验证结果（2026-06-09，Windows 真机）
- [x] 列目录 / 读文件工具往返正常。
- [x] 新建文件触发权限确认条，允许后文件落地。
- [x] run_powershell 确认后执行、结果回灌。
- [x] 「本会话全部允许」后不再弹窗；新会话恢复弹窗。
- [x] MiniMax-M2.7 的 tool-use 可用（Anthropic 兼容接口支持 tools）。

### 遗留 / 备注
- 本机为跑单测装了 `anthropic` / `openai` SDK（`pip --break-system-packages`），仅影响开发环境。

---

## 2026-06-08 — P0 脚手架 + P1 单模型流式对话

**阶段**：P0、P1
**状态**：✅ 已交付，✅ 已在 Windows 真机验证通过（2026-06-08）

### 做了什么
- 建立项目骨架：`pyproject.toml`、`config.yaml`、`.env.example`、`.gitignore`、`README.md`。
- 配置系统 `config.py`：加载 `config.yaml`（模型档案）+ `.env`（密钥），pydantic 校验。
- 模型适配层 `providers/`：
  - `base.py` 统一接口 `BaseProvider.stream_chat() -> Iterator[StreamEvent]`。
  - `anthropic_p.py`、`openai_p.py` 两个实现。
  - `__init__.py` 工厂 `build_provider()`。
- JS↔Python 桥 `bridge/api.py`：`send_message` 同步消费流，经 `evaluate_js` 推回前端。
- 入口 `app.py`：加载配置 → 起 pywebview 窗口 → 注入 Api。
- 前端 `web/`：暗色 UI，markdown + 代码高亮流式渲染，模型下拉切换，新会话。

### 关键决策
- 桌面外壳用 pywebview（轻量、纯 Python）。详见 ADR-0002。
- 流式走「Python 同步消费 + evaluate_js 推事件」方案，便于 P3 扩展 tool_use 事件类型。

### 自检
- `python3 -m py_compile` 全部 Python 文件语法通过。
- GUI 无法在本开发环境（Linux 无显示）运行，需 Windows 验证。

### 验证结果（2026-06-08，Windows 真机）
- [x] pywebview 起窗口正常、WebView2 就绪。
- [x] MiniMax（Anthropic 兼容接口）流式对话跑通；模型名 `MiniMax-M2.7` 可用。
- [x] markdown / 代码渲染、模型切换、新会话可用。

### 验证过程中踩到的坑（供后续 & 文档参考）
- 解压后**多嵌套了一层目录**（`hermes-dev/hermes-dev/`），需进里层执行 `pip install -e .`。
- **`hermes-dev` 命令未注册到 PATH**（user 安装），改用 `python -m agentcore.app` 启动更稳。
- **`.env` 文件未成功创建**（记事本易加 `.txt` 后缀 / BOM）。最终用
  `"KEY=val" | Set-Content -Path .env -Encoding ascii` 直接生成干净文件解决。
- 已知改进点：README 的 Windows 启动说明应补充 `python -m agentcore.app` 备选与 `.env` 创建的稳妥方式。

### 模型配置（本次实际使用）
- Provider：anthropic（自定义 base_url）
- base_url：`https://api.minimaxi.com/anthropic`
- model：`MiniMax-M2.7`
- 密钥环境变量：`MINIMAX_API_KEY`

### 下一步（待用户确认）
- 进入 **P3（工具 + Agent 循环）**；P2 的 UI 设置面板倾向延后到 P6。
- 见 PRD「待确认问题」OQ-1（设置面板时机）/ OQ-2（shell 默认 PowerShell/cmd）。
