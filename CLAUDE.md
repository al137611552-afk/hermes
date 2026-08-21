# hermes-dev 项目标准

继承全局 `~/.claude/CLAUDE.md`；以下是本项目特有约定。

## 一句话
Windows 桌面多模态编程 Agent：pywebview 外壳 + Web 前端 + Python 内核。
三栏 UI：会话栏 / 对话 / 工作区文件预览。

## 常用命令
- 安装（含依赖）：`pip install -e .`
- 启动应用：`python -m agentcore.app`（Windows 上 `python` 不行就用 `py -m agentcore.app`；
  不要依赖 `hermes-dev` 入口脚本，它常没进 PATH）
- 跑全部测试：`for t in tests/test_*.py; do python "$t"; done`（每个是独立 runner，不依赖 pytest）。
  其中 `test_golden.py` = **决策内核回归门**（块F，ADR 0014/0016）：`tests/golden/` 冻结了
  Need→Decision/Evaluation 各映射的行为基线，**任何改决策逻辑（block A–E 函数、Learning 块G）必须先过它**；
  改既有期望=有意行为变更，需在 cases.py 注明。门自带活性自检（劣化必红）。
- 跑前端纯逻辑测试：`node --test tests/web/*.test.js`（node:test，零依赖；**前端纯逻辑统一放
  `web/pure.js`**——可脱离 DOM、Node 可测，别埋进 app.js 的 DOM 渲染函数里。「全回归」= Python + 前端两条都绿）
- 打包分发：见下「打包」。

## 项目结构速览
```
src/agentcore/
  app.py          入口（起 pywebview 窗口、注入 Api，关窗后 api.close()）
  config.py       config.yaml + .env 加载（pydantic）
  bridge/api.py   暴露给前端的 JS API；串起 provider/agent/store/memory/mcp/workspace
  providers/      模型适配：base / anthropic_p / openai_p（统一 StreamEvent + tool-use）
  agent/          loop.py（plan→act→observe）+ gate.py（危险操作权限确认）
  tools/          read/write/edit/list/grep/glob/run_powershell/screenshot/memory
  multimodal/     ingest（图片/PDF/文本归一）+ vision（视觉回退，默认关，已被原生视觉取代）
  store/          db.py(SQLite 会话) + blobs.py(图片外置) + memory.py(长期记忆)
  longmem.py      长期记忆纯逻辑（注入/抽取/解析）
  context.py      上下文 token 预算与压缩
  mcp_client/     MCP 客户端（manager 异步桥 + tool 适配），仅 stdio + tools
  workspace.py    右侧面板：工作区文件树 + 只读预览（路径限工作区内）
  skills.py       技能包（Agent Skills 规范）：SKILL.md 解析/发现/渐进披露拼块
  skillscan.py    技能安全扫描（启发式，clean/review/warn 三档）
  skillhub.py     技能市场：marketplace.json 解析 / GitHub zip 下载 / 安装 / 更新台账
web/              index.html / app.js / style.css（CDN: marked/hljs/mermaid）
docs/             PRD / DEVLOG / ARCHITECTURE / CONVENTIONS / adr/NNNN-*
tests/            test_*.py（独立 runner）
scripts/          check_compression.py / mcp_echo_server.py
config.yaml       模型档案 + 各功能开关        .env  密钥（gitignore）
```

## 环境与验证（重要）
- **开发环境是 Linux 无显示**，跑不了 GUI；**用户在 Windows 真机验证**。
- 我这边：纯逻辑/后端就地自检（必要时用 venv 装 SDK 跑端到端，如 mcp echo server）；
  GUI、真实模型调用、平台相关的，整理成**验证清单**交用户在 Windows 验。
- 阶段节奏：更新 PRD → 实现 → 全回归全绿 → 用户 Windows 验 → 通过后更新 DEVLOG/CHANGELOG 并**定版** → 下一阶段。

## 代码与测试
- 代码规范详见 `docs/CONVENTIONS.md`。要点：纯逻辑与 IO 分离便于单测；工具遵循 MCP 三要素
  `name/description/input_schema`；危险操作（写文件/执行命令/外部 MCP 工具）默认过权限 gate。
- 新功能配同风格自检（临时目录/mock，不碰网络、不连真 server）；**改完跑全部测试，全绿才算完成**。

