# 产品需求文档（PRD）— Hermes Dev

| 项 | 内容 |
|---|---|
| 文档版本 | v0.1 |
| 状态 | 进行中 |
| 最后更新 | 2026-06-08 |
| 负责人 | 用户 + Claude |

---

## 1. 背景与目标

开发一个类似 "Hermes agent" 的工具，**运行在 Windows 上**，用于辅助编程开发。
核心诉求：

- **可自配置模型**：用户自己配置/选择使用哪个模型。
- **多模态输入/输出**：图片、截图（看屏幕）、文件/文档。
- **桌面 GUI** 形态，面向编程开发场景。

### 1.1 目标（Goals）
- G1 一个能在 Windows 双击运行的桌面 Agent。
- G2 模型与能力解耦，切换模型只改配置。
- G3 能真正读写本地代码、执行命令来辅助开发。
- G4 支持图片/截图/文档作为上下文输入。

### 1.2 非目标（Non-Goals，当前版本不做）
- 语音输入/输出（ASR/TTS）—— 明确暂不做。
- 跨平台原生支持（先聚焦 Windows，内核保持可移植）。
- 多用户/云端协作。

---

## 2. 目标用户与场景

- **用户**：开发者本人，单机使用。
- **典型场景**：
  - 让 Agent 读/改本地项目代码、跑命令调试。
  - 贴一张报错截图，让它定位问题。
  - 让它看当前屏幕/界面并给出建议。
  - 喂一份 PDF/文档作为参考来写代码。

---

## 3. 技术决策（详见 docs/adr/）

| 决策 | 选择 | ADR |
|---|---|---|
| 交互形态 | 桌面 GUI（pywebview 外壳 + Web 前端） | [ADR-0002](adr/0002-ui-shell-and-language.md) |
| 技术栈 | Python 3.11+ | [ADR-0002](adr/0002-ui-shell-and-language.md) |
| 模型来源 | Anthropic(Claude) + OpenAI 兼容 | [ADR-0003](adr/0003-model-provider-abstraction.md) |
| 多模态范围 | 图片输入 + 截图看屏 + 文件/文档（不含语音） | [ADR-0004](adr/0004-multimodal-scope.md) |

---

## 4. 功能需求（按阶段）

### P0 脚手架 + 配置 ✅
- FR-0.1 项目骨架与依赖管理。
- FR-0.2 `config.yaml` 模型档案 + `.env` 密钥加载。
- FR-0.3 启动 pywebview 桌面窗口。

### P1 单模型流式对话 ✅
- FR-1.1 接通至少一个 Provider，文本流式输出。
- FR-1.2 前端 markdown + 代码高亮渲染。
- FR-1.3 多模型下拉切换、新会话。

### P2 模型适配层完善（规划中）
- FR-2.1 Claude + OpenAI 双适配稳定可用。
- FR-2.2（可选）UI 设置面板：编辑模型档案 / temperature / max_tokens / base_url。

### P3 工具 + Agent 循环 ✅
- FR-3.1 工具系统：文件读/写/编辑、shell 执行（PowerShell）、代码搜索。✅
- FR-3.2 Agent 主循环（tool-use：plan → act → observe）。✅
- FR-3.3 危险操作权限确认（写文件/执行命令前 gate；逐次确认 + 会话级全允许）。✅

### P4 多模态 — 图片/文档 ✅（图像识别需视觉模型）
- FR-4.1 粘贴/拖拽/选文件添加图片 → 视觉模型 content block。✅ 链路已验证；
  图像识别需用支持视觉的模型（Claude / gpt-4o），MiniMax 当前接口不支持图像。
- FR-4.2 读取 PDF（pypdf 抽文本）/代码/文本作为上下文。✅ M2.7 真机通过。

### P5 截图看屏（已重定范围）✅ v0.7.0
- ~~FR-5.1 全局热键截屏 + 区域选择，喂给模型。~~（与系统 Win+Shift+S + 粘贴链路重复，撤销）
- FR-5.1' **Agent 主动截屏工具** `take_screenshot`：模型在工具循环里主动截屏看屏，
  过权限 gate、走视觉模型识图。✅ Windows 真机验证通过（v0.7.0）。详见 ADR-0010。
  人工截图场景直接用系统 Win+Shift+S 截图后粘贴（已支持，无需开发）。
  - 已知限制：`screenshot: false` 仅移除专用工具，模型仍可绕道 run_powershell 截屏（待后续）。

### P5.1 存储优化 ✅ v0.7.0
- FR-5.1.1 图像不再以 base64 全量入库：改为外置 blob 存储（`data/blobs/`，sha256 去重）+
  DB 存引用，load 会话时 rehydrate；删会话回收孤儿。✅ Windows 真机验证通过（v0.7.0）。
  详见 ADR-0011。

### P6 体验与扩展（进行中）
- FR-6.1 会话历史持久化（SQLite）。✅ Windows 验证通过（v0.5.0）
- FR-6.2 上下文 token 预算与压缩。✅ 实现完成，待 Windows 验证
  （启发式估算 + 保留近窗 + 旧段摘要，裁剪不破坏 tool 往返；持久化历史不受影响）
- FR-6.3 长期记忆。✅ Windows 验证通过（v0.8.0）
  （独立 SQLite `data/memory.db`；模型工具 remember/recall/forget + 离开会话自动抽取 +
  每次发消息把记忆注入 system，使新会话也「记得」。详见 ADR-0012）
- FR-6.4 MCP 工具接入。✅ Windows 验证通过（v0.9.0）
  （作为客户端连接 stdio MCP server，工具自动接入 Agent 循环；外部工具默认过权限 gate。
  仅 stdio + tools。详见 ADR-0013）

### P7 打包
- FR-7.1 PyInstaller 出 Windows exe。✅ **已 Windows 真机构建 + 运行验证通过（定版 1.0.0，2026-06-10）**。
  onedir 形态；只读资源进 exe、config/.env/data 放 exe 旁边、首次运行释放默认 config。
  构建期修过 `appdirs` 缺失（spec 补 pkg_resources/jaraco/packaging 等 hiddenimports）。详见 docs/PACKAGING.md。

---

## 4'. 1.X 路线（P8 / P9，1.0 之后）

> 1.0.0 = P7 打包验证通过后的基线。1.X 聚焦两件事：并发对话（结构性地基）→ 复杂项目支持。
> 决策（2026-06-10）：并发模型用**线程/对话**（复用现有同步 loop，改动最小）；
> **先做 P8 并发、再做 P9 复杂项目**（P8 是 P9 子 Agent 的前提）；P9 第一段做"任务规划与拆解"。

### P8 并发对话与后台运行（v1.1，进行中规划）
痛点：`Api` 是单对话有状态单例（一份 `session_id/messages/workspace/registry/gate`），
`send_message` 在 JS 工作线程里**同步**跑完整个 Agent 循环才返回——所以任务没结束就开不了新对话，
切会话会踩共享状态。目标：能并行开多个对话，未完成任务转后台继续跑。

- FR-8.1 **抽出 `Conversation` 运行时**（纯内部重构、行为不变）：把每对话状态
  （session_id/messages/workspace/registry/gate/_extracted_upto/_pending_workspace）从 `Api`
  单例搬进独立 `Conversation`；`Api` 退化为对话管理器，持有共享资源（config/store/memory/MCP）。
  验收：外部行为不变，全回归全绿。
- FR-8.2 **每对话后台 worker + 非阻塞发送 + 事件路由**：每个 Conversation 一条后台线程
  （单线程保序、复用同步 loop）；`send_message(conv_id, text)` 入队即返回、不阻塞。
  每个 `_emit` 带 `conv_id`；前端按对话分缓冲，只实时渲染当前对话，后台对话用"运行中/有新内容"
  角标提示，切回时回放或从 DB 重载。对话状态机：idle/running/awaiting-permission/done/error。
- FR-8.3 **后台权限与收尾**：后台对话触发权限 gate 时给该会话行红点+通知（不静默卡住）；
  `close()` 优雅停所有 worker；（可选）停止/取消运行中的任务。可选并发上限。

#### P8 详细实现清单（按 FR 分段、可独立交付验证）

**状态划分（动手前的地图）**
- 每对话私有（现散在 `Api` 上、要搬进 `Conversation`）：`session_id / history / workspace /
  registry / gate / active_model / _pending_workspace / _extracted_upto / _conv_attempted`。
- 全局共享（留在管理器、各对话引用）：`config / store(Store,已线程锁) / memory(MemoryStore,已锁) /
  mcp(McpManager,常驻loop) / _mcp_tools / limits / workspaces_root / per_session / window`。

**关键决策**
- 线程/对话：每个 `Conversation` 一条 worker 线程 + 一个串行任务队列（保对话内回合顺序），复用现有同步
  `AgentLoop`。沿用 `_capture_async` 的后台线程先例。
- `gate` 改为**每对话一个**（`_allow_all` 天然就是"本会话全部允许"语义）。
- `active_model` 移进 `Conversation`（各对话可用不同模型）。
- provider 仍每轮临时构建（现已是局部变量，天然隔离）。

**风险（重点 Windows 验）**
- ⚠ `window.evaluate_js` 现在只被单一 worker 线程调用；多 worker 并发调用 WebView2 的 evaluate_js
  线程安全性未知——FR-8.2 需给 `_emit` 的 evaluate_js 调用加锁串行化，并在 Windows 上重点验证。
- 后台对话的权限 gate 会阻塞该对话自己的 worker（符合预期，只它自己等）；别让它静默无提示。

##### FR-8.1 抽出 `Conversation` 运行时（纯内部重构、对外行为不变）✅ Windows 验证通过（2026-06-10）
- [x] 新建 `bridge/conversation.py`，定义 `Conversation` 类：持每对话私有状态 + 一份对共享资源
      （`Resources`）的引用 + `emit` 回调；构造时建自己的 `gate` 与 `registry`。
      `Resources` 另持跨对话账本 `extracted_upto` / `conv_attempted`（按 session_id 记账、需跨切换存活）。
