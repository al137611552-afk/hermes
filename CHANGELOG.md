# Changelog

本项目所有显著变更记录于此。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

后续候补：**研究墙·墙钟时间上限**；**目标满足驱动的换源**（把换源触发从"零新域名"补成"目标数据点连续缺席"，价格/数字类先做）；**Learning 运行时接线**（让 active 策略真正影响选路，须再过 Golden）；**UX Tier2 续**（①余：子 agent 角色 的可视化管理，低 ROI 暂缓；②会话「运行中」状态+并发；③diff 行内定向反馈）；**P5 第三波**（G debugger 子角色 / I 回归二分定位，按需）；**自动更新**（分发三件套最后一件，ROI 低、按需）；**macOS GUI 真机验证**（代码已跨平台、Windows 侧已验，待有 Mac 后验 WKWebView 窗口）。

## [3.49.0] - 2026-06-30

**研究检索·治本 option B：web_search 宽召回 + 确定性重排/去重/控源多样**。治根因——`web_search` 直吞 Bing 前几条、不重排不去重，搜「苹果 水果」一堆 Apple 公司页。本地、无 key、无模型、无延迟、确定性。

### Added / Changed
- **宽召回**：Bing 查询加 `count=30`，候选池 ~10→~30，多抓再筛而非直吞前几条。
- **确定性重排** `rerank_results`（`tools/web.py`，纯函数）：打分=查询词**覆盖度**（标题权重更高），多词全覆盖的内容页排到只覆盖单词的前面；**单域封顶 2** 控源多样（利好 Novelty）；去重相同 URL；域名 cap 为软偏好（候选不足放宽填满 top_n）。重排分只在工具内排序、不反喂决策（守 ADR 0014 禁 score）。
- **CJK 2-gram 分词**：中文用户把整句用空格分成短语（「怎么挑选甜苹果 颜色 手感」），整短语 substring 匹配不到→全 0 分→退化吐百科。改对 CJK 段切 2-gram + 中文疑问/泛化停用词表，让"苹果/挑选/颜色/手感"可匹配、避免「怎么」百科页虚高。
- 输出格式（`[搜索结果·eng]` + 编号条目）不变 → H1 评估器/Golden 解析零影响。无新依赖。`tests/test_web.py` +5（共 12）。

## [3.48.0] - 2026-06-30

**评估/策略内核 块 H — Research Evaluator（搜索/调研结果质量评估 + 自判重搜）**（ADR 0018）：把"结果返回了但不达标/不对题"这类**质量差距**纳入决策闭环——Hermes 能自判搜索好坏、按需重搜/换源/萃取/停搜诚实兜底。源起真实反馈（小红书"618 睡衣 500 元以内"搜出超预算结果判不出、不会重搜；"2026 最新显卡价格"无限重搜 1500s 交白卷）。**已 Windows 真机验证通过**（diag_blockH 22/22；显卡价格场景实测：重搜达预算后强制停搜、模型诚实综合作答不编造价格）。

### Added
- **块 H1 事实层**（`evaluators/research.py`）：接管 `web_search`（注册早于 Search），抽**预算约束满足度**（query 解析上限 + 结果解析标价 → `within_budget`）。blocker issue 只在可证伪时触发（有上限/有命中/有标价却无一在内），模糊项只当 signal。
- **块 H2 决策层**（`loop.py detect_low_quality_research`）：web_search 出 blocker issue → 注入"返回了但不达标，换词/换源重搜"事实促重搜。per-query 计数封顶防同词无限重搜。**喂事实而非硬拦截**。
- **块 H3a 模型裁判·文字层**（`agent/judge.py` + `detect_offtarget_research`）：provider 注入式裁判（`judge_fn(prompt,images)`，多模态就绪），判语义相关性（"夏季"≠厚秋冬款）。裁判故障/解析失败一律放行不拦。
- **块 H3b 模型裁判·多模态看图**（`detect_offtarget_answer` + 终局钩子）：对带图最终答案连配图一起判（抓"配图一看就是冬季"），不符→据图重选/重搜（`answer_refined` 每轮封顶一次）。`conversation.py` 把 image 块合进多模态 user 消息真正喂像素。
- **块 H3c 萃取（三态）+ 接地/时效闸**：裁判升三态（Verdict 加 `use`/`salvageable`）——部分污染→挑出有效项采用并标注来源、**别整批丢、别凭训练记忆硬编**（杀掉旧"请不要采用这些结果"诱因措辞）；基本是垃圾才重搜。`detect_ungrounded_answer`（纯正则零成本）：时效敏感+搜过+无引用无声明→催据来源作答或明确声明可能过时。保守触发不误杀稳定知识。
- **全局重搜预算 + 止血出口**（`research_max_rounds`，默认 3）：整轮催重搜累计达上限→翻面发一次性"停搜、用现有最相关内容综合作答+声明局限"出口，根治"换关键词绕过 per-query cap→无限重搜交白卷"。
- **Novelty/Progress + 换源策略阶梯**：`extract_domains` 抽搜索结果域名（确定性去重/归一，非模糊分）作 Novelty；Progress 两态——有新域名(NEW_INFORMATION)→换词重搜、零新域名(NO_PROGRESS)→`switch_strategy_nudge` 按阶梯换检索方式 `site:`官方/github→浏览器直通→ask_user。守 ADR 0014「Evaluation 禁 score」。
- 配套：ADR 0018；config `research_refine`/`research_judge`/`research_refine_max`/`research_max_rounds`（构造器默认全关 → 存量行为零变化）；Golden 门扩到 42 条（+14：research_judge 三态 / grounding 闸 / 换源阶梯 / Novelty）；Windows 自测 `scripts/diag_blockH.py`（22 项）。

## [3.47.0] - 2026-06-30

**评估/策略内核 块 E–G**（ADR 0016/0017）：在块 A–D 的稳定契约之上补齐**记忆 + 安全网 + 学习**——失败被跨会话记住、决策内核有 Golden 回归门、历史失败可半自动凝成带证据的候选策略。**块 E 已 Windows 真机验证通过**（SQLite 死路记忆跨会话落盘 + 真实 `detect_repeated_failure` 端到端 + 瞬时 IO 不误判，自测 11/11）；**块 G 中文建议 JSON round-trip 亦经 Windows 验**（GBK 崩坑回归，自测 16/16）。

### Added
- **块 E World State + Failure Memory**（跨步/跨会话死路记忆，ADR 0016）：`agent/world_state.py`——`WorldState`（单会话纯内存：Need 历史 / 按**指纹**聚合的失败计数 / 已证伪路径 / 未决阻塞）+ `FailureMemory`（跨会话 SQLite `data/failures.db`，key=`(指纹,错误分类,Decision)`，**一次失败=一行增量**只记主分类，`known_deadend()` 查已知死路）+ `fingerprint(工具,关键入参)`（归一化 + sha1 截 16 位）。`loop.py detect_repeated_failure`：每个**非瞬时**失败记入两库，本会话累计 ≥ 阈值**或**跨会话已知死路 → 注入"此路已 N 次不通，换思路"事实（每指纹每轮一次，瞬时 IO 不计 → 归块 D 重试）。**喂事实而非硬拦截**。config `failure_memory`（默认 true）/`deadend_threshold: 2`；构造器默认 `failure_memory=None` → 存量行为零变化。
- **块 F Golden Dataset + 回归门**（Learning 安全网）：`tests/golden/`（cases + runner）冻结决策内核行为基线（块 A verdict→Need / B evaluate 事实 / C classify 主分类 / D retry 决策 / E deadend 第几次提示 / G 候选生成边界，共 26 条），`tests/test_golden.py` 并入"全回归"并自带**门活性自检**（注入错误期望必报红，防门形同虚设）。**任何改决策逻辑须先过此门**。
- **块 G Learning Engine**（半自动改进 Need→Decision，ADR 0017）：`agent/learning/`——`aggregate(FailureMemory)` 把失败行按错误分类归并成 `Aggregate`（总次数/涉及几条路/失败时 Decision/样例）；`propose()` 只对**系统性**失败（同分类跨 ≥2 路累计 ≥3 次）升级为带**语料证据**的候选（`transient_io` 永不成策略）；`StrategyStore`（JSON 治理）生命周期 `proposed →(人审 approve + Golden 通过)→ active → retire/rollback`，**`approve()` 强制 `golden_passed=True`**（"没过语料门不准上"写进代码）、状态变迁留审计。**不自动改运行时**——决策层仍是确定性硬规则 + 模型，G 只产建议；`active()` 留作将来运行时只读消费接口，本版暂不接线 loop（零控制流改动、零回归）。`FailureMemory.rows()` 导出供聚合。
- 配套：ADR 0016 + 0017；Windows 自测脚本 `scripts/diag_blockE.py`（11 项）/`diag_blockG.py`（16 项）。

## [3.46.0] - 2026-06-30

**评估/策略内核 块 A–D**（ADR 0014）：把"判断"抽成稳定契约 `Tool→Evaluation(事实)→Policy→Need(差距)→Decision(做法)`，落地第一条确定性 `Need→Decision` 硬规则（瞬时 IO 自动重试）。**已 Windows 真机验证通过**（含真实 PowerShell 子进程端到端重试）。

### Added
- **块 A 契约骨架**（行为等价重构）：`agent/contract.py` 定义 `Need` 枚举（9 个差距：CONTINUE / NEED_INFORMATION / NEED_EXECUTION / NEED_VALIDATION / PROGRESS_STALLED / APPROACH_INVALIDATED / NEED_USER_INPUT / GOAL_BLOCKED / GOAL_SATISFIED）+ `Evaluation` dataclass（metrics/signals/issues/confidence，**不存 score**）。crazy verdict 经 `verdict_to_need()` 映射到 Need 并随轮上报 `crazy_need` 事件；`loop.py` 三个 nudge（login_wall/browse/stuck_edit）重构为"探测事实→归 Need→`_nudge_injection` 选注入"，**注入文案与分支逻辑逐字不变**。
- **块 B Evaluator 标准化**（事实层）：`agent/evaluators/`（base 调度器 + Coding/Search/Shell 三个 Evaluator，优先级 Coding>Search>Shell），把散落的退出码/字符串归一成结构化 `Evaluation`。`score()` 仅 UI 投影、决策不读。`loop.py _emit_result` 附 `eval`（纯观测）；前端 `formatEval` + `.tr-eval` 摘要条（绿 ok / 橙 warn）。
- **块 C Error Taxonomy**（可聚合分类）：`agent/taxonomy.py` 定义 `ErrorClass` 9 类（TRANSIENT_IO/AUTH/NOT_FOUND/SYNTAX/LOGIC/RESOURCE/AMBIGUOUS/EXTERNAL_BLOCKED/UNKNOWN，与 Need 正交），规则先行 + 优先级 + 失败门控 + UNKNOWN 兜底。前端摘要条缀分类标签（如 `[transient_io]`）。ADR 0015。
- **块 D Auto-Retry**（第一条 Need→Decision 硬规则）：`agent/policy.py decide_retry()` **仅对 `TRANSIENT_IO`** 触发指数退避重试（工具调用级，判据是分类不是 ok 标志）。`_exec_tool_with_retry` 包住串行+并行两路；撞上限返回最后失败交上层（不伪造 Need）；`tool_retry` 事件可观测。config 三项 `auto_retry`（默认开）/`retry_max_attempts: 2`/`retry_backoff_base: 0.5`。
- 配套文档：ADR 0014（架构契约）+ ADR 0015（错误分类）+ `docs/ROADMAP.md`（A–G 分块路线图）。

## [3.45.0] - 2026-06-27

**UX Tier2 统一管理面（MCP / Hooks）+ 一批真机 bug 修复与配置傻瓜化**。**已 Windows 真机验证通过**（含后端 headless 真连官方 MCP server）。

### Added
- **统一管理面 · MCP server**（UX Tier2-①，对标 Cursor Customize 页）：设置面板加 **🔌 MCP 扩展**——GUI 里增删改 / 启停外部 MCP server，**不必手编 config.yaml**，改动即时重连、显示每个 server 连上的工具数。**一键模板**（📁文件系统 / 🔧Git / 🌐网页抓取，自动填好结构、目录默认当前工作区）+ **📁 选择文件夹**按钮（弹原生对话框选目录，免手填路径）。运行时覆盖 `user_mcp.json`，Windows 下 npx 类命令自动包 `cmd /c`。后端 `config.*_user_mcp_server`/`merge_user_mcp` + `api.get/save/delete/toggle_mcp_server` + `api.pick_directory`。
- **统一管理面 · Hooks**（UX Tier2-①）：设置面板加 **🪝 Hooks**——GUI 里增删改 / 启停生命周期 hook（PreToolUse 退出码 2 拦截 / PostToolUse stdout 回灌），配 event / 工具名正则 matcher / 命令 / 超时，下一轮即生效。典型：写文件前扫密钥、编辑后跑 linter/SAST。运行时覆盖 `user_hooks.json`，与手编 config.yaml 共存。后端 `config.*_user_hook`/`merge_user_hooks` + `api.get/save/delete/toggle_hook`。
- **minimap 鱼眼放大**：右侧目录刻度（`#chat-index`）鼠标移动时 dock 式按距离放大附近刻度（smoothstep 过渡、近的高亮），便于点击。

### Fixed
- **run_powershell 卡死 / `UnicodeDecodeError: 'gbk'`（根因·最严重）**：`subprocess` 用 `text=True` 漏 `encoding` → Windows 中文环境默认 GBK 解码命令的 UTF-8 输出、读取线程崩/卡。系统性排查 **8 处** subprocess 全补 `encoding="utf-8", errors="replace"`（shell/procs/verify×2/hooks/trace/fixture/conversation）。**新增守卫测试 `test_encoding_guard.py`**：AST 扫全库 `subprocess(text)`/`read_text`/`write_text`/`open(文本)`，漏 encoding 即红——把这一类钉死防复发。
- **mermaid 语法错把"报错炸弹图"喷到页面顶部破坏布局**：渲染前先 `mermaid.parse(suppressErrors)` 校验、不合法保留代码块；`initialize` 加 `suppressErrorRendering` + finally 清理残留。同类加固：`renderMarkdown` 的 `marked.parse` / `hljs.highlightElement` 补 try/catch（畸形/流式半截 markdown 不再崩气泡）。
- **"agent 读取未发送草稿"**：查实前端无自动读输入框路径（`send()` 仅显式 Enter 触发），系①的连带（炸弹图冻 GUI 后误触 Enter 走 steering），①修好即消。
- **交互式命令卡死**：`npm create vite` 等交互命令在前台 `subprocess` 干等输入到超时 → 加 `stdin=DEVNULL`（拿 EOF 快速失败）+ 超时提示教用 background / `--yes`。
- **MCP 连接诊断**：失败原来只显示 ExceptionGroup「unhandled errors in a TaskGroup」→ `_flatten_exc` 拆出真异常 + `errlog` 捕获 server stderr（按字节读 + UTF-8/GBK 智能解码 `_decode_best`，**又一记 GBK 同根**）→ GUI 直接显示「server 说：目录不存在 / 参数错」等人话原因。连接超时 20→60s 容纳首次 npx/uvx 下载 server 包 + 超时提示稍等重试。
- **pydantic 启动警告**：`ContextConfig.model_summary` 撞 `model_` 保留前缀 → 加 `protected_namespaces=()` 消警告（功能不变）。

## [3.44.0] - 2026-06-25

**易用性 UX 升级（Tier1）**：调研主流 agent 2026 上半年 UX 迭代后对标补齐——智能确认分级 + 实时预览面板。**已 Windows 真机验证通过**。