## 定版（仅 Windows 验证通过后）
同步改：`pyproject.toml` version + **`src/agentcore/__init__.py` 的 `__version__`** +
`CHANGELOG.md`（[Unreleased]→版本号+日期）+ `DEVLOG.md`（状态改“已验证通过”）+
`PRD.md`（对应 FR 状态）。CHANGELOG 遵循 Keep a Changelog + SemVer。
> `__version__` 曾漂了四个版本没人发现（2026-08-14）：`current_version()` 优先读
> importlib.metadata，正式安装/打包产物里都读得到正确值，**只有 metadata 查不到时才回退到它**
> （开发机 editable 安装就是这种情况）。`test_updater` 已加闸钉住两处一致。

## 配置与密钥
- 模型档案在 `config.yaml`；密钥只在 `.env`，**绝不写进代码或文档**。
- **开箱不预设 provider / 模型**（`DEFAULT_PROVIDERS={}`、`active_model:""`，v3.56.0 起）：
  在「设置面板 → Provider」选一家填 key 即用。**别在 config/`.env`/打包模板里预填某一家**——
  预设一个用户没订阅的 provider 只会制造"这模型我没配过怎么会在这"的误会（2026-08-12 清掉过一次）。
- `max_tokens` 按各模型实际上限设，别设超（会 API 报错）：方舟系/minimax/gpt-4o≈16384、
  Claude 4 可 32000、**deepseek 标准接口上限 8192**。

## 打包分发（给用户的 zip）
- **包含 `.env`**（用户要求，省得手动复制）。解压后必须是**单层** `hermes-dev/`。
- 排除：`__pycache__`、`*.pyc`、`*.db`、`data/`、`.git`、`build/dist`、`*.egg-info`、旧占位 zip。
- 打包后提醒：含真实 key，**别外发/上传公开处**；新增 Python 依赖要提醒用户重跑 `pip install -e .`。

## 已知坑（gotchas）
- Windows `.env` 易被记事本加 `.txt` 后缀或 BOM；用 `"K=V" | Set-Content -Path .env -Encoding ascii` 生成干净文件。
- 模型输出撞 `max_tokens` 会截断工具入参；loop 已检测 `stop_reason in (max_tokens,length)` 并优雅停止
  （不执行残缺工具、提示调高或分步），不会再死循环。
- MCP：默认关；开启需本机有 `npx`/`uvx`；`mcp.enabled=false` 时不依赖 mcp SDK。
- 前端 marked/hljs/mermaid **已本地内置在 `web/vendor/`**（不再走 CDN）：启动不联网、可离线、exe 也自带。
  mermaid（约 3MB）改为**懒加载**（`app.js` 的 `ensureMermaid()`，仅出现 ```mermaid 块时才动态加载）。
  新增/升级这些库时替换 `web/vendor/` 下文件即可；`web/` 整目录已进打包 spec，无需改打包。
- 工作区预览面板根目录 = `config.agent.workspace`（默认项目根）；想让 Agent 在别处干活就改它。
- 本机（开发用 Linux）为跑测装过 anthropic/openai/mcp SDK：`pip install --break-system-packages`。
- **WebView2 死锁坑（v3.51 评审踩过）**：**别在 pywebview 的 `js_api` 方法里同步调 `window.evaluate_js` 推流式事件**——WebView2 下 js_api 处理函数返回前 JS 执行结果无法回传，首个 `evaluate_js` 就死锁：方法永不返回、前端 `await` 永远 pending（症状＝按钮一直转、事件一个不出）。**规避**：凡是"边跑边 emit"的长任务都走**后台 worker 线程**（照 `conversation.enqueue`/`_worker_loop`、`run_design_review` 的模式），js_api 立即返回、事件由线程 emit、前端靠事件（非 await 返回值）驱动渲染与收尾。**Chromium/Playwright 复现不出**（WebView2 专属）。
- **WebView2 滚动坑（v3.37 踩过）**：对话区 `.chat` 是滚动容器，某些 CSS 会让 WebView2 在内容**异步重排**时把 `scrollHeight` 暂时算塌（→ 滚轮跳回顶部、几轮后自愈），**Chromium/Playwright 复现不出**（WebView2 专属）。已知触发：① 给元素只设 `overflow-x:auto`（`overflow-y` 连带变 `auto`、成滚动容器）；② `<hr>` 用 `border:none;border-top` 重构盒子；③ 嵌套列表加 `margin`（嵌套 margin 合并）；④ 给 `table` 加 `display:block`。**规避**：宽表格用外层 `.table-wrap` div 滚动（别动 table 的 display）；列表只用 `padding` 缩进别用 margin；hr 只改色别重构；`.chat` 已加 `overflow-anchor:none`。改对话区 CSS 后**务必真机滚长对话验**，别只信 Chromium 截图。

- **`hidden` 属性会被作者样式的 `display:` 盖掉（已踩三次：`.ws-reopen` / `#send` / v3.63 的 `.trace-bar`）**：
  给元素写了 `display:flex/inline-flex` 就**必须**补一条 `.x[hidden] { display: none; }`，否则 `el.hidden=true`
  但屏幕上照常显示（症状：一启动就常驻一条本不该出现的状态条）。**自检要量渲染结果**
  （`offsetWidth || offsetHeight || getClientRects().length`）——断言 `el.hidden` 属性会一路全绿。