- [x] 把操作私有状态的逻辑从 `Api` 搬进 `Conversation`：`send_message` 主体、`_ensure_session`、
      `_budget`、`_effective_system`、`_maybe_preprocess_vision`、`_maybe_generate_conventions`、
      `_build_registry`、`set_workspace`、`_persist`、`workspace_label`、记忆抽取 `capture_async/_capture_worker`。
- [x] `Api` 退化为**对话管理器**：持共享资源（`Resources`）+ 当前活动对话 `active`；公开方法
      （`send_message/new_session/load_session/open_project/list_sessions/delete_session/
      rename_session/resolve_permission/get_workspace_tree/read_workspace_file/open_workspace_file/
      get_models/set_active_model/close`）转发到 `active`。
- [x] **保持单活动对话语义**：`new_session/load_session/open_project` 替换 `active`；`send_message` 仍同步跑
      活动对话（并发留到 8.2）。`_emit` 暂不变（不带 conv_id）。`active_model` 已移进 `Conversation`，
      `set_active_model` 同步当前对话与管理器默认（行为不变）。
- [x] 自检：全回归保持全绿（13 套，含新增 `test_conversation` 8/8 验两个 `Conversation` 的
      history/workspace/gate/registry 互不串 + Api 委派/切换/删除）。
- [x] 交付 → Windows 验"行为与 1.0.0 一致"（流式/工具/权限/会话切换/打开项目/工作区面板）。✅ 通过

##### FR-8.2 每对话后台 worker + 非阻塞发送 + 事件路由
拆两段交付：**8.2a 后端**（Linux 单测可验）+ **8.2b 前端**（node --check + Windows 真机验）。

**8.2a 后端 ✅ 实现完成（2026-06-10）**
- [x] `Conversation` 加 `queue.Queue` + 惰性启动 + 空闲退出的 worker 线程：串行消费 send 任务、
      复用同步 loop；状态 `idle/queued/running`（done/error 由轮次事件表达）；状态变更发 `state` 事件。
      单个任务抛错不搞死 worker。
- [x] `Api.send_message` 改为**入队活动对话即返回** `{"ok":True,"queued":True}`，不再阻塞。
- [x] `_emit` 改签名带 `cid`：payload 增 `cid`（事件来源对话）；每对话用绑定 cid 的 `self.emit`；
      `Api._emit` 用 `_emit_lock` **串行化 evaluate_js**（多 worker 并发安全）。
- [x] 后端并发自检：test_conversation +5 = 13/13（cid 唯一 / 入队非阻塞 / worker 串行保序且抗错 /
      send_message 异步 / 事件带 cid 且 evaluate_js 无并发重入）。全回归 13 套全绿。

**8.2b 前端 + 后端注册表 ✅ 实现完成（2026-06-10），Windows 验证通过（2026-06-11，定版 v1.1.0）**
- [x] **后端活动对话注册表**（实现中发现的必需项）：`Api.conversations: dict[cid,Conversation]` 保活
      所有运行时（含后台跑着的）；新增 `switch_conversation(cid)` 切回已存在运行时（不重载）；
      `load_session` 优先复用仍活着的同会话运行时（`live:true`），否则冷加载；`new_session/open_project/
      load_session/delete_session` 均返回 `cid`/`active_cid`；空闲空草稿离开时回收防堆积。
- [x] 前端把 `currentBubble/currentText/streaming` 等模块级全局改为**按 cid 的 View**（每对话独立、
      可离屏渲染的 chat 容器）；`__onAgentEvent` 按 `msg.cid` 路由到对应 View——活动视图实时渲染，
      后台视图照常渲染进其离屏 DOM 并标未读；切会话=挂载该 View（后台跑着的直接"续看"）。
      session_id↔cid 用 `session_created` 建立映射。
- [x] 会话栏每行状态标记（运行中脉冲点 / 未读点，CSS）；点后台运行中的会话切过去看到流式续跑。
      **放开 `streaming` 全局封锁**：可随时切会话/开新对话，发送按钮按活动 View 是否在跑来启用。
- [x] 自检：test_conversation 18/18（+注册表/切换/复用/草稿回收/删除）；全回归 13 套绿；app.js `node --check` 过。
- [x] 交付 → Windows 验：任务跑着开新对话不卡；切回后台对话看到继续输出；多对话并发流式无错乱；
      会话行运行中/未读标记正确；单对话行为无回归。✅ 通过（2026-06-11，定版 v1.1.0）

##### FR-8.3 后台权限与收尾 ✅ Windows 验证通过（2026-06-11，定版 v1.1.1；P8 收尾）
范围：必做（权限按 cid 路由 + awaiting 态 + close 优雅收尾）+ 停止/取消运行中任务；**不做并发上限**（用户定）。
- [x] 后台对话触发 `gate.confirm` 时 `permission_request` 带 `cid`（已有）；**`resolve_permission(req_id,decision,cid)`
      按 cid 路由到对应对话的 gate**（修原 bug：各对话 gate 的 req_id 从 1 起会撞号，原固定解到 active）；
      进入 `awaiting` 态发 `state` 事件——前端该会话行橙色脉冲点 + 非活动时全局 toast，切过去看到确认条。
- [x] `Api.close()` 优雅停所有 worker：每对话 `shutdown(timeout=2)`（置 `_stop`/`_cancel`、`gate.reset` 解阻塞、
      `join` 带超时），再关 mcp/store。带运行中/等权限的 worker 也不卡死退出。
- [x] `stop_conversation(cid)`：`AgentLoop.run` 加 `cancel` 标志、**回合开始前检查**；`Conversation.stop()`
      置 cancel + 清空排队任务 + `gate.reset` 解除权限等待；被停时发 `stopped` 事件（已生成部分照常落库）。
      前端运行中以「停止」按钮替代「发送」，点击调用；后台对话切过去再停。
- [ ]（不做）并发上限：用户决定本轮不做。
- [x] 自检：`test_conversation` +4 = 22/22（cid 路由不撞号 / stop 清队列+置 cancel / loop 取消即停 /
      带运行中 worker 的 close 超时内返回不卡）；全回归 13 套绿；`node --check web/app.js` 过。
- [x] 交付 → Windows 验：后台对话要权限有橙点+提示、切过去可处理；运行中点「停止」能中止；关窗干净退出不卡。
      ✅ 通过（2026-06-11，定版 v1.1.1；P8 至此收尾）

### P9 复杂项目支持（v1.2，建立在 P8 运行时之上）
目标：扛更大、更真实的软件项目（v0.9.5"打开已有项目"是前置）。按价值排序、可独立交付：

- FR-9.1 **任务规划与拆解**（1.2 首攻）：长任务 plan → 可勾选子任务清单 → 分步执行；
  进度持久化、扛上下文压缩；前端出任务/待办面板。

#### FR-9.1 详细实现清单（已定决策：工具驱动 + 对话区顶部可折叠条 + 追踪式执行）
**决策（2026-06-11）**：
- 机制＝**工具驱动**（对标 Claude Code TodoWrite）：加非危险工具 `update_tasks`，模型自行判断何时拆解、
  边做边更新状态（`pending`/`in_progress`/`completed`）。不做"自动规划一趟"的额外模型调用。
- 执行＝**追踪式**：模型在正常 agent 循环里自己推进、勾状态；后端自动驱动子任务/子 Agent 留到 FR-9.3。
- 面板＝**对话区顶部可折叠条**（每对话各一份，随会话切换；与按会话隔离天然契合）。
- 持久化＝`hermes.db` 加 `session_tasks` 表（按 session_id，与 messages 同库、随删会话级联）。
- **抗上下文压缩**：把当前未完成清单注入 `system`（_effective_system），压缩后模型也不忘自己的计划。

拆两段：**9.1a 后端**（Linux 单测）+ **9.1b 前端**（node --check + Windows 验）。
- [x] 9.1a：`store/db.py` 加 `session_tasks` 表 + `set_tasks/get_tasks`（删会话级联清）；新工具 `tools/tasks.py`
  （`UpdateTasksTool`，非危险、不过 gate，持 `TaskBinding`=store+session 取值回调+emit，run 落库并发
  `tasks_updated` 事件、回模型一句摘要；纯函数 normalize/summarize/build_block 可单测）；`build_registry`
  加 `task_binding` 注册；`Conversation._build_registry` 注入、`_effective_system` 拼「[当前任务清单]」块
  （抗压缩）、`Api.get_tasks()`；`config.yaml` system_prompt 加任务规划指引。`test_tasks.py` 9/9。
- [x] 9.1b：对话区顶部 `#task-bar` 可折叠条（进度计数 + ✅/🔄/⬜ 列表，折叠态存 localStorage）；按 cid 存
  `view.tasks`、`tasks_updated` 事件路由刷新、挂载会话时 `get_tasks` 拉权威清单；空清单不占位。
- [x] 交付 → Windows 验：复杂任务模型会建清单、顶部条显示并随进展更新勾选；切会话各看各的；
  重启/压缩后计划仍在（注入 system）；简单任务不乱建清单。全回归 14 套绿 + node --check。
  ✅ 通过（2026-06-11，定版 v1.2.0）
- FR-9.2 **代码库检索/索引**：超出 glob/grep 的项目地图 / 符号检索，按需把相关文件喂进上下文。

#### FR-9.2 详细实现清单（已定决策：按需扫描不持久化 + 仅后端工具不改前端）
**决策（2026-06-11）**：按需扫描（无缓存/失效复杂度，中小项目够快）；仅加只读工具，结果走工具块，前端不动。
- [x] 纯逻辑 `codeindex.py`：Python 用 `ast` 精确抽符号（顶层函数/类/类内方法 + 签名 + 行号），其它语言
  （JS/TS/Go/Rust/Java/C…）正则兜底；`walk_outline`/`walk_find`（跳噪音目录、文件/数量/大小上限）；
  `format_outline`/`format_finds`。无新依赖。
- [x] 工具 `tools/codesearch.py`：`code_outline(path=".")` 出目录/文件符号大纲；`find_symbol(name,path?)`
  按名找定义（先精确、全无回退子串）。只读、限工作区内、非危险。`build_registry` 注册（默认开）。