### Added
- **智能确认分级**（UX Tier1-①，对标 Claude Code *Auto mode* / Cursor *Auto-review*；治确认疲劳）：**「明显安全」的只读·检视·测试 shell 命令自动放行、不再逐次弹确认**——含 Unix（`ls`/`cat`/`grep`/`find`/`pytest`…）与 **Windows/PowerShell**（`dir`/`Get-ChildItem`/`gci`/`gc`/`findstr`/`where`…，大小写不敏感）、`git status·diff·log`、`npm test`/`pip list`/`mypy`/`ruff` 等。**safe-by-default**：写文件、编辑、commit、`pip install`/`npm install`、命令替换 `$(...)`/`@(...)`、**PowerShell 脚本块 `{…}`**（可藏 `rm`）、写重定向 `>`、`sudo`、拿不准的命令**一律仍走确认**；毁灭性命令（`rm -rf`/`git push --force` 等）**永远拦**。整条命令含 `&& || ; |` 串接的**每一段都安全**才放行。🛠 功能开关面板可一键关（默认开）。纯逻辑分类器 `permissions.command_is_safe`/`is_safe_autorun` + gate 闭包现读开关（切换即时生效）。`test_permissions.py` 18/18。
- **实时预览面板**（UX Tier1-②，对标 Claude Code *Artifacts* / Cursor *Canvas*；做 web 项目体感质变）：工作区面板加 🖥 预览开关——**在面板里 iframe 实时渲染运行中的 dev server**，不再只看源码。**自动对准**当前会话后台 dev server 的本地 URL（先扫进程输出，回退**从命令行抽端口** `http.server 8000`/`--port`/django `runserver` 等——dev server 的 URL 行常卡在 stdout 缓冲），多 server 用 `<select>` 列全部、可手填，带 ↻ 刷新 + ⤴ **在系统浏览器打开**（兜底禁止内嵌的站点如 Django 默认 `X-Frame-Options:DENY`）。点文件/切换会话自动退出预览态。后端 `procs.extract_localhost_url`/`url_from_command`/`preview_targets` + `api.get_preview_urls`，`test_procs.py` 13/13。
- **文件预览刷新按钮**：工作区文件预览 header 加 ↻（原地重读当前文件），与实时预览一致——改了文件不必回树里重新点。

## [3.43.0] - 2026-06-25

**浏览器穿透质量收口**：登录态模式 + 把穿透搜索掰成 browser-native（用户真机逐轮揪定）。**已真机验证通过**。

### Added
- **浏览器穿透「有头·登录态」模式**（反爬正解）：🌐 浏览器穿透设置加一个开关——开启后弹出**可见浏览器**，碰到要登录的网站 / 滑块验证，你手动登录·划一次，登录态**持久保留复用**，之后 hermes 以你的已登录身份类人查询（绕过反爬，不跟滑块硬刚）。底层即去掉 Playwright MCP 的 `--headless`（其持久 profile 默认保留登录）。

### Fixed
- **穿透搜索绕路 → browser-native**：浏览器穿透开着时，**主 agent 与委派子 researcher 一律去掉 `web_fetch`+`web_search`**——断掉「浏览器一时读不动就跳回外部搜索绕路瞎逛」的退路，逼其 `browser_navigate` → 点进结果 → 读内容页。配套两条 directive 纠误判：① **snapshot 看着空/像骨架 ≠ 页面没加载**（知乎等正文是 JS 懒加载/在下方：重 navigate 等一下、scroll、或直接点进结果再读，别判「要登录/被反爬」就放弃）；② **拿到结果列表就用条目 ref 点进前几条读正文，别换另一个搜索引擎重搜**（再开百度/必应/谷歌重搜＝换汤不换药的绕路）。**三轮用户真机（登录知乎+穿透）验证**：工具走对 → 不再跳 web_search → 开始点进结果读正文。

### Changed
- **会话行按钮顺序**：导出 ⬇ 移到改名 ✎ 之后（现为 📌 置顶 → ✎ 改名 → ⬇ 导出 → 🗑 删除）。

## [3.42.0] - 2026-06-24

### Fixed
- **crazy/自主模式串味**：改造成 **Ralph 式 fresh context**——每轮只喂【目标 + 已改动文件 + 下一步】、**不背累积对话历史**，跨轮记忆全靠 `update_tasks`/`update_notes`（对标 Codex `/goal` / Ralph 循环）。修掉「开 crazy 前若有对话、第 2 轮起把旧对话和目标混在一起跑偏」的问题；附带每轮更省 token、跨轮不漂移。**Windows 真机 + 真 kimi 双验证**：注入无关旧对话后跑目标不再被带偏。

### Added
- **跨平台：macOS / Linux 支持**（一份跨平台代码，非分叉）：`shell` 默认 `auto` 按系统解析（Windows→powershell，macOS/Linux→bash），加 `pyobjc-*; sys_platform=='darwin'` 依赖（mac 用系统 WKWebView），运行时注入真实 OS + shell 工具名，README 加 macOS/Linux 运行段。**Windows 行为零改变、已验**；**macOS GUI 待真机验**（代码完成，无 Mac 无法预验窗口）。
（H 轻量诊断已搁置：抓静态问题，与「长任务反复改不好」的运行时定位痛点基本不沾边。）
接 Claude / 其它模型现已可在设置面板 Provider 里填 key 启用，不再需要单列候补。

## [3.41.0] - 2026-06-24

**深度审计后的能力深化 + 情境自启**（借鉴 Claude Code / Windsurf 2026；产品哲学：少让用户操作、由 hermes 判断何时触发专用能力）。**已 Windows 真机验证通过**。

### Added
- **委派评分回炉**（借 Claude Code Performance Outcomes）：委派给子 Agent 的子任务完成后，由 lead 模型按验收标准（新 `acceptance` 入参）评分，不达标带反馈打回、复用子循环上下文重做，最多 `delegate_max_revisions` 轮（默认 0=关，🛠 面板可开）。grader 据子 Agent 的**执行证据**（实际工具调用）判断而非只看自述。
- **可编程 hooks**（对标 Claude Code PreToolUse/PostToolUse、Windsurf Cascade Hooks）：config `agent.hooks` 配「工具调用前/后跑你的命令」——Pre 退出码 2 拦截/1 警告/0 放行，Post 的 stdout 追加回灌模型；stdin 收 `{event,tool,params,workspace[,result]}` JSON。用于扫密钥/护文件/改完跑 linter 等自定义守卫。主/子 Agent 都生效，无 hook 零开销。
- **大库相关性检索 `search_code`**（对标 Windsurf RAG）：按自然语言意图检索全库最相关代码块。BM25 + 代码感知分块 + 标识符切词（拆 snake/camel + **中文二元组**），零依赖/离线/按需。诚实定位为关系排序检索（非神经语义）。
- **情境自启**（hermes 自己判断时机、零用户配置）：①反复改同一文件仍失败 → 提示用 trace_run；①大项目里浏览太多文件还没用 search_code → 提示按意图检索；②**绑定工作区时探测，检测到有测试就自动开「改完跑定向测试」**（不覆盖面板显式选择、只加不减、toast 告知可覆盖）；开发中途新建测试文件也会即时识别自动开。

### Changed
- **导出对话**：从悄悄下载到「下载」文件夹，改为弹**系统「保存为」对话框**让用户选位置 + toast 显示完整路径。
- 🛠 功能开关面板：「改完跑定向测试」被情境智能默认开启时显示「🤖（已按本项目自动开启）」。
- 依赖新增 `pytest>=7`：保证「改完跑定向测试」的默认运行器随处可用、不假通过。

### Fixed
- **BOM 误报**：带 UTF-8 BOM（Windows 工具常加）的合法源码不再被语法校验误判为「语法错误」（剥 BOM 再 parse；真语法错仍报）。
- **pytest 风格测试假通过**：没装 pytest 时不再把 `def test_*()` 当独立脚本跑（函数不执行→假通过），改为明确提示「需装 pytest」。
- 跑定向测试注入 PYTHONPATH（工作区根+src）解决裸 `from x import` 的 ModuleNotFoundError；不写 `__pycache__`。

### 性能
- 实测新功能对日常性能无可感知影响：固定开销 µs/几十 ms 级且多只在绑定工作区时一次；唯一实质开销＝「改完跑测试」每次编辑 ~0.3s（仅有测试项目、只跑那一个受影响测试），远小于模型往返。

## [3.40.0] - 2026-06-23

### Added
- **设置面板加「🛠 功能开关」**：把默认关的进阶能力做成**即点即生效 + 自动记住**的按钮，不必手改 config.yaml——`auto_affected_test`（改完跑定向测试）/`auto_review`（收尾审 diff）/`auto_test`（收尾跑整套测试，带命令输入框）。底层复用浏览器穿透的状态文件持久化（`feature_flags.json`，load_config 覆盖默认；只接受白名单键防注入）；`_make_verifier` 改为「现读 config」闭包让开关即时生效、不重建 registry（避免重置改动台账）。**已 Windows 真机验证通过**（开关即时生效、命令框显隐、重启保留）。

## [3.39.0] - 2026-06-23

易用性优化 **P1**（3.33.0~3.34.0）、**P2**（3.35.0）、**P3**（3.36.0）、**P4**（3.37.0~3.37.3，含 WebView2 滚动回归修复）**全部 Windows 验证通过、定版**。

后续候补：第三波调试能力 **G**（debugger 子角色）/ **H**（轻量诊断）/ **I**（回归二分定位）——按需再上。

## [3.39.0] - 2026-06-23

**P5 调试能力工程化**（第一波 A/B/C/F + 第二波 D/E）。借鉴 Claude Code 补「编辑→运行→看证据→定位→修」闭环；这些能力对任何模型都加分、降低对模型依赖。**已 Windows 真机验证通过**。

### Added
- **编辑后跑定向测试（FR-13.C）**：写/改文件后自动识别**受影响的测试**（按文件主名匹配 `test_<名>`/`<名>.test`）并直跑，失败连同 traceback 即时回灌——从「语法对不对」升到「测试过不过」。自动探测 pytest/独立脚本/`node --test`；新开关 `auto_affected_test`（默认关）+ `affected_test_runner`。
- **运行时值追踪 `trace_run`（FR-13.D）**：给一段驱动代码，在子进程用 `sys.settrace` 记录工作区内函数**每步局部变量 + 返回值 + 崩溃前轨迹**——debug 时直接看到中间数值，而非盲改。零源码改动、无需还原。
- **报错定位（FR-13.B）**：工具/命令输出含指向工作区文件的 traceback 时，自动附 `📍 file:line` + 源码上下文（崩溃行带箭头）+ 异常行；接入 `run_powershell` 与受影响测试输出。
- **失败固化 fixture `capture_fixture`（FR-13.E）**：把触发 bug 的输入固化成 `tests/test_capture_*.py` 复现测试并立刻跑一次确认「当前确实复现」；bug 变可复现、修好后自动随受影响测试转绿守回归。
- **复现优先 + 调试便签（FR-13.A/F）**：系统提示加调试准则（复现优先→`trace_run` 看证据→`capture_fixture` 固化→受影响测试转绿）；`update_notes` 明示维护「## 调试便签」（现象/假设/证据/已排除/下一步验证），跨轮不绕死路。

### Fixed
- 跑定向测试的子进程注入 `PYTHONPATH`（工作区根 + `src/`）+ `PYTHONDONTWRITEBYTECODE`：修「裸 `from x import` → ModuleNotFoundError」、不往工作区落 `__pycache__`、避免同秒重改命中旧 `.pyc`（Windows 真机暴露）。

## [3.38.0] - 2026-06-23

### Changed
- **导出按钮移到左侧会话行**：从顶栏移除「导出」按钮，改为每个会话行（标题旁，与置顶/重命名/删除并列）一个 `⬇` 导出按钮，悬停出现，点哪行导出哪个会话（非当前会话先切过去再导出，标题显式传入避开刷新竞态）。导出逻辑（从 DOM 重建 markdown、assistant 用 `dataset.raw` 原文）不变。

## [3.37.3] - 2026-06-23

### Fixed
- **滚轮跳回顶部（真正定位：嵌套列表 + 分隔线）**：用户精确隔离出是「两层嵌套列表 + `hr`」触发（表格/引用均正常）。鉴于引用块 `blockquote` 同样带 `margin` 却正常，普通 margin 非因；元凶是失败用例独有的两项——① `hr` 用 `border:none; border-top` 重构了盒子、② 嵌套列表的 `margin`（嵌套 margin 合并），二者在 WebView2 重排时把 chat 的 `scrollHeight` 算塌（→ scrollTop 钳回顶部、几轮后重排自愈）。修：`hr` 仅改边框色不重构盒子、列表只用 `padding-left` 缩进不加 `margin`（blockquote/表格/任务列表样式保留）。

## [3.37.2] - 2026-06-23

### Fixed
- **滚轮跳回顶部（续修，3.37.1 未根治）**：含表格的 markdown 长回答出现后，任意方向滚轮都跳回第一轮、几轮后自愈。根因＝markdown 表格此前用 `display:block`（为横向滚动）扰乱表格布局参与，在 WebView2 上触发异常重排、瞬间把 chat 的 `scrollHeight` 算塌（可滚范围≈0 → scrollTop 被钳回顶部），重排后才恢复（=自愈）。改为 **`renderMarkdown` 用外层 `.table-wrap` div 承载横向滚动、table 保持默认 `display:table`**，不再触发塌陷。（`overflow-anchor:none`、去 `:has()` 保留。）

## [3.37.1] - 2026-06-23

### Fixed
- **滚轮翻阅卡死（3.37.0 markdown 增强引入的回归）**：含表格/列表的长回答出现后，往上滑直接跳到对话开头、中间内容无法翻阅、最新回复也看不全；**且会自愈**（多聊几轮又恢复正常）——WebView2 专属，纯文字会话正常。两处修复：① 给对话滚动区加 `overflow-anchor: none`——markdown 表格等复杂内容异步重排时，WebView2 的滚动锚定会误锁滚动位置/范围（自愈正是后续重排解锁），关掉锚定即稳定（**主因**）；② 任务列表去项目符号的 `li:has(>input)` 选择器改为 `renderMarkdown` 打 `.task-list-item` class，CSS 不再用 `:has()`（避开 WebView2 选择器行为差异）。（注：曾试加表格 `overflow-y:hidden`，因会裁掉表格底边框 1px、影响观感且非主因，已回退。）

## [3.37.0] - 2026-06-23

易用性优化 **P4**（待 Windows 真机验证）。

### Added
- **Markdown 渲染增强**：为对话气泡内的表格（描边/斑马纹/表头底色）、引用块（紫色左条）、分隔线、GFM 任务列表（去项目符号、勾选框对齐）、嵌套列表补主题样式，深浅主题一致。
- **引用回复**：assistant 回答动作行加「引用」、用户消息悬停加引用按钮，把整条内容以 `>` Markdown 引用格式填入输入框（已有内容则追加）、聚焦续写；超长引用自动截断。

## [3.36.0] - 2026-06-23

易用性优化 **P3**（待 Windows 真机验证）。

### Added
- **@ 文件引用**：输入框打 `@` 弹工作区文件树自动补全（↑↓ 选择、Enter/Tab 插入、Esc 关），选中插入 `@相对路径`，agent 用 `read_file` 读最新版（轻、不撑上下文）。补全列表按会话缓存、工作区变动即失效。
- **跨会话全局搜索**：侧栏搜索框输入 ≥2 字时，除按标题过滤会话外，再搜**所有会话的消息内容**，结果列在下方（会话标题 + 角色 + 高亮片段），点一条跳到对应会话。复用 `store.search_messages`（与 recall_history 工具同源），新增 `search_messages` 桥接 API。
- **会话置顶**：会话项「📌」按钮置顶/取消，置顶组排在最前（组内仍按最近更新）；DB 新增 `pinned` 列（轻量迁移，向后兼容）。
- **切会话保留草稿**：未发送的输入按会话保留，切走再切回自动还原（本次打开以来、内存态、各会话互不串）。
- **三栏宽度可拖拽**：左会话栏 / 右工作区之间拖拽分隔条调宽，localStorage 记忆 + 上下限 clamp；工作区收起时右分隔条隐藏。

## [3.35.0] - 2026-06-23

易用性优化 **P2**（✅ Windows 真机验证通过，定版）。

### Added
- **浅色主题 + 字号调节**：设置面板新增「🎨 外观」——主题（跟随系统/深色/浅色）+ 字号（小/中/大），即时生效、localStorage 记忆、可跟随系统明暗实时切换。CSS 表面色收敛成变量，代码/工具块刻意保持暗底（配深色高亮），并修正浅色下行内代码/工具结果/权限条的对比度。
- **快捷键帮助面板**：`?` 或 `Ctrl/⌘+/` 打开、`Esc`/点遮罩关闭，分组列出现有全部快捷键。
- **超长工具输出折叠**：工具结果超过阈值（>20 行或 >2000 字符）默认折叠为预览，「展开剩余 N 行 / 收起」开关（按钮置于结果框外、始终可见）。
- **会话累计用量芯片**：顶栏显示当前会话累计 token（`Σ … tok`）+ 按公开列表价粗估的成本（`≈ $…`，未知模型只显 token），悬停看明细；切会话刷新。与每轮用量脚注（FR-11.8）互补。