- **新浮层必须入浮层栈 `pushLayer/popLayer`（v3.63 踩过）**：栈在**捕获阶段**统一处理 Esc（只关最上层 +
  `stopPropagation`）与 Tab 环绕。不入栈＝Esc 被下层吃掉（症状：Esc 关了设置面板，你的弹窗还杵着），
  自己在 overlay 上挂的 Esc 监听**永远收不到事件**。同理别在浮层里另写一份 Esc/Tab 处理。
- **别蹭老组件的 CSS 类（v3.63 踩过）**：`style.css` 是一个按功能顺序追加的大文件，**后写的同特异性规则永远赢**。
  新面板蹭 `.settings-body` → 被它后面的 `display:flex` 盖掉，整块压成一列竖排字；`.trace-modal` 的
  `height:auto` 被后面的 `.settings-modal` 固定高度盖掉。**要么完全自带样式，要么用 `.a.b` 提特异性**。
  同源提醒：新 UI 若要复用老状态机（如 `composerState` 的运行态），也得把自己接进去——评审跑在
  后台线程不进 `streaming`，结果停止键一直不出现。
- **加新工具时，先把提示词里指向旧做法的路标一起改**（v3.63 踩过）：`request_handoff` 装好了，但
  `config.yaml` 系统提示词与 `delegate.py` 的 researcher 指令里「遇登录墙先用 ask_user」还在，模型照旧路走。
  **提示词里的具体指令压得过新工具的 description**，改工具集时 `grep` 一遍旧工具名。
- **shell 前台读输出必须用二进制 `read1()`，别"清理"回 `text=True`**（2026-08-11 踩过）：
  `TextIOWrapper.read(4096)` **会阻塞到读满 4096 字符或 EOF**——于是"实时流输出"对绝大多数命令不实时、
  停在交互提示上的命令也看不见提示。现在是二进制管道 + `_StreamDecoder`（增量 utf-8 + 换行归一）。
  `test_encoding_guard` 只管 `text=True` 分支，改回去它**不会报**。相关纪律：凡"边跑边 X"的功能，
  测试要钉住**到达时间**，只断言"收到了"会让"结束时一次性吐出"也全绿。
- **交互式命令：不做全局 auto-yes**。`hardened_env()` 里会改语义的开关（`CI`/`GIT_SSH_COMMAND`）用
  `setdefault` 尊重用户；**不设 `TERM=dumb`**（git 会 "press RETURN"，多一个挂死点）；
  **不注入 `$ConfirmPreference='None'`**（等于替用户对 `Remove-Item -Recurse` 这类点了"是"）。
  提示识别（`looks_waiting_input`）只在「还活着 + 最后一行像提示 + 静止≥5s」三条同时成立时才下结论。
- **只读命令不弹权限确认是设计如此（别当 bug 查）**：智能确认分级（`auto_approve_safe` 默认开，v3.44 起）
  会自动放行 `dir`/`Get-Date`/`whoami`/`git status`/`pytest` 等只读命令（白名单在 `permissions.py` 的 `_SAFE_LEADING`）。
  免确认共三种原因（命中 allow 规则 / 本会话「全部允许」/ 只读白名单），**UI 已在执行行标注「（免确认：原因）」**
  （`gate.explain()`，v3.58 加的）——先看那句再排查。写测试用例时挑会弹确认的命令（`python --version`/`node -v`/`ipconfig`），
  别挑白名单里的（2026-08-11 因此误报过两次"漏了权限确认"）。
  **动 `command_is_safe` / 加白名单命令前，先去 `tests/test_permissions.py` 的对抗性回归加一条用例**
  （ADR 0024）：判据＝"这命令能不能拿另一个命令当参数执行"（`env rm -rf /` 就是这么漏的）。
  这是**免确认**入口，判错方向不对称——多弹一次只是麻烦，误放行一条毁灭性命令是事故。