- [x] 加进只读角色白名单（researcher/reviewer/tester 也能用这俩检索工具）；system_prompt 加使用指引。
- [x] 自检：`test_codeindex.py` 8/8（ast 抽取/正则/遍历跳噪音/精确+子串查找/两工具/注册）；全回归 16 套绿。
- [x] 交付 → Windows 验：`code_outline` 给大目录出结构、`find_symbol` 定位定义比 grep 准；模型会在摸项目时
  先用它们；只读子 Agent 也能调用；大项目不超时/不撑爆（含 1600 符号截断压测）。
  ✅ 通过（2026-06-11，定版 v1.5.0）
- FR-9.3 **子 Agent / 委派**（复用 P8 运行时）：为子任务派独立上下文的子 Agent，只回灌摘要，
  保持主上下文精简。

#### FR-9.3 详细实现清单（已定决策：默认同主模型可配 + 可折叠实时子任务块）
**决策（2026-06-11）**：子 Agent 模型默认＝当前主模型、可配 `agent.subagent_model`；前端＝可折叠实时子任务块。
- [x] 9.3a 后端：新工具 `delegate`（`tools/delegate.py`，非危险；纯函数 `compose_task`/`extract_summary` +
  `SUBAGENT_DIRECTIVE`）。`build_registry` 加 `delegate_binding`；`Conversation.run_subagent(task,context)`：
  起独立历史的 `AgentLoop`（**子注册表排除 delegate/update_tasks → 防嵌套+不碰主清单**）、用 `subagent_model`
  或主模型、**共用本对话 gate（危险操作照常确认）与 `_cancel`（停止级联）**、跑完 `extract_summary` 取摘要回灌；
  子事件经 `subagent_start`/`subagent_event`/`subagent_done` 路由。config 加 `agent.subagent_model`/
  `subagent_max_steps`、system_prompt 加委派指引。`test_delegate.py` 8/8 + `test_conversation` 假 provider 集成 2 例。
- [x] 9.3b 前端：可折叠「🤖 子任务」块（实时显示子 Agent 工具行/流式输出，完成后收起留摘要）；按 sub_id 归集
  `view.subBlocks`；抑制 delegate 的通用工具块（避免重复）；历史重渲染用同套渲染回填委派摘要。全回归 15 套绿 + node --check。
- [x] 交付 → Windows 验：复杂任务模型会用 delegate 拆活给子 Agent；子任务块实时显示过程、完成收起留摘要；
  子任务里危险操作有权限确认；点「停止」连子 Agent 一起停；主上下文只进摘要；重载历史能看到委派摘要。
  ✅ 通过（2026-06-11，定版 v1.3.0）

- FR-9.5 **子 Agent 角色与工具限权**（FR-9.3 增强，对标 Claude Code 自定义 agent）：给 `delegate` 加 `role`，
  内置角色定制职责指令 + 工具白名单，让"主 Agent 当调度者、子 Agent 专精分工"成立且更安全。

#### FR-9.5 详细实现清单（已定决策：内置 general/researcher/reviewer/tester）
**决策（2026-06-11）**：角色按"能力判定"限权（兼容动态 shell 名 run_<shell>）；未知/缺省回退 general（向后兼容）。
- [x] `tools/delegate.py` 加 `Role`/`ROLES`/`resolve_role`：general(全工具,默认) / researcher(只读) /
  reviewer(只读) / tester(只读+run_*)；只读=read_file/list_dir/grep_search/glob_search/recall。
  `delegate` 加 `role` 入参（enum）；`DelegateBinding.runner` 签名加 role。
- [x] `ToolRegistry.filtered(keep)` 按工具名过滤；`Conversation._subagent_registry(role)` 在排除
  delegate/update_tasks 基础上再按角色限权（只读角色拿不到 write/edit/shell/screenshot/memory写/mcp）；
  `_subagent_system(role)` 追加角色职责；`run_subagent(task,context,role)`；`subagent_start` 带 role/role_label。
  config system_prompt 加角色选择指引。
- [x] 前端：子任务块头显示角色（「🤖 子任务 · 调研」）；历史重渲染按 input.role 还原角色标签。
- [x] 自检：`test_delegate` 11/11（角色回退/各角色工具权限/注册表按角色过滤）；全回归 15 套绿 + node --check。
- [x] 交付 → Windows 验：模型按子任务性质选角色（调研类走 researcher 等）；只读角色**确实拿不到写/命令工具**
  （让它"改个文件"做不到、不弹写权限）；子任务块显示角色名；general 行为与之前一致。
  ✅ 通过（2026-06-11，定版 v1.4.0）
- FR-9.4 **规模化上下文 + 多文件改动评审**：更聪明的上下文选择；协调式多文件编辑 + 工作区看 diff/可回退。

#### FR-9.4 详细实现清单（已定决策：9.4a 改动评审 + 9.4b 上下文瘦身都做、a 先；台账内存级不持久化）
**决策（2026-06-11）**：「协调式多文件编辑」并入 diff/回退安全网（Agent 本就能连续多文件编辑，缺的是评审与撤销）；
台账随对话运行时存在、重启即清（文件本身不受影响）；只追踪 write_file/edit_file（run_powershell 改的不追踪，已知限制）。
- [x] 9.4a 后端：纯逻辑 `changes.py`（`ChangeLedger`：首次改某文件前快照基线、added/modified/deleted、
  `difflib` 统一 diff（上限 2000 行）、revert=恢复基线/新增文件回退=删除、改回原样不算改动、超 2MB 不追踪）；
  write/edit 工具加 tracker 回调（`build_registry` 注入；**子 Agent 共用同一台账**）；`Conversation` 持台账
  （换工作区即重置）；`Api.get_changes/get_file_diff/revert_file/revert_all_changes`。`test_changes.py` 8/8。
- [x] 9.4a 前端：工作区面板加「改动」区——变更文件列表（＋新增/✎修改/🗑删除），点行在预览区看**着色 diff**，
  每文件「回退」+「全部回退」（均 confirm）；随 refreshWorkspace（每轮 done）自动刷新；无改动不占位。
- [x] 9.4b：`context.py` 压缩超预算时**先瘦身旧回合超长 tool_result**（>600 字截短保留头部+标记，不动最近
  keep_recent_turns 回合、不破坏 tool 配对、不改原消息对象），够了就不丢回合；仍超才走整回合丢弃。
  `CompressResult` 加 `slimmed`。`test_p6_context` +2=7/7。
- [x] 集成自检：`test_conversation` +2=26/26（经注册表写入自动入账+Api diff/回退、换工作区台账重置+
  子 Agent 共用台账）。全回归 17 套绿 + node --check。
- [x] 交付 → Windows 验：Agent 改多个文件后面板列出改动、diff 准确、单文件/全部回退生效且不误伤；
  改回原样自动出账；子 Agent 的写也被追踪；切会话台账隔离；长会话压缩仍正常（瘦身优先于丢回合）。
  ✅ 通过（2026-06-11，定版 v1.6.0；**P9 全部收官**）

### 2.X 路线：P10 工程闭环（2026-06-11 立项）

**背景**：1.X（P8 并发 + P9 复杂项目）收官后，与 Claude Code 等主流工具对比的差距盘点结论——
骨架已齐（工具循环/权限/并发/子 Agent/规划/检索/diff 回退/记忆/MCP），剩余差距在**工程闭环的深度**。
按影响排序立项 P10，五个 FR 各自独立交付、按 SemVer 升版（首个交付定 **2.0.0**，后续 minor）。

- FR-10.1 **Git 集成**（单项收益最大）：工作区若是 git 仓库，提供 status/diff（对 HEAD）/log/分支/commit
  能力——工具给 Agent（commit 等写操作过权限 gate），面板「改动」区升级为 git 语义（跨重启、跨轮次）；
  内存台账保留作非 git 工作区的兜底。开发时遵循仓库礼仪（不在默认分支直接提交）。
- FR-10.2 **读写精度**：`read_file` 带**行号**输出 + `offset/limit` 局部读（大文件不再 200KB 一刀切）；
  新增 `multi_edit`（同文件多处原子替换，全部成功才落盘）；edit 失败信息更可操作。
- FR-10.3 **后台命令/长进程**：`run_powershell` 支持 `background:true`——起 dev server/watch 等长进程，
  返回进程 id；配 `list_processes`/`read_process_output`/`stop_process`；关窗/停止时清理子进程。
  解锁"启动服务→测试→看日志"的 Web 开发场景。
- FR-10.4 **压缩升级 + prompt caching**：压缩摘要从启发式截断升级为**模型生成**（一次便宜调用，
  质量对标 /compact）；anthropic 协议加 `cache_control` 前缀缓存（长会话成本/延迟显著下降，
  方舟端点是否支持需实测，不支持则优雅跳过）。
- FR-10.5 **并行委派 + 自定义角色**：一轮可发多个互不依赖的子任务并行跑（前端多子任务块并存）；
  角色支持用户自定义（config 增 `agent.roles`：名称/职责指令/工具白名单/模型——含**按角色配模型**）；
  （顺手）任务清单条目可标"已委派"联动子任务状态。

**候补（本期不做，按反馈再提）**：FR-2.2 设置面板、MCP HTTP/SSE 传输、细粒度权限规则（allowlist）、
screenshot 绕道修复（system 声明法）、Claude Fable 5 模型档案（用户暂停中，接上即用）。

#### FR-10.1 详细实现清单（已定决策：拆分工具 + 礼仪引导不硬拦；P10 首攻）✅ Windows 验证通过（2026-06-11，定版 v2.0.0）
**决策（2026-06-11，用户拍板）**：
- **工具形态＝拆分**：`git_status` / `git_diff` / `git_log` 为只读工具（非危险、不过 gate，模型随手看）；
  `git_commit` / `git_branch`（建/切分支）为危险工具（过 gate 逐次确认）。