### Fixed
- **浏览器穿透安装失败（GBK 解码崩溃）**：后台装浏览器的子进程读取改为显式 `encoding="utf-8", errors="replace"`，修复 Windows 中文环境下 `'gbk' codec can't decode byte 0x97 …` 启用失败；同时按 `\r`/`\n` 切行，下载进度可逐条显示。

## [3.34.0] - 2026-06-23

### Added
- **重新生成回答**：assistant 气泡「重新生成」按钮，丢弃该回答及其后对话、在原用户消息上重跑（覆盖式，非最后一条会二次确认）。
- **编辑并重发**：用户消息悬停「✎」就地改文本，保存即丢弃该消息之后的对话、用新内容重发重跑（Ctrl+Enter 保存 / Esc 取消；v1 为纯文本编辑，原附件不保留）。

### Changed
- 后端按「用户轮次序号」定位：`Conversation.regenerate(turn)` / `edit_and_resend(turn,text)` 截断内存历史 + DB（新增 `Store.truncate_messages_after`），经 worker 在截断后的历史上重跑（`_run_turn` 抽出复用），运行中拒绝。真 kimi 端到端验证内存/DB 一致。

## [3.33.0] - 2026-06-22

### Added
- **导出当前对话为 Markdown**（topbar ⬇ 按钮）：用户消息 + AI 回答（**markdown 原文**）重建成 .md 下载；通知/工具噪音不导出，附件标注「📎 N 张图片/文档名」。
- **会话内查找（Ctrl+F）**：右上角查找栏，实时高亮所有匹配、当前项紫色，Enter/▼ 下一个、Shift+Enter/▲ 上一个、Esc 关闭并清除高亮，计数「当前/总数」。

## [3.32.0] - 2026-06-22

### Added
- **图片点击预览 + 下载到本地**：对话里发出/工具产出的图片可点开放大（灯箱），右上角「下载」按钮存到本地，Esc/点空白关闭（对标主流 agent）。
- **回答一键复制**：assistant 气泡悬停出现「复制」，复制整条回答的 **markdown 原文**（非渲染后文本）。
- **代码块复制**：每个代码块右上角悬停出「复制」，只复制代码本体。
- 剪贴板走 `navigator.clipboard`，WebView2 偶发拒绝时降级 `execCommand("copy")`。

## [3.31.3] - 2026-06-22

### Changed
- **工作区展开按钮统一到右上角**：原来折叠按钮在面板右上角、展开按钮却浮在右下角（输入框上方），两个开关不在一处、不顺手。现展开按钮也移到右上角（顶栏下方），折叠/展开同区域操作。

## [3.31.2] - 2026-06-22

### Changed
- **浏览器穿透安装改为后台异步 + 实时进度 + 完成通知**（原来是同步阻塞、只有一句静态「安装中…」、面板卡着）。
  现在点「启用」立即返回，安装在后台进行：① 面板里实时显示「下载中… X%」（解析 npx 下载进度）；② **设置面板
  可随意关闭**，不卡;③ 装好/失败用 toast 通知（关了面板也能看到）。后端 `_install_browser_bg` 用 Popen 流式
  读输出发 `browser_mcp_progress` / `browser_mcp_done` 事件。真跑验证：立即返回 installing → 20 条进度事件 →
  完成事件连上 23 工具。

## [3.31.1] - 2026-06-22

### Fixed
- **Windows 上启用浏览器穿透报「未检测到 npx」**（虽然 Node/npx 装好了、状态也显示已检测到）。根因：Windows
  的 `npx` 实际是 `npx.cmd`，`shutil.which` 能找到（所以状态正常），但 `subprocess.run(["npx", ...])` 直接跑
  不认 `.cmd` → FileNotFoundError。同一坑也会影响 MCP server 启动（`command: npx`）。两处都修：Windows 下
  改用 `cmd /c npx`（安装命令 + browser server 的启动命令）。Linux/Mac 不变、真跑仍连上 23 工具。

## [3.31.0] - 2026-06-22

### Added
- **浏览器穿透一键开关**（深度调研）。设置面板左侧新增「🌐 浏览器穿透」项：点「启用」→ 后端自动检测
  Node → 装浏览器（`chrome-for-testing`，首次约 150MB）→ 持久化 → **运行时重连 MCP、工具立即可用**，
  不用再手编 config.yaml。没 Node 会提示先装。关闭则移除。**端到端真跑验证**：启用→连上 23 个浏览器工具
  （browser_navigate/snapshot/click…）进对话 registry→关闭→移除。
  - 实现：`config.merge_browser_mcp`（GUI 状态存 `mcp_browser.json`、load 时合并 Playwright server，与手编
    config.yaml 的 mcp 段并存）+ `Api.get_browser_mcp_status` / `set_browser_mcp` / `_reconnect_mcp`。
  - 说明：手编 config.yaml 的 mcp.servers 仍可用（power user）；GUI 开关是更省心的等价路径。

## [3.30.6] - 2026-06-22

### Fixed
- **解注释 MCP server 块导致启动 YAML 报错**（`expected <block end>, but found '<block mapping start>'`）。
  根因：模板里 `servers: {}` 是空字典，用户解注释 `browser:` 块缩进进去就和 `{}` 冲突。双修：① 模板改成
  `servers:`（空 = 无 server，解注释 server 块直接生效、不用再删 `{}`）；② MCPConfig 加校验器，`servers:`
  写成空/null 时容错成 `{}`、不报错。验证：空 servers 正常加载、启用 browser 的配置正确解析。
  - 即时修法（已坏的 config.yaml）：把 `servers: {}` 的 `{}` 删掉、改成 `servers:` 即可启动。

## [3.30.5] - 2026-06-22

### Fixed
- **更新解压会冲掉用户 config.yaml**（导致用户开启的 MCP/browser 穿透工具、其它改动在更新后丢失，
  表现为「新版 agent 不会用 browser_navigate 了」）。根因：pack.py 把 config.yaml 直接打进包，每次解压
  新版本覆盖用户改过的配置。改为：包里只带 **config.default.yaml 模板**（不带 config.yaml），首次运行
  load_config 据此生成用户的 config.yaml；之后更新只更模板、**不动用户的 config.yaml**。端到端验证：解压
  →首次跑→生成 config.yaml、mcp 段在；更新场景下用户配置保留。
  - 提醒：浏览器穿透工具（browser_navigate/snapshot 等）是 **Playwright MCP server 的工具、非内置**，
    一直需手动开启（`mcp.enabled: true` + 解注释 browser server + 装 Node 与 chrome-for-testing），
    任何版本默认都不开。

## [3.30.4] - 2026-06-22

### Fixed
- **长任务里往上翻后滑不回最新**：深度调研等任务内容飞快增长时，用户上翻看历史后再下滑，底部增长比手动
  滚动快、粘底（atBottom < 80px）一直挂不上 → 追不上最新。加「**回到底部**」浮动按钮（不在底部时出现、
  一点直达最新并恢复粘底）。Playwright 真浏览器验证 4 项行为全过。注：**非图标/GUI 统一引入**——滚动逻辑
  是老代码、近期未动。

## [3.30.3] - 2026-06-22

### Fixed
- **严重：provider 保存丢失默认启用态**（系统自测真跑发现）。新装用户（无 providers.yaml）一旦在设置面板
  勾选 / 取消任一模型，`save_provider` 基于空文件合并、丢掉默认 `enabled:True`，写出的配置使火山方舟被禁用
  → **所有模型从顶部下拉消失**。双修：① `effective_user_providers` 改为「DEFAULT_PROVIDERS 作基底、文件按 key
  覆盖」（配了别的 provider 也不丢未配过的默认）；② `save_provider` 合并基底改用 effective（首次保存保留默认）。

### Changed
- crazy 自主模式护栏：单轮 `max_steps` 40→50、墙钟时间 30min→1h、累计 token 0(不限)→300 万止损；并把
  `crazy_max_rounds / max_seconds / max_tokens / stall_rounds` 全部**暴露到 config.yaml** 统一管理。
- 系统性自测：核心 agent loop / provider 中心 / key 配置 / 记忆 / 联网 / 委派 researcher / crazy 护栏 / 危险
  命令黑名单 全部真跑通过（Python 34 套 + 前端 12 全绿）。

## [3.30.0] - 2026-06-22

### Changed
- **设置面板重构为 Provider 中心**（参考 Cherry Studio 等主流形态，「完善建议」第 2 条收官，已 Windows 验通过）。
  原「扁平模型档案」（ark-kimi / ark-deepseek… 各重复存一遍 key/url）改为 **provider（提供方）配一次
  key/url/格式、下挂多个模型**：
  - 左侧 provider 列表（火山方舟 / Anthropic / OpenAI / DeepSeek / Kimi-Moonshot，带启用开关）+ 右侧详情
    （只填 Key、Base URL 预填可改、协议格式、可用模型勾选 / 添加、+ 自定义服务）；内置预设只填 key 即用。
  - 底层：`providers.yaml` 存用户配置，load 时**展开成扁平档案喂 build_provider**（核心一行不改）；顶部
    模型下拉变 `provider/model`（如 `volcengine-ark/kimi-k2.6`）。
  - config.yaml 内置 models 段移除、收口到 provider 预设；默认启用火山方舟（填 ARK_API_KEY 开箱即用）。
  - 后端 `expand_provider_profiles` + `get_providers` / `save_provider`；test_providers 8/8 + 真跑 kimi 验证。
- 顶部 ⚙ 设置按钮去掉常驻背景框，仅 hover 淡底高亮。

### Removed
- 旧「API Key / 模型档案」两 tab 设置 UI（被 Provider 中心取代；底层 user_models 机制仍在、自定义 provider 复用）。

## [3.30.1] - 2026-06-22

### Fixed（v3.30.0 Provider 中心的真机反馈连续修复，已 Windows 验通过）
- 修 topbar：长模型名（provider/model）把下拉撑爆、把「模型 / 委派模型」label 挤成竖排 → 加 nowrap + 下拉限宽。
- 修自定义模型删除：✕ 按钮点击被外层 label 吞成「勾选」→ stopPropagation。
- provider 详情加「测试连接」（发最小请求验 key/url，真验火山方舟 OK）与「获取模型」（OpenAI 兼容拉 /models；
  Anthropic 协议无标准端点 → 提示手动添加）。
- 二次修复（真机反馈）：topbar 下拉改 flex 随窗口自适应（不再窗口小溢出/竖排）；toast 提到 z-index 10000
  （不被设置面板遮在下层）；自定义 provider 可「删除服务」（内置不可删）；修「没勾选模型默认就能用」——
  get_providers 与 load_config 统一用 effective_user_providers（开箱默认只勾火山方舟 kimi-k2.6、勾选才进下拉）。
- 修「流式输出中滚动时凭空冒出新会话、里面是上一条消息」的诡异 bug：bug 链＝①输入框 Enter 没防输入法
  回车（中文确认候选词被当成发送）+ ②切换/新建会话时输入框没清空（残留上个会话的草稿）——叠加导致误触
  新建会话后、残留的消息被回车发进空会话。两处都修：Enter 加 `!e.isComposing`、mountView 切会话即清输入框。
- 三次修复（真机反馈）：① 获取模型支持 **Anthropic 协议**（GET /v1/models + x-api-key，之前误判为无端点；
  自定义端点如火山方舟 coding 不支持时优雅提示手动）；② provider **协议格式可单选**（Anthropic / OpenAI 兼容，
  自定义服务必备）；③ **系统统一图标尺寸规范**（16/18/20 三档＝行内小 / 标准 / 突出）：关闭 X 18→20、工作区
  图标 14/15→16，消除"尺寸即兴"。

## [3.29.0] - 2026-06-22

### Added
- **产品化②：模型档案 GUI 增删改**（「完善建议」第 2 条第二块，**待 Windows 验**）。设置面板新增「模型
  档案」区：列出全部档案并标内置 / 自定义；自定义档案可编辑 / 删除，「+ 添加自定义模型」弹表单
  （档案名 / provider / model / api_key_env / base_url / max_tokens / vision），保存后顶部模型下拉即时刷新可选用。
  - 写回策略：用户档案存独立 `user_models.yaml`，load 时与内置 config.yaml models **合并**（用户覆盖同名）——
    不碰 config.yaml 的注释、无新依赖、内置档案只读。
  - 后端：`merge_models` / `load_user_models` / `save_user_models`（config.py）+ `get_model_profiles` /
    `upsert_model_profile` / `delete_model_profile`（api.py，校验后重载合并的 models 即时生效）。
    test_model_profiles 3/3，真跑验证合并。
  - 前端表单校验纯逻辑 `validateModelProfile` 抽进 pure.js（前端测 12/12）。
- **设置面板 UI 重设计**：从单列堆叠改为**左 tab 导航（API Key / 模型档案）+ 右内容**的主流设置布局；
  key / 模型卡片化、状态用 pill（绿「已配置」/ 灰「未配置」、内置 / 自定义标签）、表单两列网格对齐、
  输入框 focus 高亮、统一按钮样式；配色沿用 hermes 深色主题 + 紫 accent，留白舒展 + 轻投影。
  用 Playwright 渲染自检视觉，交互待 Windows 验。

## [3.28.0] - 2026-06-21

### Added
- **产品化①：API Key 配置面板 + 去内置 key**（「完善建议」第 2 条第一块，已 Windows 验通过）。
  - 设置面板（topbar ⚙）：列出各模型服务需要的 key（用途模型 / 是否已配置 / 掩码预览），填入即时写回
    exe 旁 .env 并生效（无需重启）；首次启动所有 key 未配置时自动弹面板引导。
  - 后端：`collect_key_requirements` / `upsert_env_line` / `mask_key`（纯函数）+ `get_api_key_status` /
    `set_api_key`（api.py，复用 os.getenv←load_dotenv 机制）。test_config_keys 7/7。
  - 去内置 key：`python pack.py --dist` 打分发包时用 .env.example 空模板占位、不带真实 key（可安全外发）；
    自用仍 `python pack.py`（含 key）。

### Changed
- **前端补可测性**（「完善建议」第 1 条）：新增 `web/pure.js`（可脱离 DOM 的纯逻辑、UMD：浏览器全局 +
  Node 可测）+ `tests/web/` 首组前端单测（node:test 零依赖，11 用例）；app.js 多处重构成「纯决策 + 薄
  DOM 应用」（会话行状态 / 忙碌判断 / 按钮态 / 任务进度 / 会话搜索 / slash 命令）。CLAUDE.md 把
  `node --test tests/web/*.test.js` 纳入「全回归」。

## [3.27.0] - 2026-06-20

### Added
- **深度调研能力（接 Playwright MCP 浏览器 + researcher 逐层下钻）**：让 Agent 突破「只能搜 + 读一级
  页面」，像人一样在站内逐层钻取数据（聚合→明细→分布→定性）。**已 Windows 真机验通过**。
  - 接入 Playwright MCP（复用已有 MCP 客户端，近乎零核心改动）：navigate/click/snapshot/type/翻页/
    截图等 23 个浏览器工具；browser_snapshot 返回带 ref 的无障碍结构，非视觉模型也能据此下钻。
    config.yaml 加 Playwright server 配置示例（装浏览器命令、`--browser chromium`、登录平台用
    `--storage-state` 复用登录态的提示）。默认仍关，需本机有 Node。
  - researcher 角色升级为「深度调研员」：拆子问题→逐层下钻不止步一级页面→优先用浏览器站内导航（先
    snapshot 看 ref、再 click 下钻）→多源交叉印证→综合带来源；主 Agent 准则也加了深度调研段。
  - 浏览器工具放行：Role 加 `allow_browse` + 浏览类白名单（按去 server 前缀基名匹配），排除
    `evaluate`/`run_code_unsafe`/`file_upload` 等高风险；仅 researcher 放行。
  - 本机端到端真跑验证（kimi + Playwright MCP，非 mock）：列表页→点进详情页逐层下钻、答案正确。

### Fixed
- `mcp_client/manager.py`：未装 mcp SDK 时由「卡到 connect_timeout 超时」改为 start() 一次性友好跳过。

## [3.26.2] - 2026-06-20