- **技能包（FR-13.S）**：格式对齐 [Agent Skills 公共规范](https://agentskills.io/specification)，**别自造 frontmatter 字段**（要加放 `metadata:`），否则丢生态兼容。**`allowed-tools` 在 hermes 里只展示、不免确认**——这是刻意偏离规范的安全立场（技能是攻击面），改动前先读 ADR-0014。新增内置技能放 `skills/<name>/`，`SKILL.md` 的 `name` 必须与目录名一致（内置技能走 `strict=True` 校验，有测试守）。
- **技能命名：接收宽容、产出严格（别改回严格）**：规范要求 name 全小写连字符且与目录名一致，但**生态里普遍不遵守，连 Anthropic 官方 `plugin-dev` 插件的 7 个技能都写成 `Agent Development`**。读第三方走 `normalize_name` 归一化、不一致时回退用目录名；只有我们自己的技能才 `strict=True`。**看到"这解析也太宽松了"想收紧之前先读 ADR-0015 §4** ——收紧＝装不了绝大多数真实技能。
- **技能安全扫描（FR-13.S2）**：`skillscan.py` 是**启发式**的，文案纪律＝**一律不说"安全"，只说"未发现可疑信号"**（有测试钉死 `SKILL_GRADES` 文案不含"安全"）。改规则时注意两条真跑教训：①提示注入模式只在 `.md` 里算 warn（脚本里那是数据，否则误伤安全工具的测试语料）；②HTML 注释阈值 600 字符（200 会把正当文档元数据全报出来）。分级只决定确认强度，**永远不硬拦**。
- 本机（开发用 Linux）为跑测装过 anthropic/openai/mcp SDK：`pip install --break-system-packages`。
- **WebView2 滚动坑（v3.37 踩过）**：对话区 `.chat` 是滚动容器，某些 CSS 会让 WebView2 在内容**异步重排**时把 `scrollHeight` 暂时算塌（→ 滚轮跳回顶部、几轮后自愈），**Chromium/Playwright 复现不出**（WebView2 专属）。已知触发：① 给元素只设 `overflow-x:auto`（`overflow-y` 连带变 `auto`、成滚动容器）；② `<hr>` 用 `border:none;border-top` 重构盒子；③ 嵌套列表加 `margin`（嵌套 margin 合并）；④ 给 `table` 加 `display:block`。**规避**：宽表格用外层 `.table-wrap` div 滚动（别动 table 的 display）；列表只用 `padding` 缩进别用 margin；hr 只改色别重构；`.chat` 已加 `overflow-anchor:none`。改对话区 CSS 后**务必真机滚长对话验**，别只信 Chromium 截图。
- **检索分工：搜索恒走 HTTP，浏览器只读页面（别改回"挂上浏览器就摘掉 web 工具"）**：v3.43 曾用
  `_drop_web_when_browser` 把 `web_search`/`web_fetch` 物理摘掉逼一切走浏览器，2026-08-07 实测证明这条
  是灾难——搜索引擎对自动化浏览器返回**空壳结果页**（Bing 结果块 0 个、DDG 验证码、百度滑块），
  等于砍掉唯一稳定的搜索通道。现在的结构是 `_make_browser_reader()`：搜索走 HTTP，`web_fetch` 受阻
  （`looks_blocked`）**自动升级**到浏览器读同一 URL。"不许绕路"的本意靠"模型没有换引擎重搜这个动作"保证，
  不是靠摘工具。**另**：无障碍快照**抓得到** JS 渲染内容（纯 SPA 文档站实测 19,954 字符），看到空快照
  先怀疑反爬、别怀疑渲染。
- **Firecrawl（FR-11.1d）的适用面比想象窄，别当银弹**：2026-08-20 真网实测——`search`=**2 credits**、
  `scrape`=**1 credit**，免费档 1000/月。它真正买得到增量的是 **JS 空壳**
  （`app.slack.com/help` 直读 141 字符 → 渲染 1874 字符）；**打不穿强反爬/登录墙**——知乎 403 页
  拿回来的是「你似乎来到了没有知识存在的荒原」拦截插页，**加 `proxy=stealth` 也一样**。所以：
  ①**付费源的产出必须再判一次**（`firecrawl_gain`），否则花了 credit 还把拦截页当正文喂给模型；
  ②命中登录墙**直接跳过它**（没登录态＝稳定的必然失败），只有浏览器有戏；
  ③降级阶梯是 **HTTP → Firecrawl → 浏览器**，越往后越重、越有侵入性。
  **别在批量评测里开 `always`**：一个研究型任务搜 7~8 次，一个月配额只够 60~70 个任务。
  **默认档是 `primary`（主搜）不是 `fallback`**——2026-08-20 真机反馈"全程没触发过"，
  说明 fallback 的判据在真实使用里几乎不成立，省下的配额纯属闲置。配额用尽（**只认 402，
  429 是限流不算**）自动退回免 key 链路并粘住，本进程内不再重试。
- **推理模型长考中途断线时，`thinking` 不该封锁重试**（`providers/base.py:blocks_retry`）。
  瞬时错误的重试门槛是"还没吐出**答案内容**"；旧口径写的是"yield 过任何事件"，而推理模型
  先吐 thinking，那道门当场被踩掉——**这层保护对推理模型等于不存在**。
  2026-08-20 真机：DeepSeek V4-FLASH 打开已有项目续开发，长考中
  `RemoteProtocolError: incomplete chunked read`，一断就整轮作废。
  封锁重试的只有 `text`/`tool_use`/`done`（重来会重复输出给用户），thinking 重复一段无所谓。
- **agent 型 MCP server 要开 `always_confirm`**（config 或面板「每次都问」）。它**不吃 allow 规则、
  也不吃「本会话全部允许」**——那个开关的心智模型是"这些零碎命令我都认"，为单点、可逆、
  几秒钟的操作设计；用它顺带放开一个能改一堆文件的自主 agent，粒度不对
  （2026-08-20 真机：用户点过全部允许后 Codex 全程零确认跑完一轮）。
  **deny 与毁灭性拦截仍优先**——放行档次只降不升，先例就是 `is_destructive` 那条。
  同时不给「总是允许这类」：`codex__codex` 没有 path/command 参数，`suggest_rule` 给的是
  **裸工具名**，点一次＝以后这个 agent 干什么都不问，且**会落盘、重启仍生效**。
- **委派型调用（agent 型 MCP）单独渲染成「委派卡」**：抬头给**目标**（prompt 首句）与
  **已用时**，默认展开——它要跑几分钟，收起来等于又变回黑箱；用时那个数字是"在跑"与"卡住"
  的唯一区分。是不是委派由**代码判定**（`AgentLoop._is_agentic`，看 `_takes_cwd`）随
  `tool_use` 事件下发，**别让前端按工具名猜**——名字是 server 起的，猜必然漏。
- **「停止」会取消在飞的 MCP 调用**（`Conversation.stop()` → `McpManager.cancel_all()`）。
  不接的话 agent 型 server 一次调用几分钟，按了停止仍要干等到 `call_timeout`（真机 900s）。
  取消后给的是**可读文案**「调用已被用户停止」——原样抛 `CancelledError` 会被包成
  "MCP 调用失败：CancelledError"，看着像故障而不是"你自己停的"；`McpTool.run` 里
  `ToolError` 直接放行、不再套第二层。计数时**已取消的要显式跳过**：
  `Future.cancel()` 对它仍返回 True，直接累加会虚高。
- **agent 型 MCP 调用会附一份 `git status` 的客观改动**（`mcp_client/gitwatch.py`）。
  agent 回来的是自然语言自述（"我修好了 X"），**动了哪些文件只有 git 说了算**——
  同评测那条「判分优先程序化」。取**调用前后的差集**，不是事后一把梭：工作区本来就可能是脏的，
  把用户自己的改动算到 agent 头上是"自信的错数"。`None`（不是 git 仓库/没装 git/超时）
  与 `[]`（干净）**是两件事**，混淆就会显示假结论。
- **判断"是不是 agent 型工具"看 schema 收不收 `cwd`**（`_takes_cwd`），**别用 thread key**：
  `codex__codex` 的 schema 里根本没有 threadId（只有 `codex-reply` 有），
  用它当门会让**起始那次**既不补 cwd 也不记改动（2026-08-20 端到端真跑才发现）。
- **MCP 工具的补参走 `prepare()`，且 loop 在 `gate.confirm` 之前调它**。确认条上要显示的是
  **真正会执行的参数**——`cwd` 决定 agent 型 server 在哪儿干活，看不到就等于没确认。
  补两样：续话 id（模型没给才补）、`cwd`（跟随**当前会话工作区**，`bind_workspace` 绑成副本——
  同一批工具实例被所有会话共用，直接改字段会串台）。内部标记以 `_` 开头，前端确认条不显示。
- **mcp SDK 1.x 驼峰 / 2.x 蛇形要两个都认**（`sdk_field()`）。CLAUDE.md 早记过 `inputSchema`
  那一个，但当时只改了那一个：2026-08-20 端到端真跑才发现还漏着 `isError`（于是
  **MCP 工具报错从来没被识别成错误**，错误文本被当正常结果回灌）和 `structuredContent`
  （续话 id 一直取不到）。**这类漏都是静默的**——加字段时顺手全库搜一遍驼峰名。
- **MCP 连不上先跑体检**：面板每行有「体检」按钮（保存失败会自动跑一次），命令行是
  `python scripts/diag_mcp.py [server]`，两边**共用 `mcp_client/diag.py` 同一份实现**。
  它把配置、命令解析到哪个可执行文件、PATH 里有没有多份同名、cwd/超时/权限档、
  以及真起一次握手的结果逐层摊开——真踩到的两种故障（参数写进命令框、PATH 两份同名）
  都**不在** `Connection closed` 那句话里。
- **Codex 的审批（`elicitation/create`）hermes 只能拒绝**：2026-08-20 用真会话
  （codex + DeepSeek）逐个试过 `accept`/`approved`/`content.decision=…`，它一律当拒绝，
  **没能确定"同意"该回什么值**。现在的做法是接住它、显式拒绝、把命令与出路打进工具块——
  不接就是 SDK 默认静默拒绝，用户只看到"写操作被拒"、查不到原因。
  出路：调用时给 `approval-policy="never"`，用 `sandbox` 决定它能改什么。
- **本机 codex 可以接 DeepSeek 跑真会话**（不必 ChatGPT 订阅）：`~/.codex/config.toml` 里
  `model_provider="deepseek"` + `[model_providers.deepseek] base_url="https://api.deepseek.com/v1"`、
  `wire_api="responses"`（0.148 起 `wire_api="chat"` 已被移除）。**顶层键必须写在任何
  `[table]` 之前**，否则会被并进上一张表（踩过）。有了它，Codex 相关的东西不必再等 Windows。
- **mcp 1.x 拿不到自定义通知，只能在流上加旁路**（`McpManager._tap`）。1.27.2 实测：
  不认识的通知**只 log 一句就丢**，`message_handler` 也拿不到——所以 `notification_bindings`
  （2.x 才有）不可用时，改成包一层读流、先偷看再原样转交。**只看不改**、一条不落地转交，
  否则整个连接就废了。两版都验过：1.27.2 与 2.0.0 都能出实时流。
- **权限两个开关矛盾时以「更严」为准**：`always_confirm` 压过 `trust`。真机踩到用户配里
  两个都为 true，按原先"trust 优先"的写法，"每次都问"被**静默作废**、一次确认都没弹。
  面板已改成两者互斥，代码侧是兜底（手编 config.yaml 仍可能写出矛盾），体检会把矛盾报成 BAD。
- **MCP 子进程继承 hermes 自己的环境**（`{**os.environ, **sc.env}`）。SDK 默认只给一份
  **白名单**环境，代理变量（HTTPS_PROXY…）、CODEX_HOME、区域设置全被滤掉——真机表现是
  codex 在子进程里反复 `Reconnecting… / request timed out`，而同一条命令在用户终端里好好的。
- **Codex 的沙箱在会话创建时定死**：`codex-reply` 的 schema 里没有 sandbox，
  所以**只读会话之后无法升级**——要改文件只能重开会话、丢掉全部上下文
  （真机：先只读问建议，追问让它改东西就卡死）。代价不对称，故**默认给 workspace-write**，
  `read-only` 只留给明确"只看不改"的场合。这条规则写在**工具说明里**（`sandbox_hint`），
  让模型第一次就选对——写权限有三道防线兜着（每次确认 / cwd 夹在工作区 / git 客观改动），不是裸奔。
- **cassette 指纹要折的不止工作区，还有安装目录**（`fold_app_dir`）。技能的附带资源清单
  （`list_skill_files`）返回**绝对路径**，它进 `load_skill` 的结果、进消息历史、进指纹——
  于是同一份录音换个检出路径必然 miss。**CI 从块 V3 收尾（run #2）起一直红、而本地一直绿，
  就是这个**（本地永远在同一个目录跑）。改动或重录前先想一句：**这段内容换台机器还一样吗**。
  同源的还有两处：shell `time` 的 `real 0m1.8s`（度量本机快慢，已归一化）、
  以及临时目录要折**父目录**（报错回溯、`<tmp>/eval.db` 都落在 ws 的兄弟位置）。
- **机器特有的真信息不该归一化**：`which python` 的路径、`ls -l` 的文件元数据是**真信息**，
  抹掉等于让回放门对着假输入判定。这类任务走 `replayable=False` + 写明理由
  （`fail_missing_toolchain` 就是例）。
- **贴进对话的图片会落盘到工作区** `.hermes/attachments/`（`multimodal/store.py`），
  并在消息里附上路径。**子 agent 只认文件**：Codex 的 MCP 工具 schema 里没有图片入参
  （2026-08-21 实测），CLI 是 `codex exec -i <FILE>`——所以"用户贴图 → 子 agent 看图"
  这条路上落盘是唯一通路，不是偷懒。落工作区而不是 /tmp：agent 被夹在工作区里，放外面够不着。
  文件名带时间戳（同名覆盖会让"上一张图"凭空失效）且只留安全字符（要拼进命令行）。
- **agent 型 MCP 的 `cwd` 是强制夹回工作区的**（`clamp_cwd`），不是"模型没给才补"。
  2026-08-21 真机：Codex 在别的目录里建了整个项目、还从那儿起了服务，工作区一个文件都没有，
  用户翻进程列表才发现。**"它只可能在这个工作区里干活"这条保证，比让模型自由选目录值钱得多**；
  工作区内的子目录仍照用，区外的改回根**并说明**（静默纠偏＝又一次沉默）。
  配套两条也别删：结果回显 `[工作目录]`，以及**"工作区无改动"要说出来**——
  agent 自述"已创建 xxx"而 git 毫无改动，是最值得当场看见的矛盾；
  但测不了（不是 git 仓库）要保持安静，别把"没测"说成"没改"。
- **别在 hermes 自己的目录里派活**：面板模板会把"当前工作区"填进 `cwd`，而没打开项目时
  那是 `data/workspaces/_scratch`——agent 会全干在那儿，**不报错、结果也看着正常**。
  调用时会喊一句（`inside_hermes_dir`），体检也报 BAD。
  **但别一刀切**：`data/workspaces/<名字>` 是 hermes 的**具名会话工作区、完全正当**，
  只有草稿区 `_scratch` 与安装目录里的其它位置才该警告（一刀切在真机上误报过）。
- **起子进程读输出必须有硬看门狗**：`p.stdout.readline()` 在对端一个字都不吐时会
  **永久阻塞**，`while time.time() < deadline` 那种循环根本跑不到——体检因此把按钮
  卡在「体检中…」再也不动（2026-08-20 真机）。用 `threading.Timer(timeout, p.kill)`：
  到点杀进程、读端自然 EOF。UI 侧再兜一层超时——**界面永远不该无限等**。
- **Windows 上 `.CMD` 垫片不能直接 spawn**：npm 装的 `codex` 其实是 `codex.CMD`，
  `subprocess.Popen(["codex", …])` 必然 WinError 2（体检因此误报"起不来"，而面板明明连得上）。
  先 `resolve_command()` 拿真实路径，`.cmd/.bat` 要经 `cmd /c` 起。PATH 候选去重也要
  **大小写归一**，否则同一个文件会被报成"有多份同名命令"。
- **Codex 一条标准 MCP 通知都不发**（2026-08-20 探针实测：全程只有自定义的 `codex/event`）。
  所以 `progress_callback` 那条标准通道对它**收不到任何东西**——要拿过程必须挂
  `NotificationBinding(method="codex/event")`；不挂的话消息在 pydantic 那关就失败
  （真机报 `Field required [type=missing]`），连进都进不来。渲染在 `mcp_client/events.py`
  （纯函数，语料取自真实载荷）：`agent_message_content_delta` 是逐字流、原样拼接不加换行；
  `exec_approval_request` 必须显示——**Codex 在等审批而 hermes 接不了这条通道**，
  不说出来就是干等到超时（出路：调用时给 `approval-policy="never"`）。
  归属按 `_meta.requestId`，认不出宁可不显示，不往别的调用上挂。
- **MCP 工具会实时推过程**（`McpTool.wants_stream=True` → `manager.call(stream=)` →
  `session.call_tool(progress_callback=)` + 上面那条自定义通道）。**agent 型 server 没有它就是黑箱**：codex 一次调用
  跑几分钟，中途什么都看不到、错了也只能等到最后。走的是 hermes 早有的 `tool_stream` 管子
  （前台 shell 用的同一条），**前端不用改**。老 SDK 没有 `progress_callback` 参数会自动跳过。
- **MCP 的 `call_timeout` 要按 server 定，不是全局一个数**（`McpServerConfig.call_timeout`）。
  **agent 型 server**（`codex mcp-server` 那类）一次调用＝跑完一整个 agent 会话，分钟级；
  而全局默认 60s 是按"一次工具调用"定的。为了它调高全局，会把 Playwright 之类的一起放松，
  于是那些真卡死的要等十几分钟才暴露。填 0 或不填都当"跟随全局"，**不是"立刻超时"**。
- **拿 ChatGPT 订阅额度干活＝把 Codex CLI 当 MCP server**（`codex mcp-server`，实测 0.143.0
  暴露 `codex` / `codex-reply` 两个工具）。这是**委派**语义：Codex 跑它自己的循环、工具和沙箱，
  hermes 只发任务描述、收结果，**不是"hermes 每一步都用 GPT 想"**——后者要模型档级别接入
  （OAuth 订阅 token 打非公开端点，ToS 灰区，见 ROADMAP 第三档）。配的时候三件事别漏：
  `cwd` 指到项目目录（否则它在别处改文件）、`call_timeout` 单独放宽、`trust: false`
  （它会自己执行命令）。前提是本机 `codex login` 过（`codex login status` 查）。
- **mcp SDK 2.0 改了字段名**：`Tool.inputSchema` → `input_schema`。`manager.tool_input_schema()` 两名都认——
  别"清理"成只读一个，`pyproject` 写的是 `mcp>=1.2`，新旧机器都可能遇到。踩过的坑是所有 MCP server
  一起连不上（`AttributeError`），而纯 mock 单测全绿——**MCP 相关改动要连真 server 跑一次**。
- **CI 的 windows runner 是英文 locale（cp1252），你的真机是中文（cp936）——别拿"本地过了"当 CI 会过**
  （2026-08-13 首跑 21 个文件红）：① 测试打印中文/`✓` 在 cp1252 下 `UnicodeEncodeError`
  （workflow 已钉 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`；**别指望改字形绕过**，测试文案本来就是中文）；
  ② `TemporaryDirectory` 清理时 sqlite 还开着 → `WinError 32`（Linux 允许删已打开文件，**只在 Windows 现形**）；
  ③ 读文件不给 `encoding="utf-8"` 就按 locale 解；④ Windows 临时目录是 8.3 短名
  （`C:\Users\RUNNER~1\...`），跟 `Path.resolve()` 后的长名**比对不相等**；
  ⑤ **单字节代码页解码永远"成功"**——把 cp1252 排在 GBK 前面等于吞掉一切、只是解成乱码（`_decode_best` 踩过）。
  另：`build.ps1` **本身不跑测试**，所以本地打包成功不代表 CI 的 `test` 闸门会绿。
- **改 `hermes-dev.spec` 的 `collect_submodules` 前先读它里面的 `_skip_cli` 注释**（2026-08-14 踩过）：
  它靠**逐个 `__import__` 子包**发现依赖，某个可选 extra 的 CLI 子模块（`mcp.cli.cli`）import 失败后
  `sys.exit(1)`，会把探测子进程连同整个打包一起带走。**过滤器必须按路径分量匹配**——
  写成子串 `".cli" not in name` 会连 `mcp.client` 一起干掉，打包照样成功但产物没有 MCP 客户端（静默坏掉）。
- **动手前先确认工作目录是不是 git 检出**（2026-08-07 踩过）：`/root` 下同时有 `hermes-dev`(旧快照,无 git) 和 `hermes-latest`(真检出)，按名字猜会把功能建在落后好几个版本的树上，最后要整套移植。先 `git rev-parse --is-inside-work-tree` + 比对 `pyproject.toml` 版本与远端 tag。

## 非目标
语音输入、跨平台原生、多用户。