- **仓库礼仪＝引导不硬拦**：system_prompt 写入礼仪（用户没明说就先开分支再提交、只在用户要求时 commit）；
  `git_commit` 的确认信息与结果**显示当前分支名**，在默认分支（main/master）提交时带 ⚠ 提醒，但不拒绝。
- **实现走 git CLI**（subprocess、cwd=工作区），不引入 GitPython 等新依赖；git 未安装/非 git 仓库时
  返回可读错误（不崩）。git 工具**常注册**（描述写明需 git 仓库），避免「会话中途 git init 后工具不出现」。
- **面板「改动」区 git 模式**：工作区是 git 仓库（根有 `.git`）时改走 git 语义——列**全部未提交改动**
  （含暂存/未暂存/未跟踪，跨重启、跨轮次、含用户手改），diff 对 HEAD；「回退」=丢弃未提交改动
  （tracked 用 `git checkout HEAD --`，未跟踪删文件），确认文案写明范围比内存台账大；
  每次调用动态判定（`mode: "git" | "ledger"` 返回给前端），非 git 工作区沿用内存台账兜底（FR-9.4a 不动）。

拆两段：**10.1a 后端**（Linux 单测可验）+ **10.1b 前端**（node --check + Windows 真机验）。
- [x] 10.1a：新模块 `gitsupport.py`——`run_git`（超时/未装 git/非仓库可读错误）+ 纯解析
  `parse_porcelain`（含改名/未跟踪归并为 added/modified/deleted）+ `is_git_workspace` /
  `current_branch` / `changes` / `file_diff`（未跟踪文件合成新增 diff）/ `revert_file` / `revert_all` /
  `commit`（add 指定路径或全部 + commit，回报分支与 ⚠）/ `log` / `branch`（create/switch；
  分支列表并入 git_status 输出，避免只读列表也过 gate）。
  新工具 `tools/git.py` 五件套，注册进 `build_registry`；只读三件加进子 Agent 只读角色白名单。
- [x] 10.1a：`Conversation.changes_mode()` + `get_changes/get_file_diff/revert_file/revert_all`
  按工作区动态路由 git/台账（响应带 `mode`，git 异常不崩面板）；
  config.yaml system_prompt 加 git 使用指引 + 仓库礼仪。
- [x] 10.1b：前端改动区按 `mode` 区分标题「未提交改动·git」/「改动」，回退确认文案按模式
  写明影响范围（git 模式含非本对话改动、新增文件会被删除）；其余复用现有实现。
- [x] 自检：`test_git.py` 10/10（临时仓库：porcelain 解析/增改删/未跟踪 diff/回退/commit 与默认分支 ⚠/
  分支/paths 限定/空仓库/非仓库错误/工具注册与 dangerous 标记）+ `test_conversation` +1=27/27
  （git↔台账路由）；全回归 18 套绿 + node --check。
- [x] 交付 → Windows 验：git 项目里模型会用 git_status/git_diff 看改动、commit 过确认条且显示分支、
  默认分支提交有 ⚠；面板列未提交改动（重启仍在）、diff 准确、回退生效；非 git 工作区行为与 1.6.0 一致。
  两轮通过（首轮反馈修了「全部回退」上百改动卡 UI——逐文件 git 子进程改批量；未配 git 身份的
  commit 报错改中文可操作提示）。✅ 通过（2026-06-11，**定版 v2.0.0，2.X 首个交付**）。

#### FR-10.2 详细实现清单（已定决策：对标 Claude Code 的 Read/Edit/MultiEdit 惯例，纯后端不改前端）✅ Windows 验证通过（2026-06-11，定版 v2.1.0）
**决策（2026-06-11，均按对标基准与现有惯例推定）**：
- `read_file` 输出**默认带行号**（`行号→制表符→内容`，cat -n 风格，与 Claude Code 一致）；
  加 `offset`（起始行，1 起）/ `limit`（最多行数，默认 2000）局部读。**按行流式读**，
  大文件不再 200KB 一刀切静默截断：输出仍设字符上限（防灌爆上下文），**没读完时明确提示
  "继续读用 offset=N"**；超长单行截断加标记。描述写明 edit 时不要把行号前缀带进 old_string。
- `edit_file` 加 `replace_all`（可选，默认 false）；**失败信息可操作**：未找到时检测
  "去行号前缀 / 空白宽松匹配"能否命中并给对应提示（行号带进来了 / 空白缩进不一致 /
  确实不存在请先 read_file 核对）；多处匹配时报次数并提示"补上下文使其唯一，或 replace_all"。
- 新增 `multi_edit`（危险，过 gate，挂改动台账）：同文件多处编辑**按序在内存应用、原子落盘**
  （任意一处失败→整体不写、报第几处因何失败）；每处含 old_string/new_string/可选 replace_all。
- 不加 config 开关（行号默认开）；前端不改；只读角色白名单不变（read_file 本就在）。
- [x] 实现：`tools/fs.py` 重写 ReadFileTool（行号/offset/limit/流式/继续提示）+ EditFileTool
  （replace_all + 可操作失败信息，匹配诊断 `diagnose_not_found` 抽纯函数）+ 新 MultiEditTool
  （`apply_edits` 纯函数原子多处替换）；`build_registry` 注册 multi_edit（危险、挂台账）；
  config.yaml system_prompt 更新读写指引（multi_edit、分段读、别带行号前缀）。
- [x] 自检：新 `test_fs_rw.py` 12/12（行号格式/offset/limit/越界 offset/继续提示/字符上限/
  超长行截断/空文件；edit 未找到三类提示/多处计数提示/replace_all；multi_edit 原子性/按序依赖/
  第 N 处报错/校验/台账挂钩/纯函数）；全回归 19 套全绿。
- [x] 交付 → Windows 验：模型读大文件分段读不再吞尾；改同一文件多处一次 multi_edit 过一次确认
  且失败原子不落盘；edit 失配时模型按提示自我纠正；常规读写/台账/git 面板行为无回归。
  ✅ 通过（2026-06-11，**定版 v2.1.0**）。

#### FR-10.3 详细实现清单（已定决策：对标 Claude Code 的 run_in_background/BashOutput/KillShell，纯后端不改前端）✅ Windows 验证通过（2026-06-11，定版 v2.2.0）
**决策（2026-06-11，按 PRD 既定范围 + 对标基准推定）**：
- `run_powershell` 加 `background:true`：后台启动返回**进程号**（启动本身仍属执行命令、过 gate）；
  配三件套——`list_processes` / `read_process_output`（**增量语义**：每次只回上次读取之后的新输出，
  对标 BashOutput；含运行状态/退出码）/ `stop_process`。list/read 只读**不过 gate** 且进只读角色
  白名单；stop_process 也不过 gate（**只能停本对话后台启动的进程**，与 KillShell 惯例一致）。
- **进程管理器每对话一个**（`tools/procs.py` ProcessManager），跨工作区切换保留；输出由读线程
  收进**环形缓冲**（上限 20 万字符，溢出丢最旧并标记；单次 read 返回上限 5 万）。
- **清理**：关窗（Api.close→Conversation.shutdown）与删除会话运行时必杀全部子进程；
  对话「停止」按钮**不杀**后台进程（dev server 是交付物，要停用 stop_process）。
- **杀进程树**：Windows `taskkill /PID x /T /F`（杀 shell 连带 dev server 子进程）+
  `CREATE_NO_WINDOW` 防黑窗闪烁；POSIX `start_new_session` + killpg。平台逻辑隔离在 procs.py。
- 并发后台进程上限 8/对话（防失控）；普通一次性命令不用 background（提示词写明）。
- [x] 实现：`tools/procs.py`（ProcessManager + 三工具，读线程/环形缓冲/增量游标/杀树）；
  `shell.py` RunShellTool 加 background 入参（manager 注入，未注入时可读报错）；
  `build_registry(process_manager)`；`Conversation` 持 manager（主/子 Agent 共用）、shutdown 杀全部、
  `Api.delete_session` 对被移除运行时（含删当前会话的旧 active）调 shutdown；
  delegate 只读白名单加 list/read；config.yaml system_prompt 加后台命令指引。
- [x] 自检：新 `test_procs.py` 8/8（bash 验：启动→增量读→exited(0)/二次读无新输出/长进程
  running→stop 杀树（连 sleep 子进程）/停止幂等/缓冲溢出 trimmed 标记+单次返回 5 万上限/
  上限 8/未知 id 报错/list 状态/工具注册与 dangerous 标记/无 manager 行为同 2.1.0/角色白名单）+
  `test_conversation` +1=28/28（shutdown 杀后台进程）；全回归 20 套全绿。
- [x] 交付 → Windows 验：模型起 dev server（background）→ read_process_output 增量轮询日志 →
  浏览器验证可访问 → stop_process 杀树后端口立即失效；普通命令无回归；起着 server 关窗后
  任务管理器无残留。✅ 通过（2026-06-11，**定版 v2.2.0**）。

#### FR-10.4 详细实现清单（已定决策：摘要模型生成+按覆盖范围缓存；caching 实测方舟支持、默认开+不支持端点自动降级）✅ Windows 验证通过（2026-06-11，定版 v2.3.0）
**关键实测（2026-06-11，方舟 coding 端点 + kimi-k2.6 直连验证）**：`cache_control` 在 system 块 /
消息块 / tools 上**均被接受且真实命中**（第二次调用 cache_read_input_tokens=3712~3840、
input_tokens 5286→1446）；请求体较小（<约 2000 tokens）时不达缓存门槛，**安静跳过不报错**。
**决策**：
- **10.4a 压缩摘要模型生成**（对标 /compact）：`compress()` 加可选 `summarize(dropped)->str|None`
  注入（纯逻辑保持可单测，None/失败回退现有启发式截断）。Conversation 持**压缩摘要缓存**
  `(覆盖条数, 摘要)`——切点不动直接复用（零额外调用）、切点前移**增量合并**（旧摘要+新增段一次
  便宜调用）；失败 2 分钟内不重试（防每次发送都白付一次失败调用）。配置 `context.model_summary`
  （默认开）+ `context.summary_model`（空=当前对话模型）。`context_compressed` 事件加
  `summary: model|heuristic`。