### Fixed
- **关程序转圈不关、等 1min+（v3.26.1 自带的 GUI bug）**：v3.26.1 的「拦截关闭 + 遮罩 + 后台
  `window.destroy()`」方案里，`destroy()` 从后台线程调用在 pywebview 上不生效 → 窗口没关、遮罩一直转
  （实测等 1min+，连几句话的新会话也卡——根因不是整理慢，是窗口没关）。改成可靠简单方案：窗口正常秒关
  （移除 closing 拦截 + 遮罩），`start()` 返回后后台整理记忆 + 最多等 5s（慢/挂就放弃，靠 `extracted_upto`
  「成功才推进」保证不丢、下次切换会话补），不再依赖跨线程 GUI 操作。

## [3.26.1] - 2026-06-17

### Fixed
- **关程序卡死（v3.26.0 引入的回归）**：v3.26.0 在 `close` 里**同步**跑记忆整理（LLM、慢），阻塞退出 →
  「卡很久、要再点一次 X」。改成**优雅关闭**：关窗时后台整理 + 前端显示「整理记忆中…」遮罩 + 完成或超时
  （20s）自动关闭（app.py 的 `closing` 钩子；GUI 待真机验）。
- **整理超时/失败会丢记忆**：抽取进度 `extracted_upto` 原在抽取**前**就占位，超时被杀/失败会丢那段
  （系统以为抽过了）。改为**抽取+固化成功后才推进进度**，防并发改用 `capturing` 标志——超时/失败时进度
  不动、下次启动重试，**不丢**。

### Changed
- **任务运行中隐藏「发送」按钮**：运行中只留「停止」，输入框 Enter 仍可发（走 steering），对标主流。

## [3.26.0] - 2026-06-17

### Changed
- **停止立即响应（中断模型流式）**：原停止「回合间生效」，要等当前模型流/工具跑完。现在 loop 在模型流式
  循环内检查停止标志、置位**立即断流**（保留已输出的部分、不执行本轮残缺工具），对标主流。
  注：执行中的长命令（工具同步跑）仍需等那条结束——中断进程要 kill，未做。
- **关程序时自动整理记忆**：原记忆整理（抽取+固化）**只在「离开会话（切换/新建）」触发**，直接关程序会丢
  最后一段对话。现在 `close` 时同步 flush 一次（`capture_sync`），关程序也整理（失败不挡退出）。

## [3.25.2] - 2026-06-17

### Changed
- **记忆固化按主题聚类（记忆结构设计）**：固化时让模型先把碎片**按主题归类**（自拟主题如「会话状态/权限安全」），
  再每主题提炼一条框架原则，content 带【主题】标注。框架层从平铺升级成**按主题组织、聚类效果可观察**。
  真机验证：10 碎片 → 5 个清晰主题各一条原则。
- **UI 去掉「排队」按钮**：任务运行中发送按钮不再显「排队」、统一显「发送」（对标主流：进行中只有「停止」，
  默认可继续发，消息作为 steering 纳入当前任务）。

## [3.25.1] - 2026-06-17

### Changed
- **principle 去重/老化（防框架层膨胀）**：固化从「add 新 principle 累积」改为「**重算替换**」——新原则已融合
  旧的（prompt 让模型参考合并），故删旧 principle 再存新；principle 数稳定不膨胀、自动去重、旧的不再相关
  就被淘汰。真机验证：连续两次固化（6→12 fact），principle 稳定在 6 条（替换而非累积到 12），且融合了两批经验。

## [3.25.0] - 2026-06-17

### Added
- **recall_history 工具（类人记忆 L3：原始对话检索，细节的无损来源）**：让模型能**跨会话搜过往对话的原始记录**。
  记忆递进下钻：① 注入的框架原则(principle)/事实(fact) → ② 不够再 `recall_history` 搜原文。工具 description
  引导「递进使用、最后兜底」，避免滥用。`store.search_messages`（跨会话关键词检索，content LIKE 任一词命中）+
  `RecallHistoryTool`（只读、parallel_safe）。这样细节不再依赖有损抽取——**框架做索引、原始记录做精确召回**。

## [3.24.0] - 2026-06-17

### Added
- **记忆固化（「类人记忆」第 2 步：写入端，闭合三环）**：攒够 fact 碎片后**离线**让模型把它们归纳成
  「框架原则」(`principle` kind)，原碎片保留作细节；principle 优先常驻召回（第 1 步预留位）。配合 v3.23.0
  的分层读取，形成「**框架常驻 + 细节下钻**」的类人记忆——碎片自动炼成框架、框架优先召回、细节按需检索。
  - 新增 `build_consolidate_request`（longmem 纯逻辑）、`_maybe_consolidate`（capture 后挂载、防重复触发、
    `consolidated_facts` 记账）；`principle` kind；`MemoryConfig.auto_consolidate / consolidate_threshold`。
  - **真机验证**（真 kimi）：10 碎片 → 7 条高质量框架原则 → 召回优先返回 principle。
  - 修：`parse_memories` 要 `{"memories":[...]}`，固化 prompt 原让模型输出裸数组导致解析全丢（真跑暴露）。

## [3.23.0] - 2026-06-17

### Added
- **记忆分层召回（「类人记忆」第 1 步：读取端）**：长期记忆注入从「最近 N 条全量」改成「分层召回」——稳定的
  用户事实/偏好(user/preference)常驻 + 其余按**当前任务相关性 top-k**（轻量词重叠打分，不依赖向量/外部依赖），
  无命中回退最近 N 条。解决两个老病：① 召回不准（注入相关的而非最近的）② token 占用（只注入相关的）。
  `_recall_memories` + `_latest_user_text`，`_budget` 取当前任务消息做检索 query；为第 2 步「框架原则优先」预留扩展位。

## [3.22.2] - 2026-06-17

### Fixed
- **crazy 期间用户补充走错分支（根因，真机暴露）**：crazy 用 `run_autonomous` 直接调 `send_message`、绕过
  worker，`_running_turn` 不置位；`enqueue` 据此判定「空闲」→ 把补充**排队、另起 worker 并发跑**（而非
  steering 注入当前自主任务），导致 v3.22.1 的注入处理在真实 crazy 里**根本没生效**（单测手动塞 `_inject`
  绕过了 enqueue，绿得有误导性）。修：`enqueue` 的 busy 判断并入 `crazy_mode`——crazy 期间补充也走 steering。
  **真机验证**（headless 跑真 kimi）：crazy 做通讯录任务时中途补充「加 export」，enqueue 返回 steering、
  export 被并入实现、且没提前夭折（跑到第2轮 pytest 验证才收尾）。

## [3.22.1] - 2026-06-17

### Fixed
- **crazy 模式下用户中途补充导致提前结束**：crazy 跑时排队的补充会作为 steering 注入模型对话；模型「回复完
  用户」后常输出 `[[DONE]]`（以为对话结束），外层误判 goal_reached 收工，把原任务和补充都丢下。修：① crazy
  下把补充**明确包装**成「任务追加需求、继续干、别因此 DONE」；② 本轮有用户补充时即便 `[[DONE]]` 也不轻信，
  强制再跑一轮确认原目标+补充都完成。与 v3.22.0（撞上限不误判）同源——crazy 的「完成」只认**未被截断/未被
  打断的主动收尾**。

## [3.22.0] - 2026-06-17

### Fixed
- **crazy 撞步数上限后误判「目标达成」、不进下一轮**：crazy 某轮撞 max_steps 被截断、强制收尾产出总结后，
  外层循环仍按「正常轮」解析完成标记——收尾里若有（或被解析成）`[[DONE]]`，就误判 goal_reached 停掉，明明
  任务是被步数截断、没干完。修：`send_message` 暴露本轮 `hit_max_steps`，`run_autonomous` 对撞上限的轮
  **强制续命**（不信收尾里的 `[[DONE]]`，因为撞上限＝被截断未完成），用「上一轮被步数截断、继续推进剩余工作」
  进下一轮，直到真·主动收尾或用尽预算。

## [3.21.6] - 2026-06-16

### Added
- **运行中改标题：空闲后自动补改工作区文件夹名**：会话正在执行（或 crazy 运行中）时改标题，文件夹重命名会被
  跳过（避免占用冲突/丢文件），此前是**静默跳过、需手动空闲再改一次**。现在改成——跳过时记下 pending，会话
  一轮结束 / crazy 结束、空闲时**自动补做重命名**（conversation 发内部 `ws_settle` 事件、api 拦截补同步）。
  运行中随便改标题，结束后文件夹名自动跟上、无需再操作。

## [3.21.5] - 2026-06-16

### Fixed
- **crazy 自主模式运行中改标题导致「工作区被重置」（丢文件）**：首次会话工作区是数字名 `data/workspaces/<id>`，
  改标题会重命名该文件夹。原有「运行中（_running_turn）跳过移动」保护只覆盖**单轮**；crazy 是后台、跨多轮的
  长任务，在两轮空隙改标题时 `_running_turn` 为空 → 误判空闲 → 把正在用的工作区搬走 → 自主任务看到空目录、
  以为被重置。修：**crazy 运行期间整段锁住工作区**——改标题只改标题、不搬目录（crazy 结束后下次改再搬）。

## [3.21.4] - 2026-06-16

### Fixed
- **子 agent 无法访问授权目录**：v3.20.2 给主 agent 的 system 注入了授权目录，但**漏了子 agent**——子 agent 的
  registry 虽有 extra_dirs（能力 OK），但 `_subagent_system` 没告知授权目录，导致委派的子任务读错工作区
  （报「工作区为空」）、臆测无权限。修：`_subagent_system` 同样注入授权目录信息（用完整绝对路径读、在授权范围
  不会被拒、别臆测）。

## [3.21.3] - 2026-06-16

### Changed
- **任务清单引导加「勤于更新 + 动态调整」**（对标 Claude 的 TodoWrite 习惯）：此前引导偏重「打勾推进」，
  实测 agent 在自己执行或子任务带回新发现时，只打勾、不重规划清单。引导改为——边做边维护，且**只要新发现
  改变了计划（无论来自自己执行还是子任务结果），就用 update_tasks 增删/重排任务、而不只是给现有项打勾**，
  让清单始终反映真实下一步计划。注：这是引导，最终勤不勤、会不会重规划仍取决于模型（Claude 更勤）。

## [3.21.2] - 2026-06-16

### Changed
- **「子任务未完成」标注改为强命令式**：实测主 agent 收到 v3.21.1 的温和标注后，仍把不完整结果当完整总结
  输出。改成命令式——明确「不完整、不可直接当最终答案，必须 ①查缺 ②补全 ③再给结论，禁止直接总结/判定完成」。
  注：这是加强信号，能否让模型照做最终仍取决于其判断力（kimi 未必每次听，Claude 更可靠）。

## [3.21.1] - 2026-06-16

### Added
- **委派子任务撞上限时标注「未完成」**：子 agent 撞 max_steps 时（即便已强制收尾产出摘要），回灌主 agent
  的结果现在显式加标注「⚠子任务未完成：撞步数上限，以下为部分成果，请判断是否够用、不够就补充/换策略」。
  给主 agent 明确信号去判断是否补充，而非把不完整结果当最终结论。loop 暴露 `hit_max_steps` 标志、
  run_subagent 据此标注。（补不补最终仍取决于主 agent 判断力。）

## [3.21.0] - 2026-06-16

### Added
- **撞步数上限时强制收尾产出**：agent 循环（含委派子任务）达到 max_steps 时，此前最后一条是 tool_result、
  无任何文本总结就裸退——委派子任务因此回灌空摘要、主 agent 只能重做（实测：子 agent 搜了 60+ 次撞上限、
  「没有产出文本摘要」）。现在撞上限会**强制收尾一轮**：禁用所有工具、让模型基于已收集信息立即给出总结/结论，
  把它作为最后的 assistant 文本返回。比纯靠模型自律收敛更鲁棒（对不会自己刹车的弱模型尤其有用）。

## [3.20.2] - 2026-06-16

### Fixed
- **add-dir 授权后 agent 仍读不到——根因是模型不知道授权目录存在**：现场复现发现 agent 要么读成工作区
  （报「空目录」）、要么直接臆测「无权限」根本不调工具。授权信息此前只存在后端 `_extra_dirs`，从没告诉模型。
  修复：把已授权目录列表**注入 system prompt**（每轮重新生成、含最新授权），明确告知「用完整绝对路径调
  read_file / list_dir、在授权范围内不会被拒、别臆测无权限」。

## [3.20.1] - 2026-06-16

### Fixed
- **自主模式没有停止按钮**：crazy 走 `start_autonomous`（不经前端 `send()`），`streaming` 没置位、停止按钮
  不显示。改为 `crazy_start/done` 事件驱动独立的 `crazyRunning` 态来显示/隐藏停止按钮，停止按钮点击也识别 crazy。
- **add-dir 授权改按 cid 路由**：`add_dir/remove_dir/get_extra_dirs` 从「`self.active`」改为按前端当前会话 cid
  路由，确保授权一定落到用户当前会话（排除 active 与当前会话不一致导致授权给错会话）。`/add-dir` 反馈改为
  列出完整已授权目录，便于核对。（注：后端复测——授权后**同会话即时生效**，文件工具与会话共享 `_extra_dirs`
  引用、append 实时可见，逻辑本身无误。）

## [3.20.0] - 2026-06-16

### Added
- **crazy 模式护栏（无人值守安全）**：为 `/crazy` 自主循环加三层护栏——
  - **预算上限**：轮数（`crazy_max_rounds`）+ 墙钟时间（`crazy_max_seconds`，默认 30min）+ 累计 token
    （`crazy_max_tokens`，默认不限），任一超限回合间停；
  - **危险操作黑名单**：免确认态下毁灭性命令（`rm -rf` / `del /s` / `format` / `mkfs` / `dd` / fork bomb /
    关机重启 / `git push --force` / `git reset --hard`）由 gate 强制拦截，无人值守的最后防线；
  - **防空转**：连续 `crazy_stall_rounds`（默认 3）轮没动用任何工具（纯文字打转）判定空转、自动中止。
  - 结束原因细分（goal_reached / stopped / stalled / time_budget / token_budget / budget_exhausted）前端展示。

## [3.19.1] - 2026-06-16

### Changed
- **crazy 模式加委派引导**：三次真机压测（日志分析器 / roguelike×2 / minilang 解释器）kimi 都一轮串行搞定、
  从不主动用 `delegate`——委派对模型是反直觉行为。给 `_CRAZY_DIRECTIVE` 加一句引导：任务由多个独立子系统
  组成时用 delegate 拆给子 agent 并行、自己负责集成（同「别停在规划」的引导思路）。注：靠模型遵守。

## [3.19.0] - 2026-06-16

### Added
- **crazy 自主模式（无人值守，对标 Codex `/goal` + Ralph 循环）**：`/crazy <高层目标>` 启动——AI 自己把意图
  写成可判定的 GOAL，然后外层循环「规划→执行→跑测试验证→自评」干到底，每轮末尾自评 `[[DONE]]`（收工）
  或 `[[CONTINUE: 下一步]]`（带着自写的下一步续命），直到达成或用尽预算（`agent.crazy_max_rounds`，默认 20 轮）。
  期间**免权限确认 + ask_user 自动放行**（无人值守），随时点「停止」中止。
  - 后端：`Conversation.run_autonomous`（同步外层循环，可单测）/ `start_autonomous`（后台线程异步启动）；
    `_parse_crazy_verdict` 解析自评标记；`set_crazy_mode` 联动 gate 免确认 + ask_user 自动放行；`_CRAZY_DIRECTIVE` 注入 system。
  - 前端：`/crazy` 命令 + `crazy_start/round/done` 事件以系统提示行展示进度。

### 局限（诚实标注）
- crazy 模式价值高度依赖模型判断力：当前默认 kimi 跑长自主循环易跑偏/空转，**接 Claude 后才真正可用**
  （Codex 那套靠的就是强模型）。安全靠工作区围栏 + 轮数预算 + 随时可停；完全无人值守仍需谨慎使用。

## [3.18.0] - 2026-06-16

### Added
- **斜杠命令机制（对标 Claude Code）**：输入框输入 `/` 唤起命令提示菜单（↑↓ 选择、Enter/Tab 补全、Esc 关），
  低频操作走命令而非常驻按钮。首批：`/add-dir <目录>`（授权工作区外目录）、`/help`。命令结果以「系统提示行」
  显示在对话流。机制数据驱动（SLASH_COMMANDS），加命令只需加一行。
- **规划模式引导用 ask_user**：把 ask_user 放进规划模式工具集（_PLAN_TOOLS，否则会被过滤掉调不了），
  并在规划 directive 里引导——遇到需用户拍板的方向性取舍时用 ask_user 给选项，别只用文字罗列让用户打字答。

### Changed
- **移除工作区头部的 add-dir 按钮（＋📁）**：改走 `/add-dir` 命令——低频操作不占常驻按钮。对标 Claude Code「高频才上 UI、低频走命令」。
- **工具栏做减法 + 图标统一**：移除工作区「刷新」按钮（文件树本就在 agent 写文件 / 切会话时自动刷新，手动刷新冗余、用户从不点）；
  「折叠 / 展开」按钮的 `×` / `📁` 换成统一的矢量 chevron 图标（收起 » / 拉出 «），告别 emoji 与字符混搭的违和。

## [3.17.1] - 2026-06-15

### Changed
- **引导 shell 别绕过工作区/授权边界读文件**：发现模型有时用 run_powershell/run_bash 的
  type/cat/Get-Content 读文件，绕过 read_file 的工作区 + add-dir 限制（shell 本就能跑任意命令、访问任意
  路径，add-dir 只约束文件工具、未碰 shell）。给 shell 工具 description 加引导：读文件用 read_file/list_dir、
  别用 shell 读文件或访问工作区外。**注**：这是引导（靠模型遵守）；硬控制仍靠权限确认（shell 默认每次
  确认，别对它点"本会话全部允许"）。不做 shell 命令路径围栏（命令形态太多、脆弱，Claude Code 也不这么做）。

## [3.17.0] - 2026-06-15

### Added
- **ask_user 结构化提问（对标 Claude Code AskUserQuestion）**：agent 在规划/设计阶段需要用户拍板方向时，
  调 `ask_user` 给出问题 + 2~4 个选项，前端弹出**可勾选的按钮 +「其他」补充输入框**，用户点选或自己补充，
  结果回灌给 agent——不用再纯文字打字回答。阻塞/resolve 机制同权限 gate（emit 事件 + threading.Event）。
  `tools/ask.py`（AskUserBinding + AskUserTool）+ `resolve_ask_user`（按 cid 路由）+ 前端 renderAskUser。
  只给主 Agent；description 引导"规划/方向性取舍时用、小事别问"。test_p3 18/18。

## [3.16.1] - 2026-06-15

### Fixed
- **流式输出时能往前翻看历史了（智能粘底，对标主流）**：之前每个 chunk 都强制把对话拽到底，长会话边
  输出边往上翻看会被拽回去。现在改智能粘底——只在你**已在底部**时才跟随流式输出；往上翻了就尊重你的
  阅读位置、不拽回，滚回底部又自动恢复跟随。主动发送 / 切换对话仍强制到底。`scrollChat` 加 `stickBottom`
  判断（chat scroll 监听），新增 `scrollChatForce`。

## [3.16.0] - 2026-06-15

### Added
- **额外授权目录 add-dir（对标 Claude Code）**：补齐 hermes 之前"硬限工作区、不能读外面"的差距。默认仍
  严格限工作区，但可通过右侧面板「＋📁」按钮选一个工作区外的目录授权，之后工具能读写其中文件——解决
  "参考隔壁项目代码""读外部共享配置/数据"等跨文件夹场景。授权目录共享给主 + 子 Agent、add/remove 实时
  生效；默认安全（未授权目录仍拒）。`Tool.resolve` 放宽到"工作区或任一已授权目录"，新增
  `add_dir`/`remove_dir`/`get_extra_dirs`/`add_dir_dialog`（复用 FOLDER_DIALOG）。test_p3 17/17 + conversation 52/52。

## [3.15.2] - 2026-06-15

### Fixed
- **auto_test 区分「命令没跑起来」vs「测试断言失败」**（实跑复杂工程时发现）：之前把"test_command
  命令找不到/没执行起来"也当测试失败回灌，模型就去瞎修——实测 kimi 甚至跑去系统造 `python→python3`
  符号链接 hack 环境。现在区分：命令没跑起来（returncode 127/126/9009/-1 或输出含 command not found 等）
  = **配置/环境问题**，提示用户检查 test_command、不进修复循环；只有真·断言失败才回灌让模型修。
  `_run_test_command` 带回 returncode，新增 `_is_launch_failure` 判定，前端 toast 区分「⚠ 命令没跑起来」。

## [3.15.1] - 2026-06-15

### Fixed
- **.gitignore 带 BOM 时第一行模式失效**（修 v3.15.0 的 ⑦）：Windows 记事本默认存 utf-8 **带 BOM**，
  读 .gitignore 用了 utf-8 → 第一行模式粘上 BOM（如 `﻿*.log`）匹配失效、对应文件没被滤掉
  （用户实测 `debug.log` 仍被 grep 到）。改用 **utf-8-sig** 读、自动去 BOM。test_ignore 5/5（含 BOM 用例）。
  （`.env` 也踩过同样的 BOM 坑，已知雷区。）

## [3.15.0] - 2026-06-15

对标 Claude Code 逐项核实后做的 4 项优化（只做真差距，砍掉对标不准/已不弱的）。后端已自测；
前端会话搜索/快捷键已 Windows 验证通过。

### Added / Changed
- **① 工具并行执行**：只读工具（read_file/list_dir/grep_search/glob_search/code_outline/find_symbol）
  标记 `parallel_safe`，同轮多个调用**并发执行**（复用 FR-10.5 的 ThreadPoolExecutor，之前只 delegate 并发）。
  多文件读取/检索快几倍；写工具/shell 仍顺序，安全。
- **⑦ .gitignore 感知**：新增轻量 `ignore.py`，文件树（build_tree）+ grep + glob 在硬编码 _SKIP_DIRS 之外
  **额外尊重项目 .gitignore**，大项目不再被生成物/缓存/日志淹没（覆盖 name/`*.ext`/`dir/` 等常见模式）。
- **⑥ 会话搜索**：会话列表上方加搜索框，按标题实时过滤。
- **⑧ 全局快捷键**：Ctrl/⌘+N 新会话、Ctrl/⌘+Shift+P 切换规划、Ctrl/⌘+K 聚焦会话搜索。

### 客观评估后未做
- ③ 项目级预批：已有「本会话全允许」兜底，评估后认为没必要；
- ② 代码块 Apply 按钮：是 Cursor/IDE 功能，Claude Code 也是模型直接调工具写，对标不准；
- ④ shell 前台流式：已有 background+read_process_output 兜底，Claude Code 前台也阻塞；
- ⑤ 记忆语义去重：hermes 自动抽取+模型去重已比 Claude Code（手动 CLAUDE.md）更自动。

### 验证
- test_p3 16/16（含只读工具并发）、test_ignore 4/4（matcher + 文件树/grep/glob 过滤）、全回归全绿。
  **前端会话搜索/快捷键交互已 Windows 验证通过。**

## [3.14.1] - 2026-06-15

### Added
- **auto_test 支持项目级 `test_command`**：在某项目工作区根放 `.hermes.yaml`、写一行
  `test_command: <命令>`，该项目就用它（覆盖全局 config.agent.test_command）——多项目切换各用各的
  测试命令、不必回改全局。`config.read_project_config` 读 `.hermes.yaml`；`_effective_test_command`
  项目级优先、全局兜底。test_conversation 50/50 + 全回归绿。

## [3.14.0] - 2026-06-15

UI 与规划呈现优化。已 Windows 验证通过。

### Added
- **规划结果结构化呈现**：规划模式强化引导——① 用 update_tasks 输出有序实施步骤为主体（不堆大段叙述）；
  ② 回复附一张 mermaid 图（任务分解/模块拆解用 `mindmap`、有先后或分支的流程用 `flowchart TD`，模型按
  任务性质自动选）；③ 关键决定/取舍写 update_notes。复用已有 mermaid 渲染（懒加载）+ 任务栏。
  实测 kimi 产出 9 项清晰有序清单 + 自动选对 flowchart、语法正确、结构合理（主流程 + 关键决策子节点）。

### Changed
- **改动区/检查点默认折叠**：右侧面板的「改动」「检查点」两区默认折叠（只显示标题如「检查点 (3)」），
  点标题展开/收起、状态记 localStorage——内容增多时不再挤压下方文件预览区。「全部回退」按钮点击不误触折叠。

## [3.13.0] - 2026-06-15

工程体验两件大的（对标 Claude Code）+ 一个身份修复。后端逻辑已自测全绿；前端 diff 渲染 + auto_test
真实测试场景已 Windows 验证通过。

### Added
- **A · 对话流内联 diff**：write/edit/multi_edit 每次改动在对话流里内联显示 diff（绿 `+`/红 `-`，复用
  现有配色），改了什么一眼可见、不用切右侧面板。diff **只走前端、不回灌模型**（省 token）——`ToolOutput`
  带 `type=diff` 块，`loop._emit_result` 提取给前端、`_exec_calls` 回灌时过滤掉；`make_diff_block` 用
  difflib 出本次 before→after 的 unified diff。
- **B · 验证闭环 auto_test（FR-11.2c）**：开 `auto_test` + 配 `test_command`（如 `pytest -q`），一轮改过
  文件就在收尾跑测试；**失败把输出回灌、复用同一循环让模型修，限 `test_max_iters` 次**（默认 2），通过
  即止——对标 Claude Code"写完自己验、错了自己改"。沿用 auto_verify 的失败回灌模式 + auto_review 的收尾
  时机。默认关、配好命令再开；前端 toast 显示通过/失败/迭代进度。

### Fixed
- **问"你是什么模型"答错（如 kimi 自称 Claude）**：system_prompt 没告诉模型它实际跑在哪，模型按训练语料
  瞎答。现运行时注入当前选中档案的真实 model id（如「kimi-k2.6」），据实回答、随切换的模型动态变。

### Changed
- `ToolOutput` 支持 `==`/`in`/`str()` 按 `text` 比较（向后兼容：返回富内容的工具不破坏旧字符串断言）。

### 验证
- test_p3 15/15（diff 产出 + 不回灌模型）、test_conversation 49/49（模型身份注入 + auto_test 闭环 4 例）、
  全回归全绿。**前端 diff 渲染 + auto_test 真实测试场景已 Windows 验证通过。**

## [3.12.0] - 2026-06-15

前端配置模型入口（体验升级，不用再改 config.yaml 选模型）。后端 + 持久化已自测；前端下拉已 Windows 验证通过。

### Added
- **前端可选主模型 + 委派模型，并持久化**：顶部新增「委派模型」下拉（含"跟随主模型"），选委派子任务
  （researcher 等）用哪个模型档案；主模型下拉现也存回 config.yaml（重启保留）。
  - `subagent_model` 改内存**即时生效**（委派时读 `cfg.agent.subagent_model`，不用重启）；
  - 持久化用**按行正则替换**写回 config.yaml（只动 `active_model:` / `subagent_model:` 两行），
    **绝不整文件 yaml.dump**——完整保留注释与多行 system_prompt（实测 diff 仅 2 行变、235 行不变）；
  - 仅"选择/切换"已配好的模型档案；新增/编辑档案（provider/model/api_key/max_tokens）仍改文件。
  - 接口：`get_models`（加 subagent）/`set_active_model`（加持久化）/`set_subagent_model`（新增）；
    `config.persist_model_selection`（行替换，可传 path 便于测试）。

### 验证
- test_model_select 5/5（行替换不破坏注释/system_prompt/models；subagent 行启用↔注释往返）、
  test_conversation 44/44（含 set_subagent_model 内存生效 + 校验）、全回归全绿；真实 config 未被污染。
  **前端下拉交互已 Windows 验证通过。**

## [3.11.2] - 2026-06-15

修 v3.11.1「改标题联动重命名工作区文件夹」在 Windows 实测未生效，并加诊断。已 Windows 验证通过
（根因：资源管理器停在该 `data/workspaces/<id>` 目录会锁住它致重命名失败，关掉即生效；非代码缺陷）。

### Fixed
- **改标题后工作区文件夹未改名**：
  - 把"运行中避让"从宽泛的 `is_busy()`（含 queued/awaiting/队列非空）收紧为 `_running_turn`（仅真正
    执行一轮时才避让）——避免做过排队/steering 操作后若有状态残留，导致整个改名被误跳过、只改了标题。
  - 重命名失败不再静默吞：写诊断到 stderr（`[rename_ws sid=…]`，`python -m agentcore.app` 控制台可见），
    并在对话区弹一条提示，便于定位 Windows 下的占用/权限原因（最常见：**资源管理器正打开着该目录会锁住
    它，致重命名失败**——改名时先别用资源管理器停留在那个 `data/workspaces/<id>` 目录）。

## [3.11.1] - 2026-06-15

打磨 v3.11.0 对话体验：修一个排队竞态 + 会话改名联动工作区文件夹。全回归全绿；已 Windows 验证通过。

### Fixed
- **任务已结束、再发消息却提示"已排队，当前任务完成后处理"**（v3.11.0 引入的竞态）：`enqueue` 在
  `put` + 启 worker *之后* 才查 `state`，而新启的 worker 抢先把 `state` 改成 `running`，令空闲时发的
  新消息被误判成排队。改用 `_running_turn` 在 put 前做 snapshot 判断"是否真有一轮在跑"——空闲发送只走
  正常 state 流转、不再误报；worker 也提前在 `state=running` 之前置 `_running_turn`，缩小并发窗口。

### Added
- **改会话标题 → 自动工作区文件夹同步改名**：原 `data/workspaces/<id>` 是纯数字、看不出是哪个项目。
  现改标题时把该文件夹一并重命名为标题（纯标题；撞名才加 `-<id>` 兜底；Windows 非法字符转义为 `_`）。
  **只动 `workspaces_root` 下自动分配的目录**——用户手动绑定的外部真实项目、正在运行的会话工作区均不动；
  目录内容随之搬移，DB 绑定路径与 live 运行时工作区同步更新。只对今后改标题生效，现有数字文件夹保持不动。

## [3.11.0] - 2026-06-13

一轮"对话体验"优化：对话排队/steering 引导 + 委派/咨询行为调优 + 步数上限放宽。
后端/行为部分 Linux eval 6/6 + 逐条补测 + steering 真模型实测全过；前端交互已 Windows 验证通过。

### Added
- **执行中可继续发消息 + steering 引导（对标 Claude Code）**：当前对话正在跑时也能输入并发送。
  **纯文本追加 = steering**——注入当前任务的**下一个工具边界**（附进同一条 tool_result 消息，不破坏
  user/assistant 交替），模型下一轮即看到「工具结果 + 你的补充」并据此重估、调整方向，而非等任务做完
  再当独立新事处理。*真模型实测*：任务"逐个读文件报告内容"跑动中追加"读完再统计总词数"，kimi 在后续
  轮纳入并正确给出总数。**带附件的追加 / 空闲时发送 = 排队成新一轮**（图片无法塞进 tool_result）；
  当前任务结束仍有未注入的追加→兜底排队成新一轮。「发送」与「停止」并存（运行中按钮显示「排队」），
  追加即 toast 区分「已追加，下一步纳入」/「已排队」。**注**：前端流式交互待真机验；「停止」清空全部
  待注入 + 排队（未做单独取消某一条）。
  实现：`AgentLoop.run(take_injects=…)` 在工具回灌时拉取补充附进 user 消息；`Conversation` 用
  `_running_turn` 标志区分注入 vs 排队、`_take_injects`/`_drain_injects_to_queue` 收发与兜底。
- **eval 套件扩到 6 任务**：新增 `delegate_implicit`（隐式调研→应自发并行委派，专防"prompt 精简致
  委派退化"再现）、`quick_query`（简单事实咨询→应快、不委派）。

### Changed
- **委派引导补回触发词**（修 v3.10.1 精简的退化）：v3.10.1 误把"看到'逐一/分别/每个/各自'这类
  遍历措辞就并行委派"当冗余砍了，导致"逐一分析很多文件/查很多网页"这类调研退回串行（实测旧版委派 4、
  砍后 0）。现补回触发词，eval `delegate_implicit` 自发委派 3 个子任务恢复。
- **简单事实咨询走快速通道**：问版本号/日期/赛程/定义这类简单事实，引导"知道就直接答、要联网就搜
  一两次综合作答、别逐页查证、别委派、别建清单"——治"简单咨询拖很多步"（实测 quick_query 6 秒 2 步）。