- **10.4b prompt caching**：anthropic 协议默认加三个缓存断点——system 末块 / tools 末项 /
  最后一条消息末块（断点逐轮后移，前缀按最长匹配复用）；`ModelConfig.prompt_cache`（默认 true）
  可按档案关。**优雅降级**：请求未产出任何事件就失败时降级重试一次（无缓存），错误信息含
  cache 字样则按 (base_url, model) 记入模块级不支持名单、后续不再尝试；流中途失败不重试（防重复输出）。
  不改原始 history 对象（断点打在拷贝上）。openai 协议端点自动缓存、无需改动。
- [x] 实现：`context.py`（compress 注入 summarize + `build_summary_request`/`build_transcript`
  纯函数）；`bridge/conversation.py`（`_compact_summarize`：复用/增量/120s 退避，`_budget` 接线 +
  事件加 `summary` 字段）；`providers/anthropic_p.py`（`apply_cache_breakpoints` 纯函数 + 降级重试 +
  `_CACHE_UNSUPPORTED` 名单）；`ModelConfig.prompt_cache` + `ContextConfig.model_summary/
  summary_model` + build_provider 透传；config.yaml 更新；前端 🗜 提示标注摘要方式（模型/启发式）。
- [x] 自检：`test_p6_context` +3=10/10（summarize 注入/None 与异常回退/摘要请求构造）；
  新 `test_cache.py` 6/6（断点三处与边界形态/原对象不变/cache 错降级且记账/瞬时错重试不记账/
  开关强关）；方舟直连冒烟通过（provider 流式 + 断点，及前置实测缓存真实命中）；
  全回归 21 套全绿 + node --check。
- [x] 交付 → Windows 验：长会话触发压缩后回答仍连贯（压缩后答得出早期细节）且 🗜 提示带
  "模型生成的摘要"；切点不动无额外延迟；ark-kimi 第二轮起首 token 明显提速（缓存命中）；
  MiniMax 档案降级无感；工具/git/后台进程回归正常。✅ 通过（2026-06-11，**定版 v2.3.0**）。

#### FR-10.5 详细实现清单（已定决策：同轮多 delegate 并行 + config 自定义角色 + delegated 任务状态；P10 收官段）✅ Windows 验证通过（2026-06-12，定版 v2.4.0，**P10 全部收官**）
**决策（2026-06-12，对标 Claude Code 并行 subagent / 自定义 agent 推定）**：
- **并行＝同一个 assistant 回合内的多个 `delegate` 调用并发执行**（对标 Claude Code 一轮发多个
  Task）：工具类标 `parallel_safe=True`（目前仅 delegate），loop 把同回合的 parallel_safe 调用
  丢进线程池（**上限 4 并发**）、其余工具照旧顺序执行；tool_result 按原调用顺序回灌。
  前端已天然支持（子任务块按 sub_id 并存、权限条每请求一条、emit 已加锁）；gate/记忆库/进程表
  线程安全已备；`_sub_seq` 计数加锁。停止级联沿用共享 `_cancel`。
- **自定义角色＝config `agent.roles`**（dict，可新增或覆盖内置）：`label`（前端显示）/
  `directive`（职责指令）/ `tools`（工具白名单，省略=全工具）/ `model`（**按角色配模型**，
  省略=subagent_model→当前模型）。`build_roles` 合并内置+自定义；`DelegateTool` 的 role enum 与
  描述按合并结果**动态生成**；未知角色仍回退 general。
- **任务清单联动＝新增 `delegated` 状态**（🤖）：沿用 FR-9.1 工具驱动哲学——system_prompt 引导
  模型"委派某清单项时把它标 delegated、收到摘要后标 completed"，不做易碎的自动挂钩。
- [x] 实现：`agent/loop.py` `_exec_calls` 并行执行组（线程池上限 4、串行组照旧且与并行组并发、
  结果按原调用顺序组装回灌）；`tools/delegate.py`（Role 加 tools/model、build_roles 合并同名覆盖、
  resolve_role 带映射、DelegateTool 动态 schema/描述、parallel_safe）；`config.py` RoleSpec +
  AgentConfig.roles；`Conversation`（_roles、run_subagent 模型优先级 role.model→subagent_model→
  当前、_sub_seq 加锁）；`tasks.py` + 前端 TASK_MARK 加 delegated 🤖（回执单列"已委派"）；
  config.yaml system_prompt 并行/自定义角色/delegated 指引 + agent.roles 注释示例。
- [x] 自检：`test_delegate` +3=15/15（build_roles 白名单所列即所得/同名覆盖/空名跳过/按角色模型/
  动态 enum 与描述；并行：一轮 3 个 delegate 总耗时 <0.7s（串行需 0.9s+）且结果按原序；
  单 delegate/普通工具顺序不变）；`test_tasks` +1=10/10（delegated 归一/🤖 块/回执）；
  全回归 21 套全绿 + node --check。
- [x] 交付 → Windows 验：两个调研子任务并行（双块同时滚动）；自定义角色 docwriter 选用、
  白名单生效（调不到 run_powershell）；任务清单 🤖→✅ 联动；停止级联；单 delegate/普通工具/git/
  后台进程无回归。✅ 通过（2026-06-12，**定版 v2.4.0，P10 工程闭环全部收官**）。

### 3.X 路线：P11 重型任务工程化（2026-06-12 立项）

**背景**：P10 收官后做了一轮**真实任务无头实测**（kimi 真模型驱动内核，bugfix/功能+git/
代码库理解/并行委派 4/4 全过，详见 DEVLOG）。结论：离线中小任务已能独立闭环；剩余差距 =
模型 × 联网 × 权限粒度 × 生态。重型任务（跨多文件/长程自治）的失败大头是工程性"自伤"
（丢上下文、不验证、跑偏不回头）——P11 针对性立项。各 FR 独立交付，首个交付定 **3.0.0**。

**第一梯队（基建 + 最大缺口）**
- FR-11.0 **本地评测基准**（首攻，先立尺子）：无头 harness 正式化为 eval 套件——固定任务集 +
  自动判分 + 一键跑分，后续所有优化可度量。
- FR-11.1 **联网检索**：`web_search` + `web_fetch` 只读工具（查文档/报错/库用法——当前功能面最大缺口）。
- FR-11.2 **验证闭环强制化**：write/edit 落盘后按扩展名自动零成本校验（py_compile/node --check）
  失败立即回灌；任务收尾自动派 reviewer 子 Agent 审改动 diff（零件全在，串线）。

**第二梯队（重型任务核心）**
- FR-11.3 **上下文工程升级**：阶段笔记外置（计划/事实/决定写工作区文件）+ 清单项完成时主动压缩
  该阶段往返 + 丢弃大 tool_result 时留"路径+行号"可重读引用。
- FR-11.4 **细粒度权限 allowlist**：按"工具+参数模式"的允许规则（如 `run_powershell(git *)`）、
  config 可配 + 确认条"记住此类"（治确认疲劳，长任务自治前提）。
- FR-11.5 **Plan mode**：只读规划态（复用 FR-9.5 限权 registry）→ 计划落档 → 确认后解锁执行。

**第三梯队（自治与形态）**
- FR-11.6 **检查点与任务级回滚**：阶段完成自动绑定检查点（git commit+清单快照+阶段笔记），
  一键回退；子任务失败带原因自动重派一次。
- FR-11.7 **CLI / headless 入口**：官方命令行模式（单任务进出，无 GUI，共用内核），解锁 CI/脚本化。
- FR-11.8 **用量可观测**：token/成本统计（provider usage 回传）、步数与预算预警。

**攻坚顺序**：11.0 → 11.1 → 11.2 → 11.4 → 11.3 → 11.5 → 11.6/11.7/11.8
（先能度量，再补能力，再治疲劳，最后自治深水区）。
**候补不立项**：设置面板、MCP HTTP/SSE、LSP/诊断集成（杠杆大工程重，明确放后）、
screenshot 绕道修复、Claude 模型档案（接上即缩小模型差距）。

#### FR-11.0 详细实现清单（已定决策：实测 harness 正式化，4 任务起步、判分全自动可离线验证）
**决策（2026-06-12）**：
- 套件位于 `scripts/eval/`：`harness.py`（无头驱动内核：构造 Api、gate 预置 allow_all、
  事件捕获，shell 按平台自适应 powershell/bash，存储用临时库不碰 data/）+ `tasks.py`
  （任务=夹具 setup + prompt + **程序化判分 check**）+ `run_eval.py`（入口：建临时工作区→
  逐任务跑→打分表，`--task`/`--model` 可选）。
- **起步 4 任务**（即本次实测集）：①bugfix（修双 bug 测试全绿，且不许改测试文件）；
  ②feature+git（开分支实现+补测+提交，验分支/提交/树干净/main 未动）；③代码库理解
  （hermes 源码为语料，按关键标识符命中率判分）；④并行委派（≥2 子任务且 ok，
  事件序证明并行：第 2 个 start 先于第 1 个 done）。
- **判分可离线自检**：每个 check 配"金标准修复/合成事件"用例（不调模型验证夹具与判分本身），
  进 `tests/test_eval.py` 随全回归跑；真跑评测需网络与 key（不进回归）。
- [x] 实现：`scripts/eval/`（harness.py 无头驱动 + tasks.py 四任务夹具与判分 + run_eval.py
  跑分入口，退出码可进 CI）+ `tests/test_eval.py` 离线自检。
- [x] 自检：test_eval 5/5（夹具初始必挂/金标准修复过/改测试=作弊挂/动 main 挂/理解题空话
  不得分/并行事件序四态/语料拷贝）；全回归 22 套全绿；**Linux 真跑 4/4**
  （bugfix 26s / feature_git 38s / comprehend 102s 关键词 5/5 / parallel 192s 真并行）。
- [x] 交付 → Windows 验：离线自检 5/5、全量跑分 4/4、退出码语义、不污染 data/。
  ✅ 通过（2026-06-12，**定版 v3.0.0，P11 首个交付**）。