- **单轮步数上限 25 → 40**：给调研类长任务余量（配合 80% step_warning + 委派分摊步数——每个子 Agent
  另有独立预算，是长任务不撞墙的正解）。

### 验证
- 逐条核对 system_prompt 删除项对性能无影响：eval 6/6 + 补测（multi_edit/行号前缀陷阱、后台进程
  三件套、可视化 mermaid、remember、update_notes）全部正确触发。除委派触发词（已修）外删除项均无影响。

## [3.10.1] - 2026-06-13

### Changed
- **全局 system_prompt 大幅精简（8248 → 1328 字符，砍 84%，对标 Claude Code 工程纪律）**：
  原 prompt 是一路加 FR 堆出来的，约 2/3 是"逐个工具的用法说明"，与工具自身 description 重复
  （双花 token + 稀释指令）。现遵循"工具用法靠 description、system_prompt 只放跨工具的行为准则 +
  协同策略"——删掉读写/git/联网/进程/任务/笔记/委派/记忆的逐工具教学，保留开发准则与几条
  单工具说明覆盖不到的协同策略（规划/并行委派/联网时机/git 礼仪/记忆分层）。
  **eval 4 任务前后对比 4/4→4/4 零退化**，关键行为（自发并行委派、git 开分支礼仪）均保持；
  每次请求省约 2000+ token。纯 config 改动、跨平台一致 + eval 充分验证，跳过真机验证。

## [3.10.0] - 2026-06-12

P12 检查点改为自动打点（方案A，对标 Claude Code/Cursor）。已 Windows 真机验证通过。

### Changed
- **检查点从"模型手动调工具"改为"自动打点"（P12，对标 Claude Code/Cursor）**：每个回合**首次改文件前
  系统自动**快照"本回合改动前的文件 + 任务清单 + 工作笔记"，回合内多文件改动累加进同一个检查点；
  用户在面板「检查点」区一键「回到此处」即撤销整回合改动。**不再依赖模型自觉**（旧设计模型常忘了调，
  实测模型全程没碰检查点也照样有完整回退点）。`agent.auto_checkpoint` 默认开；自动检查点保留最近 30 个。
- **「回到此处」改为图标按钮**（回拨箭头）+ 悬浮提示，宽度固定、不再随标签长短忽宽忽窄；空列表不占位。

### Removed
- 移除模型可调的 `checkpoint` 工具（能力已被自动打点取代；模型只管干活，检查点系统替它管）。
- 移除工作区面板的手动「＋ 存检查点」按钮（自动打点已覆盖其价值——你想留的"满意状态"，下一轮
  agent 动手前的自动打点会替你存住；后端 create_checkpoint 方法保留备用）。

## [3.9.0] - 2026-06-12

P12 FR-12.1 provider 韧性（瞬时错误自动退避重试）。纯后端容错逻辑、跨平台一致，
test_retry 8/8 离线覆盖 + 真模型冒烟正常路径无回归，跳过 Windows 真机验证。

### Added
- **provider 自动重试/退避（P12 FR-12.1）**：模型调用遇瞬时错误（网络抖动 / 429 限流 / 5xx 服务端 /
  529 过载）自动指数退避重试（最多 3 次、封顶 20s、带抖动）——弱网/限流下不再"一抖就报错"。
  **只在还没吐出任何内容前重试**（避免重复输出）；与已有的 cache_control 降级共存（缓存不被端点
  接受时摘掉缓存重试，不占退避预算）。anthropic / openai 两协议都覆盖。

## [3.8.0] - 2026-06-12

P11 FR-11.8 用量可观测（token/缓存/步数 + 步数预警 + CLI --json usage）。已 Windows 真机验证通过。
**P11 至此全部收官。**