#### FR-11.8 详细实现清单（已定决策：token/步数可观测，成本不内置定价表；P11 收官段）
**决策（2026-06-12）**：
- provider 在 `done` 事件 meta 带 `usage`：anthropic 全量（input/output/cache_read，实测方舟支持）；
  openai **尽力而为**（自然带 usage 才取，不强加 stream_options 以免打挂不支持的端点）。
- AgentLoop 累加一轮内各步 usage + 步数，回合末发 `usage` 事件 {steps,max_steps,input,output,cache_read}；
  步数接近上限（≥80%）发一次 `step_warning`（长任务"在推进还是打转"可感知）。
- 前端：每轮末一条克制的用量行（tokens 入/出、缓存命中、步数）。CLI `--json` 输出加 usage。
- **不内置美元定价表**（各模型/端点价格多变、易过时）——只给 token 与步数这种客观量;
  成本换算留给用户按自己的单价算。
- [x] 实现：anthropic_p `_usage` / openai_p 尽力取 usage（done.meta，None 安全）；agent/loop.py
  累加 token + 步数、回合末发 usage（全 0 不发）、≥80% 步数发 step_warning；前端 EV.USAGE/STEP_WARNING +
  renderUsage 脚注 + 预警 toast；cli.py usage 进 JSON 与 stderr。
- [x] 自检：test_p3 +2=11/11（两步 usage 累加=230/28/50+步数+max_steps；端点静默不发 usage；
  step_warning 一次）；全回归 29 套绿 + node --check；**真模型冒烟**：CLI --json 显示
  input 4507 / output 231 / cache_read 11136 / 3 步（缓存命中实打实）。
- [x] 交付 → Windows 验：用量脚注（token/缓存/步数）；步数预警 toast；CLI --json 带 usage；
  不支持 usage 优雅留空。✅ 通过（2026-06-12，**定版 v3.8.0，P11 全部收官**）。

#### FR-11.7 详细实现清单（已定决策：复用内核的无头单任务入口，事件流到终端；默认自动批准+deny 仍拦截）✅ Linux 实测，待 Windows 验
**决策（2026-06-12）**：把评测 harness 产品化为正式 CLI——`agentcore/cli.py` + console 脚本
`hermes-cli`。复用与 GUI 完全相同的内核（Api/Conversation），只把事件流打到终端。
- Api 加可选 `emit` 钩子（替代 evaluate_js）；CLI 构造 `Api(cfg, emit=printer)`、跑一轮 send_message。
- 默认**自动批准**危险操作（`gate._allow_all`，等同本机自跑命令）；**config deny 规则仍拦截**
  （gate 中 deny 优先于 _allow_all）。`--plan` 走只读规划态最稳。
- 输出：助手文本→stdout、工具活动→stderr（便于 `>` 取答案）；`--json` 结尾一行
  `{ok,answer,tools,subagents,elapsed,error}`；退出码 0/1 可进 CI。prompt 支持位置参数 / `-` / 管道。
- 无头适配：shell 按平台自适应、关 auto_conventions/screenshot（避免意外写文件/无显示器截屏）。
- [x] 实现：`bridge/api.py` `emit` 钩子；`agentcore/cli.py`（argparse + _read_prompt + run，
  人类/JSON/plan/quiet/max-steps）；**根级 `run_cli.py` 免安装入口**（对称 run.py，自动把 src 加进
  路径——修验证期发现的 `python -m agentcore.cli` 未安装时 ModuleNotFound 问题）；pyproject 加
  `hermes-cli` 脚本；README 加「命令行/无头模式」并标明 run_cli.py / hermes-cli / -m 三种入口的适用前提。
- [x] 自检：`test_cli.py` 7/7（prompt 位置/管道解析；run 人类模式 stdout+stderr 分流+自动批准；
  JSON 单行；plan 置标志不自动批准；error 退出码 1；空 prompt 退 2）；全回归 29 套绿；
  **真实模型四模式实测**（人类/JSON/stdin 管道/--plan 不改文件/修改型退出 0，开发态经 PYTHONPATH）。
- [x] 交付 → Windows 验：用户反馈 `python -m agentcore.cli` 未安装时 ModuleNotFound → 补 `run_cli.py`
  免安装入口（`python run_cli.py "任务" -w 项目`）；其余模式逻辑与平台无关、Linux 已实测，跳过逐项
  重测。✅ 通过（2026-06-12，**定版 v3.7.0**）。（exe 不含 CLI，仅源码/pip 安装可用，README 已注明。）

#### FR-11.6 详细实现清单（已定决策：检查点=任务+笔记+改动文件三件套快照；模型建/用户回退；子 Agent 失败重试一次）
**决策（2026-06-12）**：
- **检查点 = {任务清单 + 工作笔记 + 本对话经文件工具改过的文件当前内容}** 一并快照，存 DB
  （`checkpoints` 表，按会话、删会话级联）。**git 无关**——用改动台账已追踪的文件集（与 ledger
  同口径，已知限制：run_powershell 改的不计），既兼容非 git 工作区、也不往用户仓库塞自动提交。
- **谁建**：①模型用非危险工具 `checkpoint(label)` 在有意义的里程碑创建（对标 update_tasks 的工具驱动）；
  ②前端「存检查点」按钮手动建。**谁回退**：**只有用户**经前端「回到此处」(confirm)——模型**没有**回退工具，
  防它自己抹掉已完成的工作。回退=把文件写回快照（新增的删除）+ 还原任务清单与笔记。
- **子 Agent 失败自动重试一次（11.6b）**：run_subagent 的子循环抛异常时，附上失败原因自动重试一次
  （provider/配置错不重试）；仍失败才把失败摘要回灌主 Agent。
- [x] 实现：db.py `checkpoints` 表 + add/list/get/级联；`checkpoints.py`（capture_files/restore_files/
  make_payload 纯逻辑）；`tools/checkpoint.py`（CheckpointBinding + CheckpointTool 非危险，只创建）；
  Conversation create/list/restore_checkpoint、run_subagent 失败重试一次；Api 转发
  （restore 仅 Api 给前端、不进模型注册表）；前端工作区面板「检查点」区（列表+存+回到此处 confirm、
  checkpoint_created toast）；config system_prompt 加检查点指引。
- [x] 自检：`test_checkpoint.py` 3/3（capture/restore 往返+新增回退=删除+幂等；store 往返+级联；
  工具非危险+校验）；`test_conversation` +2=33/33（Api 建/回退还原文件+任务+笔记、模型无 restore 工具；
  子 Agent 抛异常重试一次后成功）；全回归 28 套绿 + node --check；**真模型端到端**：模型加函数后自发
  checkpoint 存档→文件改坏→用户 restore 一键还原。
- [x] 交付 → Windows 验：模型建检查点+手动存；「回到此处」一键还原文件+任务+笔记；模型无回退能力；
  删会话不残留；委派自发并行。✅ 通过（2026-06-12，**定版 v3.6.0**）。

#### FR-11.5 详细实现清单（已定决策：只读规划态复用 FR-9.5 限权 + 放行 update_tasks/notes；前端开关）
**决策（2026-06-12）**：
- 规划模式＝**对话级开关**（每对话一份，前端按钮切换）。开启时本对话发消息走**只读工具集**：
  复用 `_READ_ONLY_TOOLS`（read/list/grep/glob/code_outline/find_symbol/recall/git 只读/进程只读/
  web 检索）**外加 update_tasks + update_notes**（让模型把计划写成清单与笔记），**屏蔽**所有写/命令/
  截图/记忆写/delegate/git 写/mcp。system 追加规划指令"只勘察+产出方案、不要改文件或执行、计划好就停"。
- 关掉开关＝转入执行：之后正常对话用全量工具按计划做。模式是运行时态、不持久化（重启回默认关）。
- [x] 实现：Conversation `plan_mode`+`set_plan_mode`、send_message 按 plan_mode 选注册表
  （registry.filtered(in _PLAN_TOOLS)）、`_effective_system` 加 `_PLAN_DIRECTIVE` 块；
  `Api.set_plan_mode`；前端输入区「📋 规划」开关（按 cid 存 view.planMode、顶部提示条、
  发送按钮文案变"规划"、切会话同步）。
- [x] 自检：`test_conversation` +1=31/31（plan 工具集只读+update_tasks/notes 在、写/命令/委派不在；
  set_plan_mode 切换 + system 注入/移除）；全回归 26 套绿 + node --check；
  **真模型端到端**：规划模式下只 list/glob/read/git_status 勘察 + update_tasks/notes 出计划、
  app.py 零改动；关闭后 write_file 落地 argparse。
- [x] 交付 → Windows 验：开「📋 规划」→ 模型只勘察产出计划不动文件、关闭后执行、挡写、多对话独立。
  ✅ 通过（2026-06-12，**定版 v3.5.0**）。验证反馈：规划按钮 UI 与 📎/发送不统一，已在 3.5.0 内
  改为矢量图标按钮（见 CHANGELOG Fixed）。

#### FR-11.3 详细实现清单（已定决策：工作笔记外置 + 可重读引用；「主动按阶段压缩」重诠释为笔记承载）
**决策（2026-06-12）**：
- **11.3a 工作笔记外置**（核心）：加非危险工具 `update_notes`（整份替换，对标 update_tasks），
  把"已确认事实 / 已做决定 / 当前进展 / 待避免的坑"存到**会话级**（`session_notes` 表，跟 tasks
  同库、删会话级联），并注入 system「[工作笔记]」块——**抗上下文压缩、跨重启**。任务清单=待办，
  工作笔记=过程中沉淀的事实与决定，二者平行。
- **重诠释原②「清单项完成时主动压缩」**：不做脆弱的"精确切割某阶段工具往返"（易破坏 tool 配对、
  与现有压缩重叠）。改为：让模型把阶段结论写进工作笔记，**旧往返即便被压缩丢弃，结论仍在笔记里**
  ——以更稳的方式达成"阶段推进不丢线索"的目标。system_prompt 引导"完成一个阶段就把结论记进笔记"。
- **11.3b 可重读引用**：压缩瘦身大 tool_result（FR-9.4b）时，若该结果来自 `read_file`，在截短标记里
  写明来源文件与"可用 read_file 重读"——比单纯截断更有指引，模型需要细节时能精准重取。
- [x] 实现：db.py `session_notes` 表 + set_notes/get_notes（删会话级联）；`tools/notes.py`
  （NotesBinding + UpdateNotesTool + build_notes_block 纯函数）；build_registry 注册（仅主 Agent，
  子 Agent 不含）；Conversation 注入 binding、`_effective_system` 拼「[工作笔记]」、`get_notes`/
  `Api.get_notes`；context.py `_read_sources`（tool_use_id→read_file 路径）+ 瘦身标记带"可重读"；
  config system_prompt 加笔记指引。
- [x] 自检：`test_notes.py` 5/5（build_block/存取与级联/工具整份替换与校验/注册/截短带 read 来源标注、
  非 read 不带、原对象不改）；全回归 26 套绿 + node --check；**真模型端到端**：模型用 update_notes
  记下项目约定→落库→**重启加载同会话后笔记自动注入 system**（含 Python 3.12 等具体内容）。
- [x] 交付 → Windows 验：模型用 update_notes 记事实/决定；压缩后追问早期结论仍答得出；重启后同会话
  笔记还在；删会话不残留。✅ 通过（2026-06-12，**定版 v3.4.0**）。

#### FR-11.4 详细实现清单（已定决策：allow/deny 规则 + 确认条「总是允许这类」；对标 Claude Code permissions）✅ Linux 实测，待 Windows 验
**决策（2026-06-12）**：
- 规则＝`工具名` 或 `工具名(glob)`，glob 匹配该工具「主体」（run_* 取 command、文件类取 path、
  web 取 url；fnmatch、大小写敏感）。config `agent.permissions.allow/deny`；**deny 优先于 allow，
  也优先于「本会话全部允许」**（硬拦截不被绕过）。
- 确认条加「总是允许这类」：把推导规则（命令→首词通配、路径→父目录通配、否则裸工具名）加入
  **本会话** allow（重启不保留，与 _allow_all 同生命周期）。
- 纯逻辑 `permissions.py`（tool_subject/parse_rule/rule_matches/evaluate/suggest_rule）可单测；
  gate 接 allow/deny + 新决定 `allow_rule`。
- [x] 实现：`permissions.py`；`gate.py` 接规则评估（confirm 先判 deny→allow→_allow_all→询问）+
  emit 带 suggest + ALLOW_RULE；`PermissionsConfig` + `AgentConfig.permissions`；Conversation 构造
  gate 注入 config 规则；前端确认条「总是允许 <规则>」按钮（perm-rule 样式）；config.yaml 示例。
- [x] 自检：新 `test_permissions.py` 8/8（解析/主体/匹配/deny 优先/推导/gate：config allow 免弹、
  deny 不弹直接拒、记住此类后同类免弹、allow_all 仍生效但 deny 优先）；全回归 25 套绿 + node --check；
  **Linux 真模型实测**：config `allow:["run_bash(git *)"]` 下模型连跑 git init/status 零权限请求。
- [x] 交付 → Windows 验：config allow 放行不弹、deny 直接拒、「总是允许这类」本会话同类免确认异类仍问、
  全部允许仍可用但 deny 优先、规则重启不残留、无配置行为不变。
  ✅ 通过（2026-06-12，**定版 v3.3.0**）。

#### FR-11.2 详细实现清单（已定决策：11.2a 自动校验默认开、11.2b 收尾评审默认关；纯后端不改前端）
**决策（2026-06-12）**：
- **11.2a 写入后零成本校验**（默认**开**）：write/edit/multi_edit 落盘后按扩展名校验——
  .py/.pyi 用标准库 `ast.parse`、.json 用 `json.loads`（**无依赖、跨平台、必可用**），
  .js/.ts 等用 `node --check`（无 node 静默跳过）；失败信息（含行号）**并入工具返回**回灌模型，
  改坏在当步暴露、不必等模型自己想起来验。校验器异常绝不影响写入本身。
- **11.2b 收尾自动评审**（默认**关**，按需开）：一轮里改过文件就在收尾派 reviewer 子 Agent 审
  本轮 diff（只读、结论经子任务块呈现、不改主历史）；纯对话/只读轮零开销，取消时不触发。
  默认关因为每次多一次模型调用——重型/重要改动时开。
- [x] 实现：新 `verify.py`（detect_kind/verify_text 纯函数 + make_verifier）；fs.py 三个写工具
  加 verifier 注入与 `_with_verify`；`build_registry(verifier=...)`；`AgentConfig.auto_verify`
  （默认 true）/`auto_review`（默认 false）；Conversation 注入 verifier、`_maybe_auto_review`
  （扫本轮写工具调用 → 派 reviewer 审 diff）；config.yaml 两开关。
- [x] 自检：新 `test_verify.py` 6/6（detect_kind/py 与 json verify_text/make_verifier 读盘/
  三写工具并入校验结果/无 verifier 行为不变）；`test_conversation` +1=30/30
  （auto_review 仅写轮触发、纯对话/取消/关闭不触发）；全回归 24 套绿；
  **真实模型端到端**：模型写出缺冒号的 py→工具返回当场报语法错→模型 edit 自我修正→最终语法 OK。
- [x] 交付 → Windows 验：模型改 py/json 改坏时工具结果即报语法错并自我修正；
  开 auto_review 后改代码收尾出现 reviewer 子任务块给评审结论；纯对话不触发。
  ✅ 通过（2026-06-12，**定版 v3.2.0**）。

#### FR-11.1 详细实现清单（已定决策：免 key 双源 auto 链路，标准库实现零新依赖）
**关键实测（2026-06-12，开发机直连）**：Bing `www.bing.com/search` HTTP 200 可解析
（`b_algo` 块，真链在 `u=a1<base64>` 参数）；DDG lite HTTP 200 可解析（真链在 `uddg=` 参数）；
DDG html 版 202 反爬不可用。Bing 国内外均可达 → **auto 链路 = Bing 优先、DDG 兜底**。
**决策**：
- 两个只读工具（非危险、不过 gate、进只读角色白名单）：`web_search(query, max_results?)`
  搜索并返回"标题/URL/摘要"列表；`web_fetch(url, max_chars?)` 抓页并转正文文本
  （HTMLParser 去 script/style、保标题；JSON/纯文本直出；下载 2MB、输出默认 2 万字符截断带标记）。
- **零新依赖**：urllib + html.parser + 正则解析；解析器为纯函数（喂金标准 HTML 离线单测）；
  引擎页面改版导致解析失败时给可读错误并自动换下一个源。
- 配置 `web` 段：enabled（默认开）/ search_engine（auto|bing|duckduckgo）/ timeout /
  max_results / fetch_max_chars；enabled:false 不注册工具（行为同 3.0.0）。
- system_prompt 加联网指引（查文档/报错/库用法先搜后答、引用来源 URL）；
  允许抓 localhost（配合 FR-10.3 自测 dev server 是特性不是漏洞）。
- [x] 实现：`tools/web.py`（_http_get 集中 IO / parse_bing / parse_ddg_lite / bing_real_url /
  extract_text 纯函数 + 两工具）；`config.py` WebConfig + config.yaml web 段；
  `build_registry(web=...)`；Conversation 主/子注册表注入；delegate 只读白名单加两工具；
  system_prompt 联网指引。
- [x] 自检：新 `test_web.py` 6/6（金标准 HTML 解析/a1+base64 与 uddg 真链解码/extract_text
  去脚本保标题/空 query 与非 http 报错/auto 换源与聚合错误/disabled 不注册+角色白名单）；
  直连冒烟（bing 真实结果、example.com 正文）；**真实模型端到端**：PEP 703 状态题——模型自发
  search→fetch PEP/官方文档→交叉验证→带来源准确作答（10 次工具调用 77s）。全回归 23 套全绿。
- [x] 交付 → Windows 验：对话内自发 search→fetch→答案带来源；时效事实先搜后答；
  来源 URL 点击走系统浏览器（窗口不动）；子 Agent 可联网；web.enabled:false 回退正常。
  ✅ 通过（2026-06-12，**定版 v3.1.0**）。

### 4''. 4.X 路线：P12 工程深度（2026-06-12 立项）

**背景**：P11 收官后做了一轮真实复杂项目实测（kimi 驱动从零做表达式求值器，28/28 独立对抗测试通过，
详见 DEVLOG）。结论：**harness/工程闭环层已与成熟工具同档，不再是瓶颈**；剩余差距收敛到
模型 × 工程深度 × 生态。P12 针对"工程深度层"——把 hermes 在真实工程里的稳健性与诊断能力补齐。

- FR-12.1 **provider 韧性**（先做）：模型调用对瞬时错误（网络抖动/429/5xx）自动退避重试，
  仅在吐内容前重试（避免重复输出）；与已有 cache 降级共存。
- ~~FR-12.2 诊断升级~~ **（2026-06-12 评估后撤销，不做）**：探测外部 linter / diagnose 工具 / LSP
  三条路深究后 ROI 均低——①接 ruff：模型已有 shell 能自己跑 lint/test，diagnose 工具只是包装、
  无新能力，且 linter 只抓低级错（未用 import/未定义名），这些模型跑测试就暴露；②上 LSP：重工程，
  但 agent 有"执行测试"这个更强的 ground truth，LSP"不跑就知道"对 agent 的边际收益远不如对人类，
  且 hermes 无 IDE 宿主拿不到实时 LSP；③触发本 FR 的"除零没包装领域异常"是逻辑/设计问题，linter/LSP
  都查不出（那是测试/审查的活）。而 hermes 现有三层已覆盖诊断核心：auto_verify 语法校验 + 模型自己
  跑测试（比 linter 强）+ 收尾 reviewer 审 diff（比 linter 更抓逻辑）。结论：再叠 linter/LSP 是边际
  递减。**Claude Code 非 IDE 模式本身也不内置 linter/LSP，就是让模型跑命令**——印证此判断。