### Added
- **用量可观测（P11 FR-11.8）**：每轮结束显示本轮 token 用量（输入/输出/**缓存命中**）与工具步数
  （对话区一条克制脚注；CLI `--json` 输出含 `usage`）；步数接近上限（≥80%）弹「接近上限」预警，
  长任务"在推进还是打转"可感知。anthropic 协议全量回传（实测方舟含 cache_read）、openai 协议尽力而为
  （端点不回传则优雅留空，不强加参数以免打挂）。**不内置美元定价表**（价格多变易过时），只给客观
  token/步数。

## [3.7.0] - 2026-06-12

P11 FR-11.7 CLI/headless 入口。已 Windows 验证（含免安装入口修复）。

### Added
- **命令行 / 无头模式（P11 FR-11.7）**：新增免安装入口 `python run_cli.py`（pip 安装后亦可用
  `hermes-cli`）——不开 GUI、单任务进出，复用与桌面端完全相同的内核（工具循环/委派/检查点/规划…），
  解锁脚本化与 CI。助手文本→stdout、工具活动→stderr；`--json` 输出结构化结果、退出码 0/1 可进 CI；
  `--plan` 只读规划态、`-w` 指定工作区、`-m` 选模型、管道 stdin 喂任务。默认自动批准危险操作，
  config `permissions.deny` 仍拦截。（`Api` 增 `emit` 钩子以支持无头事件流。）

### Fixed
- `python -m agentcore.cli` 在未 `pip install` 时报 `No module named agentcore.cli`（agentcore 在
  src/ 下、不在导入路径）：补根级 `run_cli.py` 免安装入口，自动把 src 加进路径。

## [3.6.0] - 2026-06-12

P11 FR-11.6 检查点/任务级回滚（+ 子 Agent 失败重试）+ 委派引导改善（多个独立大块时主动并行委派——
实测"三块调研"从主 Agent 串行 377s 变为 3 子任务并行 222s，中小任务仍不委派）。已 Windows 真机验证通过。

### Added
- **检查点与一键回退（P11 FR-11.6）**：新增 `checkpoint(label)` 工具——模型在里程碑/风险改动前
  快照「本对话改过的文件 + 任务清单 + 工作笔记」；工作区面板新增「检查点」区，可手动「存检查点」、
  对任一检查点「回到此处」**一键把文件+任务+笔记一起还原**（确认后执行）。**模型只能创建、不能回退**
  （回退仅用户操作，防模型自己抹掉成果）。git 无关、与改动台账同口径（run_powershell 改的不计）。
- **子 Agent 失败自动重试一次（P11 FR-11.6b）**：委派的子任务若子循环异常，自动附上失败原因重试
  一次，仍失败才回灌主 Agent（配置/密钥错不重试）。

## [3.5.0] - 2026-06-12

P11 FR-11.5 Plan mode（只读规划态开关）。已 Windows 真机验证通过。

### Fixed
- **输入区「规划」按钮样式与附件/发送不统一**（验证反馈）：附件与规划按钮改用**矢量 SVG 图标**
  （回形针 / 清单），复用全站 `.icon-btn` 规范，等宽方形、与输入框同高；规划开启时图标随高亮变金。

### Added
- **规划模式（P11 FR-11.5）**：输入区新增「📋 规划」开关（按对话独立）。开启后本对话只用**只读
  工具**勘察现状、并用 update_tasks/update_notes 产出实施方案——**写文件/执行命令/委派等全部禁用**，
  模型不会动手只会规划；顶部提示条标明状态、发送按钮变「规划」。确认计划后关掉开关即转入正常执行。
  治"方向错最贵"：复杂改造先看清楚、定好计划再动手。

## [3.4.0] - 2026-06-12

P11 FR-11.3 上下文工程升级（工作笔记外置 + 压缩可重读引用）。已 Windows 真机验证通过。

### Added
- **工作笔记（P11 FR-11.3a）**：新增 `update_notes` 工具（整份替换，与任务清单互补：清单记待办、
  笔记记已确认的事实/决定/进展/坑）。笔记存会话级（跨重启）、注入 system「[工作笔记]」块
  （**抗上下文压缩**）——长任务里旧的工具往返即便被压缩丢弃，沉淀的结论仍在。

### Changed
- **压缩瘦身的可重读引用（P11 FR-11.3b）**：上下文压缩截短旧的大 `read_file` 结果时，标记里
  写明来源文件并提示「可用 read_file 重读 <路径>」，模型需要细节时能精准重取，而非只剩残文。

## [3.3.0] - 2026-06-12

P11 FR-11.4 细粒度权限 allowlist（按工具+参数模式的 allow/deny + 确认条「总是允许这类」）。
已 Windows 真机验证通过。

### Added
- **细粒度权限规则（P11 FR-11.4，治长任务确认疲劳）**：config `agent.permissions.allow/deny`
  按「工具+参数模式」放行或拦截危险操作——规则形如 `run_powershell(git *)`、`write_file(docs/*)`、
  `git_status`（glob 匹配命令/路径/URL）；**deny 优先于 allow，也优先于「本会话全部允许」**
  （危险命令如 `run_powershell(rm *)` 不会被绕过）。
- **确认条「总是允许这类」**：批准时一并把推导出的规则（命令→首词通配、路径→父目录通配）
  加入本会话白名单，后续同类操作免确认（重启不保留）。

## [3.2.0] - 2026-06-12

P11 FR-11.2 验证闭环（写入后零成本校验 + 收尾自动评审）。已 Windows 真机验证通过。

### Added
- **写入后零成本语法校验（P11 FR-11.2a，默认开）**：write_file/edit_file/multi_edit 落盘后
  自动按扩展名校验——`.py` 用标准库 ast、`.json` 用 json（无依赖必可用），`.js/.ts` 等用
  `node --check`（无 node 静默跳过）；改坏时**当步**就在工具结果里报出语法错（含行号），
  模型立即自我修正，不必等它自己想起来验。`agent.auto_verify` 可关。
- **收尾自动代码评审（P11 FR-11.2b，默认关）**：开 `agent.auto_review` 后，一轮里改过文件就在
  收尾自动派 reviewer 子 Agent 审本轮 diff（只读、结论经子任务块呈现）；纯对话/只读轮零开销。

## [3.1.0] - 2026-06-12

P11 FR-11.1 联网检索（web_search + web_fetch + 外链系统浏览器打开修复）。已 Windows 真机验证通过。

### Added
- **联网检索（P11 FR-11.1）**：两个只读工具（免确认、只读子 Agent 角色可用）——
  `web_search`（免 key 搜索：Bing 优先、DuckDuckGo 兜底，返回标题/URL/摘要，跳转链自动还原
  真实 URL）、`web_fetch`（抓网页转可读正文，去脚本保标题，JSON/纯文本直出，2MB 下载/2 万字符
  输出上限；也可抓 localhost 检查自己起的 dev server）。**零新依赖**（标准库实现）。
  config 新增 `web` 段（enabled / search_engine / timeout / max_results / fetch_max_chars，
  `enabled:false` 不注册、行为同 3.0.0）；system_prompt 加"先搜后答、附来源"指引。
  实测：模型自发 search→fetch PEP 原文→交叉验证→带来源准确回答时效性问题。

### Fixed
- **对话里点击链接把整个应用窗口导航走且无法返回**（首轮 Windows 验证反馈）：现统一拦截——
  http(s) 链接用**系统默认浏览器**打开（应用窗口不动），javascript:/file: 等链接一律阻止。

## [3.0.0] - 2026-06-12

P11 首个交付：FR-11.0 本地评测基准。已 Windows 验证通过（离线自检 5/5 + 全量真跑 4/4 +
退出码语义 + 不污染 data/）。

### Added
- **本地评测基准（P11 FR-11.0）**：`scripts/eval/` 无头评测套件——固定 4 任务
  （bugfix / 功能+git / 代码库理解 / 并行委派）+ 全自动判分 + 一键跑分
  （`python scripts/eval/run_eval.py`，可 `--task`/`--model`，退出码可进 CI）。
  判分器可离线自检（`test_eval.py`，不调模型）；为 P11 后续优化提供可重复的"尺子"。
- 打包说明补充：目标机需安装 Git for Windows（2.0.0 起 git 功能依赖系统 git，未装则
  该功能可读报错、其余不受影响）。

## [2.4.0] - 2026-06-12

P10 FR-10.5 并行委派 + 自定义角色 + 任务联动。已 Windows 真机验证通过。**P10 至此全部收官。**

### Added
- **并行委派（P10 FR-10.5）**：模型在同一轮发出的多个 `delegate` 调用会**并发执行**（上限 4），
  互不依赖的调研/评审等子任务同时跑、前端多个子任务块同时滚动；结果按原调用顺序回灌，
  停止按钮照常级联停掉全部子任务。单个委派与普通工具的顺序语义不变。
- **自定义子 Agent 角色**：`config.yaml` 的 `agent.roles` 可新增（或同名覆盖）角色——
  `label` 显示名 / `directive` 职责指令 / `tools` 工具白名单（所列即所得，省略=全工具）/
  `model` **按角色配模型**（省略 = subagent_model → 当前对话模型）。delegate 工具的角色列表与
  描述按配置动态生成；未知角色仍回退 general。
- **任务清单联动**：新增 `delegated`（🤖 已委派）状态——把清单项委派出去时标 delegated，
  收到子任务摘要后转 completed（提示词引导，沿用工具驱动哲学）。

## [2.3.0] - 2026-06-11

P10 FR-10.4 压缩升级 + prompt caching。已 Windows 真机验证通过（含方舟缓存命中、
MiniMax 降级无感、模型摘要连贯性）。

### Added
- **prompt caching（P10 FR-10.4b）**：anthropic 协议请求默认加 cache_control 前缀缓存断点
  （system / 工具表 / 最后一条消息）——长会话与多轮工具循环的输入大头逐轮命中缓存，成本与
  首 token 延迟显著下降。**实测方舟 coding 端点真实命中**（二次调用 cache_read 3700+ tokens、
  input 5286→1446）；不支持的端点自动降级重试并记账不再尝试（不报错）；可按模型档案
  `prompt_cache: false` 强关。openai 协议端点自动缓存、无需配置。

### Changed
- **上下文压缩摘要升级为模型生成（P10 FR-10.4a，对标 /compact）**：超预算丢弃旧回合时，摘要
  不再是逐条截断拼接，而是由模型把被丢段压成忠实、信息密集的一段（保留目标/已做改动/未完成事项/
  约束）。摘要**按覆盖范围缓存**：切点不动零额外调用、切点前移只做一次增量合并；失败自动回退原
  启发式（短时退避防反复白调）。`context.model_summary`（默认开）/`context.summary_model`
  （默认当前对话模型）可配；🗜 提示会标注"模型生成的摘要"。

## [2.2.0] - 2026-06-11

P10 FR-10.3 后台命令/长进程。已 Windows 真机验证通过。

### Added
- **后台命令/长进程（P10 FR-10.3）**：`run_powershell` 支持 `background:true`——dev server /
  watch 等长进程后台启动、立即返回进程编号（启动仍过权限确认）；配三件套：
  `list_processes`（列后台进程与状态）、`read_process_output`（**增量**读新输出，适合轮询日志）、
  `stop_process`（整棵进程树终止）——三件均不弹确认，list/read 进只读子 Agent 角色白名单。
  解锁"启动服务 → 测试 → 看日志"的 Web 开发场景。
- **进程不残留**：关窗、删除会话运行时自动杀掉该对话全部后台进程（Windows `taskkill /T /F`
  整树终止 + 无黑窗闪烁）；对话「停止」按钮**不杀**后台进程（dev server 是交付物，
  用 stop_process 停）。每对话上限 8 个并发后台进程；输出环形缓冲 20 万字符防膨胀。

## [2.1.0] - 2026-06-11

P10 FR-10.2 读写精度。已 Windows 真机验证通过。

### Added
- **multi_edit 工具（P10 FR-10.2）**：同一文件多处精确替换，按序应用、**全部成功才落盘**
  （任意一处失败整个文件不改，报第几处因何失败）；每处可单独 replace_all。危险工具过确认、
  入改动台账。
- **edit_file 加 `replace_all`**（默认 false）：多处匹配时可一次全替换。

### Changed
- **read_file 输出带行号**（`行号+制表符+内容`，对标 Claude Code），新增 `offset`/`limit`
  分段读；按行流式读取，大文件不再 200KB 一刀切静默截断——**没读完会明确提示
  "继续读请用 offset=N"**；超长单行（>2000 字符）截断加标记。
- **edit_file 失败信息可操作**：未找到时自动诊断三类原因并分别提示——行号前缀带进来了 /
  空白缩进不一致 / 确实不存在（请先 read_file 核对）；多处匹配时报次数并提示补上下文或
  replace_all。`test_fs_rw.py` 12/12。

## [2.0.0] - 2026-06-11

P10 FR-10.1 Git 集成（2.X 首个交付）。已 Windows 真机验证通过（两轮：首轮功能 +
二轮 commit/分支/批量回退）。

### Added
- **Git 工具五件套（P10 FR-10.1）**：`git_status` / `git_diff`（对 HEAD，未跟踪文件可看内容）/
  `git_log` 为只读工具（不弹确认，模型随手看）；`git_commit`（可 paths 限定，默认提交全部）/
  `git_branch`（建/切分支）为危险工具（过权限确认）。走 git CLI、无新依赖；未装 git / 非 git
  仓库时返回可读错误。子 Agent 只读角色（researcher/reviewer/tester）可用只读三件。
- **仓库礼仪（引导不硬拦）**：system_prompt 写入——只在用户要求时提交、用户未明说不在默认分支
  （main/master）直接提交（先开分支）；commit 结果显示分支名，默认分支直接提交附 ⚠ 提醒。

### Changed
- **面板「改动」区升级 git 语义（git 工作区自动启用）**：工作区根是 git 仓库时，改动区显示
  **全部未提交改动**（暂存/未暂存/未跟踪，**跨重启、跨轮次**、含用户手改），标题为
  「未提交改动·git」，diff 对最近一次提交（HEAD），「回退」=丢弃未提交改动（确认文案写明范围）。
  非 git 工作区沿用 1.6.0 的内存台账（仅本对话改动），行为不变。`test_git.py` 13/13。

### Fixed
- **git 模式「全部回退」改动上百时卡 UI**（首轮 Windows 验证反馈）：由逐文件回退（每文件 2 次
  git 子进程）改为**批量执行**（reset / checkout 分批、未跟踪文件进程内删除），改动数不再影响
  git 调用次数。
- **未配置 git 身份时 commit 的报错**改为中文可操作提示（给出 user.name/email 配置命令，
  也可让 Agent 代配）。

## [1.6.0] - 2026-06-11

P9 FR-9.4 多文件改动评审/回退 + 上下文瘦身。已 Windows 真机验证通过。**P9 复杂项目支持至此全部收官。**

### Added
- **多文件改动评审与回退（P9 FR-9.4a）**：Agent 改过的文件（write_file/edit_file，含子 Agent）
  自动进**改动台账**（基线=本对话第一次动它之前）；右侧工作区面板新增「改动」区——变更列表（＋新增/✎修改/🗑删除），
  点文件看**着色 diff**，支持单文件「回退」与「全部回退」（均确认）。改回原样自动消失；台账内存级、随对话运行时
  存在（重启即清，文件不受影响）。已知限制：run_powershell 改的文件不追踪。`test_changes.py` 8/8。

### Changed
- **上下文压缩更细粒度（P9 FR-9.4b）**：超预算时**先把旧回合里超长的工具结果截短**
  （保留头部+标记，最近回合不动、不破坏工具调用配对），够了就不再丢整回合；仍超才走原有的整回合丢弃+摘要。
  长会话多读几个大文件后，压缩丢的信息明显更少。

## [1.5.0] - 2026-06-11

P9 FR-9.2 代码库检索/索引。已 Windows 真机验证通过（含超量符号截断压测）。

### Added
- **代码库检索/索引（P9 FR-9.2）**：新增两个只读工具补足 grep/glob 给不了的结构化检索——
  `code_outline`（列目录/文件的类、函数、方法大纲 + 签名 + 行号，**不读全文件就掌握项目结构**）、
  `find_symbol`（按名**找定义**，比 grep 准——只给定义不给所有提及；先精确、全无回退子串）。Python 用标准库
  `ast` 精确抽取，JS/TS/Go/Rust/Java/C 等用轻量正则兜底；按需扫描、无新依赖、跳噪音目录并带上限。只读子 Agent
  角色（researcher/reviewer/tester）也能用。`test_codeindex.py` 8/8。

## [1.4.0] - 2026-06-11

P9 FR-9.5 子 Agent 角色与工具限权。已 Windows 真机验证通过。

### Added
- **子 Agent 角色与工具限权（P9 FR-9.5）**：`delegate` 新增 `role` 参数，内置四种角色——
  `general`（默认，全工具）/ `researcher`（只读：读/列/搜索）/ `reviewer`（只读，评审）/ `tester`（只读+可跑命令）。
  每个角色有定制职责指令 + 工具白名单：**只读角色根本拿不到写文件/编辑/命令/截图等工具**，既更专注也更安全。
  主 Agent 可据子任务性质挑角色（调研→researcher、评审→reviewer、测试→tester、改代码→general），
  让"主 Agent 调度 + 子 Agent 专精分工"真正成立。子任务块头部显示角色名。未知/缺省角色回退 general（向后兼容）。
  对标 Claude Code 的自定义 agent（定制提示 + 工具限权）。`test_delegate.py` 11/11。

## [1.3.0] - 2026-06-11

P9 FR-9.3 子 Agent / 委派。已 Windows 真机验证通过。

### Added
- **子 Agent / 委派（P9 FR-9.3）**：复杂、相对独立的子任务可用新工具 `delegate` 交给一个
  **独立上下文的子 Agent**（同工作区、同工具，但不含 delegate/update_tasks，不嵌套、不碰主任务清单）去完成，
  跑完**只把一段摘要**回灌主 Agent——主对话上下文保持精简。子 Agent 默认用当前主模型、可配
  `agent.subagent_model`；危险操作仍走同一权限 gate，点「停止」连子 Agent 一起停。前端新增可折叠
  「🤖 子任务」块：实时显示子 Agent 的工具调用与输出，完成后收起、留下摘要（重载历史也能看到摘要）。
  `test_delegate.py` 8/8 + run_subagent 假 provider 集成测试。

## [1.2.0] - 2026-06-11

P9 FR-9.1 任务规划与拆解（P9 首个特性）。已 Windows 真机验证通过。

### Added
- **任务规划与拆解（P9 FR-9.1）**：复杂/多步任务时 Agent 用新工具 `update_tasks` 维护一份
  可勾选的子任务清单（对标 Claude Code TodoWrite，整份替换式：pending/in_progress/completed）。对话区顶部
  新增可折叠「任务清单」面板（进度计数 + ✅/🔄/⬜，状态随进展更新、折叠态记 localStorage）。清单按会话持久化
  （`hermes.db` 新增 `session_tasks` 表，随删会话级联）、按对话各自显示；当前清单注入 system_prompt，
  **上下文压缩/重启后模型仍记得自己的计划**。`update_tasks` 为非危险操作、不过权限 gate。`test_tasks.py` 9/9。

## [1.1.2] - 2026-06-11

启动加速 + 清理 pywebview 序列化报错。已 Windows 真机验证通过
（`导航开始→pywebviewready` 从 ~930ms 降到 ~330ms，日志无报错）。

### Fixed
- **启动慢 + 启动时一串 `RecursionError` 与 WebView2 COM 跨线程错误**（`window.native.AccessibilityObject…` /
  `CoreWebView2 can only be accessed from the UI thread`）：根因是 pywebview 序列化 js_api 对象时遍历其
  **公有属性**，扎进我们存的 `Api.window`（pywebview Window）→ 原生 .NET/WebView2 对象图，`Rectangle.Empty`
  自指无限递归、跨线程访问 WebView2 报 COM 错；pywebview 反复爬这套对象图白白吃掉数百毫秒。把该引用改为
  **下划线私有 `Api._window`**（pywebview 跳过 `_` 开头属性）即从源头消除报错并大幅提速。

### Changed
- **桥就绪前禁用输入**：`pywebviewready` 之前禁用输入框/发送按钮，避免桥未就绪时点击卡住；就绪后自动开放。
- **devtools（debug）默认关闭**：默认 `debug=False`，设环境变量 `HERMES_DEBUG=1` 开发者工具
  （省开销、生产更干净）。

### Added
- **启动诊断计时探针**（仅 `HERMES_DEBUG=1` 时打印）：终端输出 `load_config` / `Api.__init__` /
  `导航开始→pywebviewready` / `get_models` / `refreshSessions` 各段耗时，便于日后排查启动性能。
  新增桥方法 `Api.client_log`。

## [1.1.1] - 2026-06-11

P8 FR-8.3：后台对话权限路由 + 停止运行中任务 + 退出优雅收尾。已 Windows 真机验证通过。
（按补丁定版：本次为 P8 收尾、沿用 1.1.x；P9 仍规划为 1.2。）

### Added
- **后台对话权限与收尾 + 停止运行中任务（P8 FR-8.3）**：
  - **后台对话权限按 cid 路由**：后台对话需要权限确认时，该会话行出现橙色脉冲点 + 全局提示，切过去能看到
    确认条处理；修复 `resolve_permission` 原固定解到当前对话的 bug（各对话 gate 的 req_id 会跨对话撞号）。
  - **停止运行中的对话**：运行中输入区以「停止」按钮替代「发送」，点击中止当前对话（回合间生效，已生成
    部分照常落库）；`AgentLoop` 加回合前检查的取消标志、`stop_conversation(cid)` API。
  - **退出更干净**：`Api.close()` 先优雅停所有对话 worker（解除权限等待、join 带超时），再关 MCP/存储；
    带运行中或等权限的任务关窗也不卡死。
  - 不做并发上限（本轮范围外）。`tests/test_conversation.py` 22/22。

### Fixed
- **后台对话等待权限时会话行误显紫点而非橙点**：触发权限会同时给该行加 `awaiting`（橙）与 `unread`（紫），
  未读规则选择器优先级更高盖掉了橙色；未读规则加 `:not(.awaiting)`，让等待权限的橙点优先。

## [1.1.0] - 2026-06-11

P8 并发对话与后台运行（FR-8.1 + FR-8.2）+ 启动性能优化（前端依赖本地化 + mermaid 懒加载）。
已 Windows 真机验证通过。

### Added
- **并发对话与后台运行（P8 FR-8.2）**：任务没跑完也能开新对话/切会话，原对话
  **后台继续跑**、切回可看到流式续看。后端每对话一条 worker 线程 + 串行队列、`send_message` 非阻塞、
  事件带 `cid` 且 evaluate_js 串行化、活动对话注册表 + `switch_conversation`（切回后台运行时不重载）；
  前端按 cid 分独立视图（离屏渲染）、放开 streaming 全局封锁、会话行运行中/未读标记。

### Changed
- **启动性能：前端依赖本地化 + mermaid 懒加载**。原来每次启动都从 cdnjs 联网拉 marked/highlight.js/
  mermaid（mermaid 一个就约 3MB），网络慢就"等半天"。现把三者内置到 `web/vendor/` 本地加载——
  **启动不再联网、可离线、打包 exe 也自带**；mermaid 改为仅在出现 ```mermaid 代码块时才动态加载，
  平时启动连这 3MB 都不碰。
- **抽出 `Conversation` 运行时（P8 FR-8.1，内部重构、对外行为不变）**：把每对话私有状态
  （session_id/history/workspace/registry/gate/active_model）与逻辑从 `Api` 单例搬进独立
  `bridge/conversation.py`（`Conversation` + 共享资源/账本 `Resources`）；`Api` 退化为对话管理器、
  公开方法转发到当前活动对话。为并发对话（FR-8.2）铺地基。新增 `tests/test_conversation.py`
  验状态隔离（最终 18/18）；全回归 13 套全绿。

## [1.0.0] - 2026-06-10

首个正式版本：打包成**免 Python 环境、双击即用**的 Windows exe（onedir）。
已在 Windows 真机完成构建 + 运行验证。

### Added
- **打包成 Windows exe（P7，onedir）**：frozen-aware 路径
  （`paths.py`：只读资源进 exe、config/.env/data 放 exe 旁、首次运行释放默认 config）；
  `hermes-dev.spec` + `build.ps1` + `docs/PACKAGING.md`。

### Fixed
- **exe 启动报 `The 'appdirs' package is required`**：pkg_resources/setuptools 的运行时依赖
  PyInstaller 没自动收全。spec 把 `pkg_resources` 加进 `collect_submodules`，并显式补
  `appdirs` / `jaraco.*` / `packaging.*` / `more_itertools` 到 `hiddenimports`。
- 发布版 spec 关闭控制台黑窗（`console=False`）。

## [0.9.5] - 2026-06-10

打开已有项目文件夹 + 对话可选中复制 + 左侧图标统一 + 顶部显示项目/会话名。已 Windows 真机验证通过。

### Added
- **打开已有项目文件夹**：左侧边栏新增文件夹图标按钮——弹系统选目录框，以选中的
  已有项目起一个新会话，工作区绑定到该目录；Agent 的读写/搜索/shell 直接在真实项目里干活。
  绑定按会话持久化（sessions 表新增 `workspace` 列，旧库自动迁移补列），重进该会话回到同一项目；
  默认空白会话行为不变。删除会话只删记录、**绝不删你的项目文件夹**。
  （已有 hermes.md 则沿用，没有则触发自动生成。）

### Changed
- 左侧「新会话 / 打开项目」改为**统一尺寸的简洁线性图标**（告别文字+emoji 混搭）。
- **顶部标题动态显示**：打开的真实项目→文件夹名；空白会话→会话标题（重命名实时更新）；
  否则回退 `Hermes Dev`。

### Fixed
- 对话内容现在可**选中复制**（显式开启 `user-select: text`，覆盖 webview 默认；可点击的工具块/
  思考块标题仍不可选）。

## [0.9.4] - 2026-06-10

长期记忆只存"跨项目通用"事实（项目专属进 hermes.md，避免跨项目干扰）+ 修复 forget 后被自动
抽取学回来（忘记墓碑）+ forget 支持按关键词删除。已 Windows 真机验证通过。

### Fixed
- **forget 后又被自动学回来**：以前 `forget` 删掉记忆后，离开会话的自动抽取会从"你说过我叫X"
  的对话里把它重新提取、再次记入——表现为"忘记后切换对话仍记得"。现在 `forget` 会给被删内容
  打"忘记墓碑"，自动抽取不再重新记同一内容（显式 `remember` 仍可再记并解除墓碑）。
- `forget` 工具支持按 **query 关键词删除所有匹配记忆**（不止按单个 id），用户说"忘记我的名字"
  能一次清干净，避免多条残留。

### Changed
- **长期记忆只存"跨项目通用"的事实**（避免跨项目互相干扰）：自动抽取与 `remember` 工具的
  口径收紧为只记用户身份/称呼、长期偏好/工作习惯、反复强调的要求、技能能力倾向；
  **项目专属内容（某项目的目标/架构/技术栈/决定/约定等）不再进全局记忆，应写进该项目的
  hermes.md**。类别 `project` → `skill`（旧库里的 project 条目按"事实"显示，不自动迁移）。
  对标 Claude Code 的"全局 vs 项目"分层。改动集中在抽取提示词、`remember` 描述、system_prompt。

## [0.9.3] - 2026-06-10

工作区按会话隔离 + 缺 hermes.md 自动生成 + UI 精修（Linear/Vercel）+ 两处 UI 修复。
已 Windows 真机验证通过。

### Added
- **工作区按会话隔离**：每个会话用独立文件夹 `data/workspaces/<会话id>/`，工具（读写/shell/
  截图）、右侧面板、项目规范都限定其中——不同项目互不污染，hermes-dev 自身源码也不再被扫到。
  显式设 `agent.workspace` 则关闭隔离、固定用它（向后兼容）。新会话切换工作区时面板自动刷新、
  顶部显示当前工作区路径。配置 `agent.per_session_workspace` / `workspaces_root`。
- **缺 hermes.md 时自动生成**（去掉了之前的按钮）：会话工作区有项目内容但没有 `hermes.md` 时，
  后台据「全局开发标准 + **本会话项目摘要**」自动提炼生成一份**本项目专属**的 hermes.md
  （目录结构 + README/依赖清单等关键文件，带上限；只写本项目相关、不卷入无关内容），
  生成后弹提示并刷新面板。配置 `agent.auto_conventions`。新模块 `conventions.py`（纯逻辑可单测）。

### Fixed
- 切换会话后右侧**预览区残留上个项目内容**：工作区变化时自动清空预览、回到空态。
- **发送按钮高度**与输入框对齐（撑满行高，比例更协调）。

### Changed
- **UI 精修（Linear/Vercel 精致暗色）**：按钮分级（发送=填充强调色主按钮+轻投影+悬停微抬，
  其余=ghost 悬停淡底，图标按钮方形低调）、平滑微交互（悬停淡染、按下回弹）、柔和焦点辉光
  （仅键盘焦点显示、不残留）、顶栏/输入区克制投影、气泡与卡片统一稍大圆角+细投影。纯样式，
  不改结构与功能。

## [0.9.2] - 2026-06-10

思考过程反馈(T1+T2) + 会话导航索引 + 项目规范自动加载(hermes.md) + system_prompt 完善 +
滚动条主题色。已 Windows 真机验证通过。

### Added
- **思考过程反馈**，消除「长时间空白等待后回复突然弹出」的突兀感：
  - T1：发送后**立即**出现「思考中…」动画指示器 + **已用时长**计时；首个内容到达即替换，
    工具结束/压缩/视觉等静默间隙也会重新显示，全程有反馈。
  - T2：把模型的**推理过程实时流入淡色可折叠「💭 思考过程」块**——provider 接 OpenAI
    `reasoning_content` 与 Anthropic `thinking_delta`（端点不产出则不显示、不报错，T1 兜底）；
    思考内容仅展示，不计入答案、不持久化。答案/工具到来后思考块自动折叠。
- **会话导航索引**（右缘迷你刻度条）：每条用户消息对应一个刻度，悬停显示该条文字、
  点击平滑跳转到对话里对应位置并短暂高亮；长会话往前翻查更方便。纯前端，
  发送/加载历史/新会话时自动刷新。
- **项目规范自动加载**（类似 Claude Code 的 CLAUDE.md）：工作区根目录若有 `hermes.md`，
  其内容作为「[项目规范]」自动注入 system_prompt，开发时遵守、与通用规范冲突时以它为准；
  每次发消息读最新（改了即生效）。文件名/开关由 `agent.conventions_file`（默认 `hermes.md`，
  空字符串关闭）控制。读取限工作区内、20000 字符上限。`workspace.read_conventions` 可单测。

### Changed
- 完善全局 `system_prompt`：参考全局开发标准补了「开发规范」段（先读后改 / 分段推进 /
  最小改动 / 贴合风格 / 重视可测与质量 / 如实报告 / 沟通简洁 / 危险操作先确认），
  并声明工作区 `hermes.md` 优先。
- 滚动条改为融入暗色主题（轨道透明、滑块用边框色、悬停变亮），不再是显眼的白色；
  工作区 HTML 预览的 iframe 内也注入同款滚动条样式（注入进 `<head>`，不触发 quirks 模式）。

## [0.9.1] - 2026-06-10

右侧工作区文件预览面板 + 可视化输出增强（SVG/Mermaid/HTML）+ max_tokens 截断修复 +
默认模型改 ark-kimi、各模型 max_tokens 调大。已 Windows 真机验证通过。

### Added
- **右侧工作区文件预览面板**（只读）：第三列展示 Agent 工作区文件树，点文件预览——
  代码/文本（高亮）、图片、Markdown；**HTML 用 sandbox iframe 直接渲染**（看 mockup 原型）+
  「在浏览器打开」。Agent 每轮跑完自动刷新（新生成的文件即时出现）；可折叠（状态记 localStorage）。
  后端只读 API `get_workspace_tree` / `read_workspace_file` / `open_workspace_file`，
  路径强制限制在工作区内（越界拒绝）、文件大小上限、跳过 .git/__pycache__/node_modules 等。
- 对话内可视化输出：
  - **Mermaid 渲染**：```mermaid 代码块会被渲染成流程图/时序图/状态图（前端引入 mermaid，
    暗色主题；流式途中不渲染、气泡定稿与加载历史时才渲染；失败优雅降级为代码块；离线降级）。
  - **内联 SVG**：模型直接输出的 `<svg>` 经 marked 当 HTML 渲染为矢量图（加了显示样式）。
  - system_prompt 增「可视化输出」指引：UI 稿用 SVG、流程/交互图用 mermaid、高保真原型用
    write_file 写自包含 .html。让模型主动产出可直接渲染的设计稿。

### Changed
- 代码高亮跳过 `language-mermaid` 块（交给 mermaid 渲染）。
- 默认模型改为 `ark-kimi`（kimi-k2.6，原生视觉），启动即用免切换。
- `ark-kimi` 的 `max_tokens` 4096 → 16384（写高保真 HTML/长代码不再被截断）。

### Fixed
- **输出被 max_tokens 截断时的工具调用死循环**：以前模型输出撞上 max_tokens 上限被截断时，
  其 `tool_use` 入参（如 write_file 的 content）残缺，agent 循环却照常执行 → 写出 0kb 空文件 →
  模型见空又重试 → 无限循环。现在循环检测到 `stop_reason in (max_tokens, length)` 即记下已生成
  文本、明确报错并停止，不执行残缺工具调用。OpenAI 适配也修正了「截断(length)被误报成 tool_use」。

## [0.9.0] - 2026-06-09

P6.4 MCP 工具接入，已 Windows 真机验证通过。

### Added
- **MCP 工具接入（P6.4）**：hermes-dev 作为 MCP 客户端连接外部 stdio server，把其工具
  自动接进 Agent 工具循环（工具名形如 `服务名__工具名`）。详见 ADR-0013。
  - 新包 `mcp_client/`：`McpManager`（常驻后台 asyncio loop + 每 server 常驻 `_serve` 协程，
    同步内核安全调用异步 SDK）、`McpTool`（适配器，结果转 text/image 块）。
  - 外部工具默认「危险」逐次过权限 gate；server 配 `trust:true` 可免确认。
  - 故障隔离：坏 server 跳过、调用失败回灌模型，绝不拖垮启动与其它 server。
  - 配置 `mcp`（`enabled`/`connect_timeout`/`call_timeout`/`servers{command,args,env,cwd,trust}`），
    默认关闭。新增依赖 `mcp>=1.2`（惰性导入：未装且未启用时应用照常）。
  - `scripts/mcp_echo_server.py`：零 Node 的最小 stdio MCP server（echo/add），用于端到端验证。
- `tests/test_p6_mcp.py` 12/12。

### Changed
- `build_registry` 新增 `mcp_tools` 入参；非空时把 MCP 工具一并注册。
- `Api` 新增 `close()`（关 MCP 子进程 + 存储连接）；`app.py` 在窗口关闭后调用。

## [0.8.0] - 2026-06-09

P6.3 长期记忆（跨会话），已 Windows 真机验证通过。

### Added
- **长期记忆（P6.3）**：跨会话、跨重启持久的事实/偏好/项目背景，独立于单会话历史。
  - 独立 SQLite 库 `data/memory.db`（`store/memory.py` 的 `MemoryStore`），与会话库解耦。
  - 模型工具 `remember` / `recall` / `forget`（非危险，不过权限 gate，UI 作为工具块可见）。
  - 自动抽取：离开会话（新建/切换）时后台把刚结束的对话抽成记忆条目、去重入库，
    完成弹出「🧠 已记入长期记忆」toast。
  - 回忆注入：每次发消息前把记忆按预算拼进 system，使**全新会话也「记得」**旧事实；
    注入后再走 P6.2 上下文压缩。
  - 配置 `memory`（`enabled` / `auto_capture` / `max_inject` / `max_inject_chars` /
    `min_messages_to_capture` / `db_path`）。ADR-0012。
- 纯逻辑模块 `longmem.py`（注入块/对话转录/抽取提示/输出解析，可单测、不碰网络）。
- `tests/test_p6_memory.py` 8/8。

### Changed
- `build_registry` 新增 `memory_store` 入参；非 None 时注册记忆工具。
- bridge `_budget` 改用 `_effective_system()`（基础 system + 注入的长期记忆块）。

## [0.7.0] - 2026-06-09

P5 Agent 截屏工具 + 图像外置 blob 存储 + 查漏补缺（重命名/temperature/清理），
已 Windows 真机验证通过。

### 已知限制
- `agent.screenshot: false` 仅移除专用 `take_screenshot` 工具；模型仍可能绕道
  `run_powershell` 执行截屏命令（会弹其权限确认）。彻底禁用需配合限制 shell——留作后续。

### Added
- 图像外置 blob 存储（P5.1）：消息里的图片不再以 base64 全量入库，改为写到 `data/blobs/`
  （按 sha256 去重）、DB 只存引用，读出时还原；删会话回收孤儿 blob。解决长会话（尤其 Agent
  自动截图）DB 膨胀。`storage.externalize_images` 开关（默认开）。ADR-0011。
- Agent 主动截屏工具 `take_screenshot`：模型可在工具循环里主动截屏看屏（Pillow ImageGrab），
  复用权限 gate（危险操作需授权）+ 视觉模型识图链路。`config.agent.screenshot` 总开关。
- 工具框架支持返回富内容块：`ToolOutput(text, blocks)`，截图作为 image 块注入。
- 前端工具块内展示截图缩略图。
- 新增依赖 `pillow`。ADR-0010（Agent 截屏工具，含「图片走 tool_result 同消息并列块」结论）。

- 会话重命名前端入口：会话项悬停出现 ✎，点击内联编辑标题（Enter/失焦提交、Esc 取消），
  接通既有 `rename_session`（此前后端已实现、前端缺入口）。
- 模型档案支持 `temperature`（可选，采样温度）：`ModelConfig.temperature` 贯通到
  Anthropic / OpenAI 两个 provider；不填则用 provider 默认。

### Changed
- 工具执行 `_exec_tool` 返回扩展为 `(text, ok, blocks)`；返回图片的工具其 image 块作为并列块
  追加到本轮 tool_result 所在的 user 消息（部分端点不解析 tool_result 内嵌图片）。
- P5 范围重定：放弃「全局热键/区域选择手动截图 UI」（与系统 Win+Shift+S 重复），改为
  Agent 主动截屏工具。

### Removed
- 移除开发期诊断输出 `bridge._debug_attachments`（每条消息往 stderr 打附件摘要），生产无需。

## [0.6.0] - 2026-06-09

上下文预算压缩 + 火山引擎方舟集合模型，均已 Windows 真机验证通过。

### Added
- 上下文 token 预算与压缩：长会话喂给模型前若超预算，保留最近若干回合、把更早内容
  压成摘要塞进 system，控制上下文溢出与成本；持久化历史不受影响（DB 始终完整）。
- `context.py`（启发式 token 估算 + `compress`，无新依赖）；`ContextConfig` 配置段
  （`enabled` / `max_input_tokens` / `keep_recent_turns`）；config.yaml 加 `context` 段。
- 前端 `context_compressed` 事件 + 「🗜 已压缩」提示块。
- ADR-0009（上下文预算与压缩）。
- 火山引擎方舟（Volces Ark）coding 端点 5 个模型档案（兼容 Anthropic 协议、共用
  `ARK_API_KEY`）：`ark-doubao` / `ark-glm` / `ark-minimax` / `ark-kimi` / `ark-deepseek`。
  Linux 真连通验证通过，含 tool-use 完整往返；`.env.example` 加 `ARK_API_KEY` 占位。

### Changed
- bridge 写回逻辑：按「压缩后喂入条数」从 loop 结果切出本轮新增消息，extend 回完整
  `history` 并落库（压缩只作用于喂模型的副本，不污染持久化历史）。

## [0.5.0] - 2026-06-09

会话历史持久化已在 Windows 真机验证通过（重启恢复、多会话切换、删除、自动标题）。

### Added
- 会话历史持久化（SQLite）：对话自动存盘、重启恢复；左侧栏多会话（新建/切换/删除）。
- `store/db.py`（`Store` + `make_title`）；`StorageConfig` 配置段（默认 `data/hermes.db`）。
- bridge 会话方法：`list_sessions`/`load_session`/`delete_session`/`rename_session`。
- 前端左侧会话栏 + 历史消息重渲染；`session_created` 事件。
- ADR-0008（会话持久化 SQLite）。

### Changed
- 前端布局改为「左侧会话栏 + 右侧对话」；`new_session` 改为开启新会话（首条消息时落库）。

## [0.4.0] - 2026-06-09

视觉预处理回退机制落地（默认关闭）。机制真机验证通过；但 MiniMax `coding_plan/vlm`
需编程套餐凭证，普通开放平台 key 调不通，图像识别端到端暂搁置（见 ADR-0007 已知限制）。

### Added
- 视觉预处理回退：主模型不支持视觉时，用 VL 端点（`MiniMax-VL-01`）把图转成文字
  描述再喂给主模型，让 M2.7 等纯文本模型也能"看图"。端点/key/开关全可配。
- `multimodal/vision.py`；`ModelConfig.vision` 声明；`vision_fallback` 配置段
  （**默认 enabled: false**）；`AppConfig.resolve_api_key_env`。
- 前端"🔍 视觉预处理"进度块（`vision_start`/`vision_done` 事件）。
- ADR-0007（视觉预处理回退，含已知凭证限制）。

### Changed
- 支持视觉的模型（Claude/gpt-4o）标 `vision: true` 直发原图，其余在开启回退时走预处理。

### Removed
- `minimax-vl` 档案（M2.5 经 chat 端点同样不识图，视觉统一走 VL 预处理路径）。

## [0.3.0] - 2026-06-09

文档附件（PDF/代码/txt）已在 Windows 真机验证通过；图像链路经诊断确认正确，
视觉识别需用支持视觉的模型（Claude / gpt-4o），MiniMax 当前接口不支持图像。

### Added
- 多模态输入：图片（粘贴/拖拽/选文件）+ 文档（PDF 经 pypdf 抽文本、文本/代码）。
- `multimodal/ingest.py`：附件归一为统一内容块；图片大小/文档字符/附件数限额。
- 配置 `multimodal` 段；依赖加 `pypdf`。
- 前端附件预览条、拖拽整窗放下、用户消息内附件渲染。
- `minimax-vl` 档案（M2.5 走 OpenAI 兼容端点）；`bridge._debug_attachments` 诊断输出。
- ADR-0006（多模态输入：统一内容块；PDF 抽文本而非渲染图片）。

### Changed
- `send_message` 增加 `attachments` 参数；OpenAI provider 适配 image_url。

## [0.2.0] - 2026-06-09

已在 Windows 真机验证通过（工具调用 + 权限确认 + PowerShell 执行；MiniMax-M2.7 tool-use 可用）。

### Added
- 工具系统：`read_file` / `write_file` / `edit_file` / `list_dir` /
  `grep_search` / `glob_search` / `run_powershell`，统一 schema + 工作区路径沙箱。
- Agent 主循环（plan→act→observe），`max_steps` 防死循环。
- 权限 gate：危险操作逐次确认 + 本会话「全部允许」。
- 前端：工具调用折叠块、结果展示、权限确认条。
- 配置 `agent` 段：`workspace` / `max_steps` / `shell` / `shell_timeout`。
- ADR-0005（工具系统、Agent 循环与权限 gate）。

### Changed
- Provider 接口扩展 tool-use：`StreamEvent` 加 `tool_use`、`done` 带 `stop_reason`，
  `stream_chat` 增 `tools` 参数，`Message.content` 支持 content blocks。
- `send_message` 由单轮对话改为驱动 Agent 工具循环。

## [0.1.0] - 2026-06-08

已在 Windows 真机验证通过（pywebview 窗口 + MiniMax 流式对话）。

### Added
- P0 项目脚手架：`pyproject.toml`、`config.yaml`、`.env.example`、`.gitignore`。
- 配置系统：加载 `config.yaml` 模型档案 + `.env` 密钥（pydantic 校验）。
- 模型适配层：统一 `BaseProvider` 接口 + Anthropic / OpenAI 兼容两个实现 + 工厂。
- JS↔Python 桥与流式事件推送。
- pywebview 桌面窗口入口。
- P1 前端：暗色 UI、markdown + 代码高亮流式渲染、多模型切换、新会话。
- 文档体系：PRD、ARCHITECTURE、CONVENTIONS、DEVLOG、ADR 0001–0004。
- MiniMax 模型档案（Anthropic 兼容接口，`MiniMax-M2.7`）。

[1.6.0]: #
[1.5.0]: #
[1.4.0]: #
[1.3.0]: #
[1.2.0]: #
[1.1.2]: #
[1.1.1]: #
[1.1.0]: #
[1.0.0]: #
[0.5.0]: #
[0.4.0]: #
[0.3.0]: #
[0.2.0]: #
[0.1.0]: #