**候补（本期不做）**：完整 LSP 集成、IDE 插件、MCP HTTP/SSE。
**最高优先级候补：Claude 模型档案**——已分析确认"模型本身"是 hermes 体验上限的最大单一变量，
接 `ANTHROPIC_API_KEY`（档案已配好、网络验过能通）即用、零开发，收益远大于继续雕工具。

#### P12 检查点重构（方案A 自动打点，2026-06-12 与用户讨论后定）✅ Linux 实测，待 Windows 验
**背景**：用户质疑 checkpoint 价值。盘点结论——主流（Claude Code 较新版/Cursor）也有 checkpoint，
但是**自动每步打点 + rewind**，hermes 旧设计是"模型手动调工具"，两个弱点：①靠模型自觉（常忘，实测
做求值器全程没调过）②与 git/改动台账重叠。决策**方案A**：改自动打点，对标主流。
- [x] 实现：删模型 `checkpoint` 工具（tools/checkpoint.py + binding + registry）；`AgentConfig.auto_checkpoint`
  默认开；`Conversation._on_change`（替换 change_tracker=ledger.snapshot）——回合内每个文件首次改动前把
  其旧内容累加进**同一个**检查点（_turn_snap + _upsert_turn_checkpoint，首建后 update），主/子 Agent 共用；
  send_message 回合开始重置；store add/update/prune_checkpoints（自动留最近 30）；config system_prompt
  去掉 checkpoint 工具指引、改注明"系统自动打点"；前端自动打点静默刷新不弹 toast、手动存才提示。
- [x] 自检：test_checkpoint 3/3（含 prune）；test_conversation 34/34（多文件回合一个检查点、回退撤销整回合
  含新建文件删除+已有文件还原；模型注册表无 checkpoint 工具）；全回归 32 套绿；**真模型端到端**：模型
  edit 加函数、全程没碰检查点工具，系统自动打点（标签取自用户消息），用户回退精确还原改动前原文。
- [x] 交付 → Windows 验：改文件后面板「检查点」自动出现"改动前 · <你的话>"（无需模型操作）；
  「回到此处」撤销整回合；多轮多个检查点；删会话不残留；模型无 checkpoint 工具。
  收尾 UI：删手动「＋存检查点」按钮（自动已覆盖）、「回到此处」图标化（回拨箭头+悬浮提示，宽度固定）。
  ✅ 通过（2026-06-12，**定版 v3.10.0，P12 收官**）。

#### FR-12.1 详细实现清单（已定决策：瞬时错误退避重试，仅吐内容前，与 cache 降级共存）
- [x] 实现：`providers/base.py` `is_transient_error`（status 408/409/429/5xx/529 + 异常名 + 消息启发式）
  / `backoff_delay`（指数退避+抖动、封顶 20s）/ `retry_stream`（仅未 yield 时重试，MAX_RETRIES=3）；
  openai 抽 `_stream` + retry_stream 包裹；anthropic 统一循环（cache 降级不计退避 + 瞬时退避重试）。
- [x] 自检：`test_retry.py` 8/8（瞬时判定/退避递增封顶/重试后成功/吐内容后不重试/非瞬时不重试/
  用尽抛出/anthropic 瞬时重试/cache 降级与瞬时重试共存）；全回归 31 套绿；真模型冒烟正常调用不受影响。
- [x] 交付：纯后端容错、平台无关，test_retry 8/8 + 真模型冒烟无回归 → 用户决定跳过 Windows 验。
  ✅ **定版 v3.9.0**（2026-06-12）。

---

### 5'. 调试能力工程化（用户称「P5」，2026-06-23 立项）

**背景**：现状下 Agent 写完只做零成本语法校验（`py_compile` / `node --check`，见 FR-11.2a），缺的是
**运行时对错信号与中间证据**——「每轮修改没有即时对错信号」「盲调，看不到中间数值」「不可复现」。
借鉴 Claude Code 的调试工作流，立项补「**编辑→运行→看证据→定位→修**」的闭环。核心判断：这些能力对
**任何模型都加分**（弱模型少猜几轮、不至瞎改），与「接 Claude」是叠加而非替代——先补闭环性价比最高。
各 FR 独立交付，按上面三波推进；FR 编号续 FR-13.x。

**FR 清单（A–I → 三波）**：

- **第一波（便宜、立竿见影；prompt/directive + 轻扩展）**
  - FR-13.A **复现优先流程**：debug 任务先固化「现象 + 触发输入 + 期望/实际」，再动手；directive + `/debug` 引导。
  - FR-13.B **traceback 自动定位**：工具/命令报错时解析 traceback，定位到文件:行 + 摘出相关源码片段回灌，少一轮「贴报错」。
  - FR-13.C **编辑后跑定向测试**（扩 `verify.py`，首攻）：写/改文件后**识别受影响的测试并直跑**，把通过/失败结果喂回循环——
    从「语法对不对」升到「测试过不过」，补「每轮无即时对错信号」的核心缺口。落地：探测测试命令（pytest/node:test）+ 按改动文件映射测试。
  - FR-13.F **调试便签**（扩 `tools/notes.py`）：显式记录「假设 X / 已排除 Y / 证据 Z」结构，跨轮不丢、不重复试错路。
- **第二波（核心，投入大但质变；补「运行时证据」）**
  - FR-13.D **trace_run 插桩工具**（新 tool，**最值得投入**）：给定函数/位置 + 输入 → 临时注入日志/打印 → 跑 → 收集中间值 → 自动还原。
    让 Agent 真看到「这步算出来是多少」，把盲调变有踪可查。
  - FR-13.E **失败输入固化为 fixture**（`capture_fixture` 工具 + 约定）：出现错值时把当时输入状态快照成 fixture，bug 从「不可复现」变「可复现」。
- **第三波（锦上添花）**
  - FR-13.G **debugger 子角色**（扩 `delegate.py` 的 `ROLES`）：在 researcher/reviewer/tester 之外加 debugger——只读勘察、专职「定位 + 产出复现」，缩小范围后交主循环修。
  - FR-13.H **轻量诊断**（曾在 FR-12.2 撤销「LSP 集成」，此处重提**轻量版**：探测外部 linter 跑、不内嵌 LSP；范围另议）。
  - FR-13.I **回归二分定位**：「以前好的、现在坏了」时，对改动/提交做二分缩小到引入点。

**靶心（用户 2026-06-23 补充的真实场景）**：长项目后期 debug **反复「定位不准原因」、改半天改不好**。
据此校准——三波是按**成本/ROI**排的，但最直击「定位不准/盲调」的是 **D 插桩 trace**（给运行时证据）+ A 复现 + B traceback；
C 给的是「对错信号」（知道*还错着*）、是该先有的便宜地基，但**不把第一波全做完才轮到 D**。
「是否模型能力限制」结论：定位不准分两层——①**没数据可看**（多数"反复定位不准"其实是 agent 无证据瞎猜，**工程可解**：D/B/A 摘眼罩）
②**拿到证据后的推理质量**（吃模型，Claude 更准）。故大头工程可补、残差是模型天花板，与「接 Claude 叠加非替代、先补闭环最划算」一致。

**推进顺序（用户 2026-06-23 定）**：**C（地基，扩 verify.py）→ D（+A 给复现输入）→ 再回头 B/F/E → 第三波 G/H/I**。
- **FR-13.C ✅ 已实现接入**（待 Win 验）：`verify.py` 受影响测试探测 + 自动探测命令 + `make_post_edit_checker` 组合校验；
  config `auto_affected_test`/`affected_test_runner`；fs.py 不动。真机修了 PYTHONPATH（`from x import` ModuleNotFoundError）+ 不写 pyc。自测 24/24。
- **FR-13.D ✅ 已实现**（待 Win 验）：新工具 `trace_run`（`tools/trace.py`）——子进程 `sys.settrace` 记录工作区内函数逐步局部变量+返回值+崩溃前轨迹，
  让 Agent **看到中间值**而非盲调。**改用 settrace 而非"插 print 再还原"**（零源码改动、无需还原、更全更稳）。自测 9/9。
- **FR-13.A ✅ 已实现**：config.yaml 系统提示加「调试准则」（复现优先→trace_run 看证据→capture_fixture 固化→受影响测试转绿→notes 记假设）。
- **FR-13.B ✅ 已实现**：新 `diagnose.py` 解析 traceback、定位工作区内最深一帧、读盘摘源码上下文回灌；接入 `run_shell` + 受影响测试输出。自测 10/10。
- **FR-13.F ✅ 已实现**（轻量）：扩 `update_notes` 说明 + directive，引导「## 调试便签」结构（现象/假设/证据/已排除/下一步验证）。
- **FR-13.E ✅ 已实现**：新工具 `capture_fixture` 把触发输入固化成 `tests/test_capture_*.py` 并立刻跑一次确认复现；自动接入 FR-13.C 闭环。自测 6/6。
- **进度小结**：第一波 A/B/C/F + 第二波 D/E 全部落地，✅ **Windows 真机验证通过、定版 v3.39.0**（2026-06-23）；仅剩第三波 G/H/I（按需）。全回归绿：Python 38 文件 + 前端 23。

---

## 5. 非功能需求

- **可移植性**：平台相关代码（截图/shell/热键）隔离到独立模块。
- **安全**：密钥只存 `.env`，不入库；危险操作需确认。
- **可维护性**：模型/工具插件化，遵循 docs/CONVENTIONS.md。
- **可观测**：错误信息冒泡到 UI，开发期可开 devtools。

---

## 6. 验收标准（每阶段）

每阶段交付后由用户在 **Windows 真机**验证；通过后记入 DEVLOG 并进入下一阶段。
未通过则记录问题，确认后再修复/推进。

---

## 7. 待确认问题（Open Questions）

- OQ-1 P2 是否需要 UI 设置面板，还是手改 yaml 即可？（已决：延后到 P6，2026-06-09）
- OQ-2 P3 shell 工具默认用 PowerShell 还是 cmd？（已决：PowerShell，可配置，2026-06-09）
- OQ-3 是否需要会话历史跨重启持久化（P6）。
