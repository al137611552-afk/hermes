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
  - **非交互硬化 + 交互提示识别**（✅ 定版 v3.61.0，2026-08-11，Windows 真机验证通过）：环境变量
    补齐（npm/ssh/gh/dotnet/CI 等）+ PowerShell 进度条前缀；命令停在交互提示上时**认出来并点名提示原文**
    （保守判据：还活着 + 最后一行像提示 + 静止≥5s），不再干等满 180s；超时文案按成因拆三条。
    **立场：不做全局 auto-yes**（确认框是防误删的最后一道闸）。顺带修好前台"实时流输出"其实不实时。
  - **交互式命令的应答通道**（✅ 定版 v3.62.0，2026-08-11，Windows 真机验证通过；ADR 0022）：后台进程 stdin 开 PIPE，
    一条通道两个入口——`write_process_input` 工具（模型侧，过 gate）+ 工具块行内输入行（人接管，不过 gate）。
    **前台不等人**（无人值守不能卡在提示上），人接管只存在于后台通道。
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

#### FR-11.1b 检索质量与分工修正 — ✅ 已定版 v3.53.0（2026-08-07，**纠 v3.43 的结构约束**）

**问题**：用户反馈"浏览器工具链在搜索场景形同虚设，搜了半天几乎一无所获"，并归因为
"`browser_snapshot` 抓不到 JS 动态渲染内容"。

**实测复核（2026-08-07，本机 Playwright + 真实网络）——归因要改**：
| 目标 | 默认 headless | 换真实 Chrome UA |
|---|---|---|
| playwright.dev 文档（纯 React SPA，全 JS 渲染） | aria 快照 **19,954 字符 / 22 个内容节点** | 同左 |
| Bing 结果页 | 标题正常、body **35 字符**、结果块 **0 个** | **仍 0 个** |
| DuckDuckGo | 明文验证码挑战 | 报错页 |
| 百度 | 「百度安全验证」滑块 | **正常 10 条、快照 28,350 字符** |

→ **无障碍快照抓得到 JS 渲染内容**（SPA 文档站证伪了原假设）；抓不到的是**反爬识别出自动化浏览器后
返回的空壳结果页**。真正的病根是 **v3.43 的结构约束**：只要挂上 `browser_*` 就把 `web_search` +
`web_fetch` 物理摘掉（`conversation.py` 主/子注册表两处），逼一切走浏览器——而搜索引擎恰恰是浏览器
最过不去的一类站，等于把唯一稳定的搜索通道砍了。**主流无一家用浏览器驱动搜索引擎**（Claude Code 走
Anthropic 服务端 web_search、Gemini CLI 走 Google 搜索 grounding、Codex 走服务端带缓存的 web search），
业界分层是「官方 API/结构化端点 → 直接 HTTP → 只有需 JS/登录/交互才上浏览器」。

**决策**（本轮范围＝免 key，不引入任何第三方搜索 API key）：
- **分工按能力固定，且由代码保证而非 prompt**：搜索恒走 HTTP；`web_fetch` 命中
  `looks_blocked`（反爬/登录墙/JS 空壳）时**自动升级**用浏览器读同一 URL（`browser_reader` 注入），
  模型没有"换个搜索引擎再搜一遍"这个动作可做 —— v3.43「不许绕路」的本意保留，但不再连快路一起砍。
- **结构化搜索源**：Bing 优先走 `&format=rss`（实测 10 条干净 XML），HTML 解析降级兜底。
- **多引擎并发 + RRF 融合**：Bing 与 DDG 并发跑、按 `1/(60+rank)` 融合去重（跨引擎都出现的结果上浮），
  再走既有 `rerank_results` 控源多样性 —— 取代"第一个有结果就返回"。
- **正文提取升级**：readability 式主正文抽取（按文本密度/链接密度选块、类名黑白名单），
  抽不出可信正文则回退整页；新增 `focus` 参数按需摘录相关段落。
- **浏览器伪装**：`browser_mcp_args` 默认加 `--user-agent`（真实 Chrome UA）。
- **标签页卫生**：directive 要求读完 `browser_tabs` 关掉（旧标签会让每次 snapshot 都拖一串，白烧上下文）。
- 配置新增 `web.browser_fallback`（默认开）：关掉则只提示受阻、不自动动用带登录态的浏览器。
- [x] 实现：`tools/web.py`（parse_bing_rss / canonical_url / fuse_results / extract_main_text /
  score_node / excerpt_for_query / _clip + 并发 `_gather` + `browser_reader`）；
  `conversation._make_browser_reader()` 取代 `_drop_web_when_browser`；`build_registry(browser_reader=…)`；
  `config.BROWSER_UA`；config.yaml 与 researcher 角色 directive 改写。
- [x] 自检：`test_web.py` 13→**23**、`test_conversation.py` 两条钉旧行为的测试改钉新分工、
  `test_p6_mcp.py` +1；全回归 Python 全绿 + 前端 67/67。
- [x] **真跑（真 kimi + 真 MCP 浏览器）**：①联网问答 3 步 89s 出带来源的正确答案，模型**自发用上 `focus`**；
  ②HTTP 空壳页 → 自动升级 → 真实 Playwright MCP 浏览器渲染读到正文（0.6s）。
- [x] **Windows 真机验证通过**（2026-08-07）：真机搜索质量（国内网络下 Bing RSS）、浏览器穿透下 UA 生效、
  自动升级在有登录态时的表现、标签页不再堆积。**定版 v3.53.0**。
- **同版一项产品决策**：**应用内更新提醒默认关**（新配置 `agent.update_check=false`）——用户不要这个提醒。
  `Api.check_update` 是前端唯一检查入口，在那一处拦住即整条链路停用、关着时不发网络请求；
  ADR 0020 的能力保留（设 `true` 即回来）。**副作用**：以后推 tag 不再触发客户端更新提示，发版靠自己 pull/打包。

**同轮修掉的存量 bug（真跑暴露）**：mcp SDK **2.0 把 `Tool.inputSchema` 改名 `input_schema`**，
`manager.py` 仍读旧名 → 装了新 SDK 的机器**所有 MCP server 一律连不上**（浏览器穿透/文件系统/Codex
模板全废）。改 `tool_input_schema()` 两个名字都认、都没有给空 schema；`pyproject` 的 `mcp>=1.2`
不动（新旧都能跑）。有回归测试钉死。

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

### FR-13.S 技能包（Agent Skills）— ✅ 已定版 v3.52.0（2026-08-07）

**动机**：把"某类活怎么干"打包成可复用技能，稳定专项工作能力。外部现状（2026-08 核实）：Agent Skills 已于 2025-12-18 开放为**公共规范**，约 40 个产品兼容（Codex/Copilot/Cursor/Gemini CLI/Goose…）——对齐格式即可互通生态。

- **形态**：一个目录 + `SKILL.md`（YAML frontmatter + Markdown），可带 `scripts/`（脚本）/`references/`（文档）/`assets/`（模板）。**严格对齐规范字段，不自造**。
- **渐进披露三层**：①`name`+`description` 常驻 system（实测 ≈100 token/技能）→ ②`load_skill` 读正文 → ③脚本/文档/模板用现成工具按需取。这是"装几十个技能也不撑上下文"的关键。
- **查找顺序**（靠后优先、同名覆盖）：内置 `<BUNDLE_DIR>/skills` → 用户全局 `<APP_DIR>/skills` → `agent.skills_dirs` → 项目级 `<工作区>/.hermes/skills`。
- **安全（刻意收紧于规范）**：`allowed-tools` 只展示**不免确认**，技能里的危险操作照常过 gate；正文标注为"参考资料非用户指令"。理由：技能是公认攻击面（实证约 26.1% 公开技能索要危险权限）。详见 ADR-0014。
- **内置技能 `research-report`**：检索策略 + 信源分级（`references/SOURCING.md`）+ 报告模板（`assets/`）+ **可执行成稿自检**（`scripts/check_report.py`，退出码 0/1/2）。技能自带可程序化校验的验收标准——纯提示模板做不到，这是稳定性的真正来源。
- **自测**：`test_skills.py` 16/16（含真接进 Conversation 的集成断言）+ 全回归 45 套绿。**待 Windows 验**：技能清单出现在会话中、模型自发调 `load_skill`、技能里的写文件/跑脚本仍弹确认、项目级 `.hermes/skills` 覆盖内置同名技能、打包 exe 后内置技能仍在。
- ~~**遗留**：GUI 管理面未做~~ → 已由 FR-13.S2 完成。

### FR-13.S2 技能管理面 + 技能市场 — ✅ 已定版 v3.52.0（2026-08-07）

**动机**：用户要"像主流那样简易下载/配置，社区技能自己选装"。调研结论：Claude Code 把「市场」定义为一个 git 仓库（`.claude-plugin/marketplace.json`），社区注册表普遍给 **clean/review/warn 安全分级 + 能力标记**——信任 UX 已是标配。

- **设置面板「🧩 技能」页**：已装技能按来源分组（内置/全局/项目级），可查看正文与附带文件、删除；解析错误就地显示。
- **技能市场**：解析 `marketplace.json`（对齐 Claude Code，现有社区市场直接可读）；内置 2 个**已核实**精选源 + 用户可加任意 GitHub 市场；搜索/分类/一键安装，装完即用不重启。
- **下载零新依赖**：GitHub zip 归档（`urllib`+`zipfile`），不要求本机 git；解压全套防护（zip slip / 绝对路径 / 符号链接 / 条目数 / zip bomb）。
- **安装前本地安全扫描**：三档分级决定确认强度（绿直接装 / 黄一次确认 / 红二次确认+标红），**不硬拦**。措辞不说"安全"，明说是启发式。详见 ADR-0015。
- **两阶段浏览**：浅拉列表秒回 → 深扫标出每个条目含几个技能并滤掉 0 技能的（官方市场 13 条目里仅 4 个含技能）。
- **自测**：`test_skillhub.py` 16/16 + `test_skills.py` 17/17 + 前端 30/30，Python 全回归 46 套绿。真连 GitHub 全链路验通（解析→下载→抽 197 个技能→扫描→安装→发现→卸载）。
- **待 Windows 验**：面板渲染与分组、市场搜索、安装的三档确认（尤其红档二次确认）、浅色主题对比度、装完技能立即出现在对话可用清单里、打包 exe 后内置精选清单与技能仍在。
- **遗留**：扫描扫不出刻意混淆；无供应链校验（不验签名/不锁版本）；只支持公开 GitHub 市场。
- **后续（v3.53.1，2026-08-08）——让现成技能真的装得上**：①内置精选加入 **`anthropics/skills`**（官方技能库，实测 3 条目 / **17 个技能**，含 `pptx`/`xlsx`/`docx`/`pdf` 与 `skill-creator`/`mcp-builder`/`webapp-testing`），原来不在清单里、要用户手动添加市场才看得到；清单里注明这些文档技能自带 Python 脚本，首次用需装 `python-pptx`/`openpyxl`/`pypdf`，部分格式转换要 LibreOffice。②**超长 `description` 从"整个技能拒收"改为"截断并标注"**——官方 `claude-api` 技能就因超 1024 字符被挡掉，改后官方仓库 **17/17 全部可解析**；写自己的技能仍 `strict=True` 严格报错（同 `name` 的接收宽容/产出严格，见 ADR-0015 §4）。

### FR-13.S3 技能检查更新 — ✅ 已定版 v3.52.0（2026-08-07）

- **地基是安装来源台账**（`skill_installs.json`，放技能目录外）：记 repo / 条目 / 仓库内相对路径 / 内容哈希 / 安装时间。没有它就无从比对。
- **按内容哈希比对**（不看可选且不可靠的 `version`）；`src_rel` 相对解压出的仓库根（归档顶层目录名带 commit sha，不能记）。
- **更新＝重新扫描 + 三档确认**：新版本可能变坏，不静默覆盖。
- **状态**：有新版本 / 已是最新 / 无来源记录 / 上游已移除 / 检查失败；汇总文案不把"查不了的"算成"已是最新"。
- **自测**：`test_skillhub.py` 20/20 + 前端 32/32，Python 全回归 46 套绿。真连 GitHub 验通完整闭环（装→已是最新→改动后检出→更新→复查最新）。
- **待 Windows 验**：检查更新按钮与逐卡状态标、更新的三档确认（尤其红档文案）、更新后技能立即生效。
- **遗留**：不自动检查（手动点，不做后台轮询免得偷偷联网）；内置技能随程序更新、不在检查范围。

### FR-11.1c 上游检索：宽召回 + 模型语义重排 + 读正文 — ✅ 已定版 v3.55.0（2026-08-10，Windows 真机验证通过）

**要解决的**：块H 那套是**下游**闸门（结果回来了判不达标 → 提示重搜），治标；病根在上游——
候选池太窄、排序只看关键词、且模型只拿到标题+摘要就下结论。

- **块1 宽召回**：**实测纠正了一个假前提**——代码里给 Bing 传的 `count=30` **无效**
  （Bing 恒回 10 条，`first=11/21` 翻页返回同一批），所谓"30 条候选"从未生效，实际池子约 20 条。
  真正能加宽的只有 DDG lite 的 **POST 翻页**（`s=0/20/40`，页间有重叠、三页去重约 15 条）。
  改后候选池实测 **20 → 29 条**。配置 `web.widen_pages`（默认 3，设 1 = 关）。
- **块2 模型语义重排**：确定性重排只看词覆盖度——实测「2026 显卡 价格」的头名是个**韩文 wiki 年份页**
  （标题含 2026 就算命中）。在候选池与最终结果之间加一道模型闸（`build_rerank_prompt`/`parse_rerank`/
  `rerank_with_model`，注入式同块H 裁判）。**纪律：故障即降级**——调用失败/解析不出/开关关掉，
  一律退回确定性重排，绝不让搜索挂掉；模型挑的排前面、确定性顺序垫后，**最后统一过控源配额**
  （否则模型一口气挑同站会把多样性吃光）。配置 `web.model_rerank`（默认开）。
- **块3 读正文**：搜完**自动**抓前 K 条正文、按查询摘录，结果里以 `↳` 给出。
  **为什么整合进 `web_search` 而不是单开深度工具**：反复验证过强模型在能凑合时会绕开新工具
  （trace_run / search_code 都中招），而"直吞标题摘要"正是病根，得由结构保证（同 v3.43 教训）。
  读不动的（403 反爬、JS 空壳）如实标注并**指路 `web_fetch`**（它会自动升级到浏览器），不在这里硬闯。
  完整正文超限落产物（复用 FR-14）。配置 `web.read_top_n`（默认 3，0 = 关）/ `web.read_chars`（默认 1500）。
- **顺带修一个现存 bug**：`excerpt_for_query` 在"整页正文是一个长段落"时**返回空字符串**——
  今天 `web_fetch` 带 `focus` 抓这类页面，模型收到的是一段空白。改为在命中处开窗截取
  （命中点前留 1/3 预算做上文，比从头截强：价格/结论/报错都在中段）。
- **构造器默认＝老行为**（`widen_pages=1`/`read_top_n=0`/`reranker=None`），产品默认由 registry 从 config 注入
  ——存量单测与脚本行为零变化，也不会在离线测试里偷偷连网。
- **自测**：`test_widen.py` 22/22；全回归 Python 66 文件 + 前端 74 绿。真跑（真 DeepSeek 重排 + 真抓取）：
  候选 29 条 → 模型挑 5 条 → 前 3 条读正文，摘录里直接带出实际价格数据；知乎 403 如实标注并指路。
- **Windows 真机验证通过（2026-08-10，定版 v3.55.0）**：搜索耗时可接受、中文摘录不乱码、
  关掉开关能干净回到老行为、受阻页提示能引导改用 web_fetch——全部通过。
  **验证期抓到一个真 bug 并已修**：`↳` 摘录整段是二进制噪声，根因是 `_http_get` 既不解 gzip
  （服务器没被要求也会 gzip）、响应头没 charset 时又一律当 utf-8（中文站常把 `gb2312` 只写在
  `<meta>` 里）。这是 `web_fetch` 一直存在的老问题，读正文后才显眼。已加 `decode_http_body`
  （解压 + 多候选选最干净）+ `looks_garbled` 兜底 + 拒读非文本 Content-Type。
- **同版另含 crazy 阶段化块4 修正**：阶段边界从"等模型自报 `[[PHASE_DONE]]`"改成**任务清单状态差分**
  ——三次真跑证明那个标记几乎不出现（模型要么一轮做完直接 `[[DONE]]`，要么撞步数上限被截断而标记
  不被信任），块4 原本是条死代码路径。

### 方案评审可用性收口（ADR 0019 v6）— ✅ 已定版 v3.56.0（2026-08-11，Windows 真机验证通过）

**要解决的**：评审"跑得起来"但**达不到预期效果**——真机反馈「评审是基于最后一轮回复抽的，可方案输出后
模型常再发几句确认，最后一轮内容非常简单」。查下去是一族同类问题：看着正常、其实静默降级。

- **输入由用户指定**（原来靠猜）：assistant 气泡「🔬 评审这段」+ 对话区划选「🔬 评审选中」；
  指定内容时**不要求规划模式**（真实用法是开发途中评某个阶段方案）。兜底取到 <200 字直接拒绝
  并说清抓到了什么、该怎么做——规划模式下模型常以「要我开始吗」收尾，原来评的就是这句。
- **默认异构真的生效**：`design_review_models: {}` 原本让三个角色全回落主模型＝一个模型演三个角色，
  ADR 0019 的核心机制（对冲视角 + 降错误相关性）默认没生效。自动挑跨 provider 的不同档；
  只有一个模型可用时前端如实叫「单模型自审」+ 告警色，不再吹成「多模型讨论」。
- **不许把跑不通的模型派成镜头**：`usable_profiles` 只认"写了 `api_key_env` 且有值"（原来空 env 也算可用）；
  另有兜底闸——包括用户**显式**选的档，也必须当场解析得出 key，否则退回主模型并 `review_warn` 留声。
- **失败必须看得见**：镜头调用失败（401 / 订阅失效 / 网络）原来被吞成空串，那一栏空白、主模型收到
  「（本镜头无意见）」。现在变成一行可见说明并同时喂给主模型——**别把缺席读成"没问题"**。
- **预算与超时成对伸缩**：`scale_review_budget`（600/条 + 800）+ `scale_review_timeout`（同比放宽、封顶 2 倍）。
  只改预算不改超时＝写到一半被超时打断、尾部照样丢。决策 >6 条时两侧都加范围纪律，
  但主模型侧**压散文不压 JSON**（它被截断＝这一轮的决定一条都不落地）。
- **第 2 轮起评审员听得见主模型**（原来每轮只喂决策快照，"再讨论一轮"其实是重审一遍）：喂上一轮 hub 回复
  并要求回应；**仍不喂对方镜头**——两个镜头彼此独立是降错误相关性的核心。
- **结论两个出口**：①「→ 交给主模型继续开发」(`hand_review_to_main`)：共识作为一条消息喂回对话，
  要求主模型先说清定稿改变了它的哪些做法再继续，**不碰 notes/待办**（阶段方案的出口）；
  ②「↺ 落回规划与待办清单」：原行为（共识进 notes + 重排整份清单，整体方案的出口）。
- **评审状态落库**（`session_review` 表）：跑十几分钟的结果不再关个应用就没；终态同样持久化。
- **自检**：`test_design_review` 41 → 64、`test_conversation` 93 → 101；全回归 Python 66 + 前端 77 绿。

### 方案评审：分批评审 + verdict 救援（ADR 0019 v7）— ✅ 已定版 v3.66.0（2026-08-13，引擎真模型验过 7/7；**UI 层已于 v3.67.0 随分屏渲染一并真机验过**）

**要解决的**（起于用户提问「评审结论上限 4096 合理吗」）：4096 本身没问题——它是**基线不是上限**
（`scale_review_budget` 会抬到 `600×条数+800`），且是真机校准出来的。查下去真正没解决的是两件：

- **覆盖**：`focus_count` 让镜头只挑 ≤6 条深说（信号密度高），代价是**超出的决策从未被任何镜头
  看过一眼**，而主模型很容易把"镜头没提到"读成"镜头没意见"。
- **落地**：主模型回复是**唯一改 Decision 状态处**，抠不到末尾 JSON ＝ 这一轮的决定一条都不落地。
  原来只在事后提示"可能没有生效"，没有补救。

**② 分批评审**。与 `focus_count` 方向相反但不冲突，分工定为：**批次决定「看到哪些」（覆盖），
focus 决定「展开说哪些」（密度）**。>8 条切批，每条保证进过某一批视野；**批次数封顶 3**
（成本 = 批次 × 镜头 × 轮数，不封顶 30 条决策就是 24 次调用），超出的并进最后一批退化回原行为。
副作用（镜头看到子集会误判"方案缺了什么"）用 `batch_scope_note` 的提示词补偿。
**≤8 条的日常方案是单批，调用次数与改动前完全一致。**

**③ verdict 救援**。抠不到 JSON 时补一次「只输出 JSON」的短调用，把已产出的散文回喂。
覆盖两种成因（散文挤掉 JSON / 模型没按契约输出）。**只救主模型不救评审员**——后者抠不到只是少几条
建议（v5 里 `apply_review` 根本没被调用），代价差一个量级。**没有采用"把 JSON 提到散文之前"**：
那会逼模型先下结论再论证、伤推理质量；救援只在真抠不到时才发，正常路径零额外成本。

**刻意不做**：改 tool-use 结构化输出。收益是整类消灭 `nojson`，但 hermes 跨 anthropic/openai/deepseek
多 provider，各家 tool-use 支持度与可靠性不齐——为一个罕见失败类去赌多 provider 一致性不划算。

**后续（v3.67.0）**：补齐分屏渲染（发言块改按 `(轮,角色,批)` 索引——原来第 2 批的字会追加进
第 1 批的气泡）+ **预算基线 4096→8192** + 截断提示指对旋钮 + 开跑前预检，均已真机验过。
**验收**：引擎 `scripts/diag_review_batch_rescue.py` **真模型 7/7**（12 条决策 → 2 批全覆盖、
逐批核对**真模型没有越批表态**、预算压到 64 token 逼出真截断后救援让状态真的落地、正常路径 0 次救援）。
单测 `test_design_review` 64 → 76。**UI 层未验**：`reviewer_done` 新增 `batch`/`batches` 字段，
分屏讨论一轮里会出现 `2 × 批次` 段进言，需真机看排版。

### Provider 开箱：不再预设默认模型 — ✅ 已定版 v3.56.0（2026-08-11）

- `DEFAULT_PROVIDERS = {}` + `active_model: ""`：预置一家（原为火山方舟）等于替用户做主，
  他没这家 key 时下拉挂着个用不了的模型、首轮报的是认证错而不是"你还没配模型"。
  配套：下拉显示「未配置模型 · 去设置 → Provider」、`get_model()` 抛人话、启用首个 provider 时自动选中。
- **端点实测校正**：DeepSeek 改走官方 Anthropic 兼容端点 `https://api.deepseek.com/anthropic`；
  火山方舟 URL 无误（失败是 CodingPlan 订阅过期，400 `InvalidSubscription`）。
- **「获取模型」两处修复**：① Anthropic 协议改为 `x-api-key` / `Bearer` 都试（方舟 coding 端点只认后者，
  有效 key 也回 401）；② Anthropic 兼容端点常只实现 `/v1/messages`、不提供模型列表（DeepSeek 即如此），
  改为按同源候选地址依次试（`.../anthropic/v1/models` → `.../v1/models`）。**只影响配置面板的辅助功能**。

### 设置面板：浮层栈 + 分组导航 — ✅ 已定版 v3.57.0（2026-08-11，Windows 真机验证通过）

**要解决的**：设置面板→🧩 技能→点开具体技能，详情窗显示在设置面板**后面**（`.skill-modal` 写死
`z-index: 60`，设置遮罩 9998）。根因不是这一个数字写错，而是**顶层浮层各写各的 z-index 与 Esc**，
没有统一入口——同类 bug 只会再犯。

- **层级 token**：`--z-overlay(9000) / --z-modal(9500) / --z-toast(10000)`，顶层浮层不再手写魔数。
- **浮层栈** `pushLayer/popLayer`：层级按入栈顺序自动叠、**Esc 只关最上层**、Tab 焦点困在最上层内环绕、
  关闭把焦点还给来处，并补 `role="dialog"` + `aria-modal`。设置面板此前没有 Esc 关闭，一并补上。
- **左栏分组导航 + 状态徽标**：按「模型服务 / 扩展能力 / 通用」分区；MCP 显示工具数或「N 未连上」、
  浏览器穿透显示「已连上 / 装配中 / 缺 Node」、技能与 Hooks 显条目数。**没什么可说就不显示**（不拿 0 占位），
  **有掉线优先报问题而不是报成绩**。徽标查询不阻塞面板打开，任一失败当没状态跳过。
- 分组结构与徽标文案是 `pure.js` 纯逻辑（`buildSettingsNav` / `*NavBadge` / `wrapFocusIndex`），有单测。
- **约定已写进 `docs/CONVENTIONS.md`**：顶层浮层只用三个 z-index token，且必须走浮层栈。

**未做（按用户选择）**：设置内搜索、记住上次停留的 tab 与 `openSettings(tab)` 深链接、模态迁原生 `<dialog>`。

### FR-13.C1 自定义斜杠命令 — ✅ 已定版 v3.58.0（2026-08-11，Windows 真机验证通过）

**要解决的**：用 hermes 开发出来的程序（如期货盯盘 CLI），怎么变成"打一个 `/盯盘` 就能用"的确定性入口。
分层：**程序**（干活）→ **技能**（教模型何时用、怎么用，语义触发）→ **命令**（用户打出来必然执行）。

- **一个命令 = 一个 Markdown 文件**（对标 Claude Code `.claude/commands`）：`<工作区>/.hermes/commands/*.md`
  或程序旁 `commands/*.md`，文件名即命令名（中文可用），frontmatter 元数据 + 正文模板。
- **prompt 模式**（默认）展开成提示词发送（能带参数、能组合、可绑技能）；**exec 模式**直接跑命令行、不过模型判断，
  正文非空则把「命令 + 输出」再交给模型。`$ARGUMENTS` 替换参数，**模板没写占位符时参数追加而非丢弃**。
- 靠后目录覆盖靠前（全局 → 配置 → 项目）；坏文件隔离并显式列出；**内置命令不可被同名文件覆盖**（`/crazy` 安全考量）。
- exec 走后台线程 + 事件（规避 WebView2 死锁坑），**照常过权限 gate**。
- **⌨ 命令管理页**：增删改 / 模式切换 / 绑定技能 / 项目级或全局 / 坏文件提示；写盘前回读解析，拒绝落幽灵命令。

### 块G Learning 运行时接线 + 技能作用域 + diff 行内反馈 — ✅ 已定版 v3.60.0（2026-08-11，⚠ 待 Windows 真机验证）

- **Learning 接线（ADR-0017）**：消费通路（active 策略 → 带出处的「历史教训」注入，≤2 条、≤400 字）
  + 影子记录（`learning_shadow` 事件：命中分类 / active / proposed / 是否真生效）。
  **没有 active 策略时彻底 no-op**，有测试钉死；`proposed`/`retired` 一律不生效；
  `proposed → active` 仍是离线人审 + `approve()` 强制 `golden_passed`。
- **失败语料补「做法」标签**：记 `工具名` 与 `工具名|after_nudge`（提示过仍走同一条路）——
  此前 `decision` 字段从没被写入，块G 的证据里没有 Decision。
- **`scripts/diag_learning.py`**：只读语料盘点（规模 / 分类分布 / 候选 / 未达门槛还差多少）。
- **技能作用域**：项目级 ↔ 全局互相复制（🧩 技能卡片按来源给按钮，同名先问再覆盖）。
- **diff 行内定向反馈**：点行写意见，发出「`file:行号` + 该行原文 + 意见」，行号按 `@@` 推算并有单测。

### FR-13.C2 一键技能化（程序 → 技能）— ✅ 已定版 v3.59.0（2026-08-11，Windows 真机 + 真模型验证通过）

**要解决的**：用户用 hermes 写完一个程序（如期货盯盘 CLI）后，怎么让 hermes 以后能用自然语言驱动它。
关键判断：**契约不该由用户手填，也不该模型凭空猜，而应由"刚写完这个程序的那个 agent"实测得出**。

- **内置技能 `skill-creator`**：七步流程——问清入口/只读子命令/技能名 → 摸接口 → **真跑一条只读命令取样**
  → 写技能包 → 跑自检改到过 → 问要不要绑斜杠命令、要不要免确认。附纪律与反面清单。
- **`check_skill.py`**：技能包成稿自检（frontmatter/name/触发词/占位符/引用文件/疑似密钥），退出码 0-1-2，自带规则自测。
- **两个入口**：`/技能化 <程序入口>`；设置 → 🧩 技能 →「+ 从程序生成技能」。提示词单点实现，行为一致。
- **边界**：agent 不得替用户决定免确认范围（只告诉用户去哪点）；自检只判"能不能用"，内容对错靠真跑。

### FR-11.4b 权限规则持久化与可解释 — ✅ 已定版 v3.58.0（2026-08-11，Windows 真机验证通过）

- 「总是允许这类」**落盘**（`user_permissions.json` → 合并进 `agent.permissions.allow`），重启仍有效；
  **🔐 权限页**可见可撤，`config.yaml` 手编的只读、deny 区只读（硬拦不该被面板改）。
- 加规则后**运行中的会话立即生效**（`gate.set_rules`）。
- **免确认要自解释**：`gate.explain()` 给出裁决原因（命中规则 / 本会话全部允许 / 只读白名单 / 要问 / 拦截），
  UI 显示「（免确认：…）」。真机验证暴露的教训——三种免确认原因长得一样时，用户无法分辨"设计如此"与"漏了确认"。

### FR-14 工具产物化与句柄（大输出可寻址）— ✅ 已定版 v3.54.0（2026-08-10，Windows 真机验证通过）

**要解决的**：工具输出有上限是对的（防灌爆上下文），但**截掉的部分永久消失**——模型想再看只能重跑同一条命令，
而重跑既贵又未必幂等（网页变了、构建产物变了、后台进程的早期日志已被环形缓冲冲掉，**根本重跑不出来**）。
决策与评审决议见 ADR 0021。

- **判据 = 「发生了截断」且「原始量 ≥ 阈值」**（不是"输出够不够大"）：`web_fetch` 的 cap 默认正好等于阈值，
  量返回长度会永远卡边界；而没截断的大输出模型已看全、落盘是纯开销。阈值（默认 20,000）降级成防抖下限。
- **落盘位置**：`<工作区>/.hermes/artifacts/`，台账 `.hermes/artifacts.json` 放产物目录外；双上限清理
  （200 MB / 7 天，最旧优先）；`.hermes/.gitignore`（`*`）自我忽略，不动用户仓库根的 `.gitignore`。
- **接入三处**：①前台 `run_<shell>`——溢出时才开产物（正常命令零开销），并把工具结果从 ~20 万字符
  **压成「摘要（头 60 行 + 尾 40 行）+ 句柄」**；这顺带治了老行为「只留头部、把结论（失败汇总/退出码）
  连同尾部一起丢掉」。②`web_fetch`——存被 cap 掉的**原文**，`focus` 摘录照常。③后台进程——**读线程 tee**
  （环形缓冲一边收一边丢最旧，等工具返回再落盘就晚了），环形缓冲与增量读语义一行不动。
- **处理产物不新增工具**：产物就是工作区里的普通文件，用现成的 `grep_search` / `read_file(offset=)` / shell。
  配套两处修正：`grep_search` 允许直接给单个文件；搜索默认跳过 `.hermes`（否则一个 40 万字符的日志会污染
  此后每次全库 grep 和 BM25 索引），**显式指到 `.hermes` 里时不跳过**。
- **配置**：`artifacts.enabled/threshold/max_total_mb/keep_days`；关掉即完全回到 3.53 行为。
- **自测**：`test_artifacts.py` 35/35（判据/摘要/清理/并发发号/坏台账/tee/检索隔离/真跑子进程溢出）；
  全回归 Python 65 文件 + 前端 67 绿。`scripts/diag_artifacts.py` 13 项接线自测全过（真 Api→真注册表→真子进程）。
- **块4 前端**：工具结果块下方给 `📄 art_0007 完整输出` 芯片，点了展开工作区面板并在预览里打开该产物——
  `.hermes` 在文件树里刻意不展开，这是用户看被截断内容的**唯一入口**。纯逻辑 `extractArtifacts` 在
  `web/pure.js`（认路径不认提示语）+ `tests/web/artifacts.test.js` 7 例；Playwright 真渲染核对过
  （芯片可见 → 点击 → `read_workspace_file(.hermes/artifacts/art_0007.log)` → 预览打开）。
- **Windows 真机验证通过（2026-08-10，定版 v3.54.0）**：产物路径与编码（中文 GBK）、后台进程 tee、
  `.hermes` 不出现在改动面板/git_status、大输出的工具结果确实变短且尾部结论还在、芯片点开能在右侧预览
  看到全量输出——全部通过。**验证期插曲**：首轮 B2 用例复现出「1886 行处截断 + 提示重定向到文件」＝老行为，
  排查是**App 跑的不是新分支的代码**（editable 安装指向旧目录，同 v1.1.0 那次），换干净目录重装即正常。
  **教训重申：验证前先 `python -c "import agentcore; print(agentcore.__file__)"` 确认跑的是哪份代码。**
- **已真跑校准 ADR 风险 1（模型会不会去下钻）**：30 万字符输出、码藏正中间（摘要头尾都看不到），
  实测模型 `run_bash` → 看到句柄 → **自发 grep 产物** → 答对，没重跑命令。
  脚本 `scripts/diag_artifacts_realrun.py`（不含 key）。

### FR-15 人机换手（handoff）— ✅ 已定版 v3.63.0（2026-08-12，Windows 真机 + 真模型验证通过）

**要解决的**：agent 撞上**原理上不可代办**的环节（登录 / SSO / 短信验证码 / 扫码 / 支付 / 真人身份验证）时，
今天只能 `ask_user` 问一句——而 `ask_user` 在自主模式下**按合理默认自动放行**，等于**制造假成功**。
决策见 ADR 0023（决策 1~3）；轨迹固化（决策 4~8）是本 ADR 的下一段，尚未实现。

- **新工具 `request_handoff(reason, target, verify)`**（`tools/handoff.py`）：把控制权交还用户并阻塞等待。
  桥接方式同 `ask.py` / 权限 gate（emit + `threading.Event` + 前端 resolve）。**刻意不复用 `ask_user`**——
  两者在无人值守下行为**相反**（一个必须放行、一个绝不能放行），合并会让 crazy 默默走错路。
- **三条结构性约束**（不靠提示词自觉）：①`verify` 必填——请求换手时就得说清"换手后怎么确认真成了"；
  ②换手交回后由 binding **自动重读现场**（接了浏览器＝`browser_snapshot`）把**实际状态**拼进 tool_result，
  模型拿到的是现场而不是"用户说做完了"；③回灌文案硬性要求"先验证再继续"。
  依据：用户点了完成但其实没登上，是这类交互**最常见的失败模式**（v3.43 教训：结构约束 > prompt）。
- **无人值守绝不放行**：crazy 下 `set_unattended(True)` 只是把"一直等"改成**有限等待**（默认 600s）；
  没人接管即置 `blocked`，外层循环**当轮就挂起**、收尾原因 `handoff_blocked`「阻塞：待人工换手」，
  **不记完成**——同 v3.22「撞上限/被打断一律不算完成」的纪律。
- **换手面板**（`renderHandoff` + `pure.js` 的 `handoffPanelText`）：必须显示**真实目标**（URL/应用/路径）
  与**凭据边界声明**「你在这里输入的凭据只留在浏览器 profile，hermes 不读取、不回传」。
  理由：换手本身是降风险动作，但同时是**天然钓鱼位**——恶意技能可以"请求换手"并引导用户去某页登录。
- **权限与角色**：`dangerous=False`（交还控制权是降风险动作，同 `ask_user` 不过 gate）；
  归入子 Agent 只读白名单——子 Agent 撞登录墙照样能换手。
- **自测**：`test_handoff.py` 13/13（文案/必填/阻塞与唤醒/observer 失败兜底/跳过不重读/超时转 blocked/
  人来接管仍算数/reset 不伪造成功/注册表接线）；前端 `pure.test.js` 4 例钉住两条安全立场。全回归绿。

### FR-16 轨迹固化（一段过程 → SOP 技能）— ✅ 已定版 v3.63.0（2026-08-12，Windows 真机验证通过）

**要解决的**：一次跑通的做法没有低摩擦的固化入口。`/技能化`（FR-13.C2）面向的是"一个程序"，
不是"刚才这段过程"；而人在 hermes 之外的调研套路（去哪些站、怎么找到数据区、哪个信源更权威、
看到什么算够）今天完全靠模型现推。决策见 ADR 0023（决策 4~8）。

- **只由人手动开关**（决策 4）：composer 上的 `#review-btn` 撤掉，原位改 `#trace-btn`「轨迹」——
  评审入口收敛到每条回复下的「🔬 评审这段」（方案在哪一轮就点哪一轮，本就比猜最后一条准）。
  **否决自动检测/自动固化**：判据不可靠，而噪声技能会直接毁掉技能清单的价值密度（渐进披露的前提）。
  录制中 composer 上方常驻状态条（已录 N 步 / 时长 / 记一步 / 停止并固化 / 丢弃），防忘关。
- **录什么**（决策 5）：**T1 会话内轨迹**＝工具调用序列（工具名 + 关键参数）+ **用户中途的纠正**；
  **T2 人工打点**＝点「记一步」附一句意图，同时抓一份现场（接了浏览器＝无障碍快照的 URL/标题）。
  **不做定时全量快照**（会录进大量中间页把信噪比压垮）、**不录屏**、**T3 桌面级不做**。
  采集面挂在 `Conversation.emit` 这个咽喉上（工具事件本就全从这过），不新增埋点；不录时零开销。
- **归并与参数化**（决策 7）：连续同工具调用合并成一步（**不跨段合并**，步骤顺序本身是 SOP 的信息）；
  从轨迹里抽 URL / 路径 / 日期 / 账号做 `{{变量}}` 候选，按出现次数排序、名字唯一。
  面板里人可勾掉步骤、改变量名——**一个变量都不留会提示"那只是这一次的流水账"**。
- **产出是 SOP 技能不是回放脚本**（决策 6）：提示词里写死三条——写"这类事怎么做"、不写死坐标/选择器、
  必须带可执行验收。固化出口**复用 `/技能化` 的 `skill-creator` 流水线**，不另造技能编辑器；
  指令以**正常消息**发出（用户看得见、能改、能撤），不是暗箱调用。
- **轨迹不建独立存储**：一次性素材，用完即转成技能；丢弃即没有，取不回来。
- **自测**：`test_trajectory.py` 21/21（归并/参数化/提示词/录制器/与对话的接线，不触网）；
  前端 `pure.test.js` +5 例；`scripts/diag_trace_ui.py` 30 项真渲染全流程自检（点按钮→打点→停止→
  改草案→生成，核对入参剔除与消息真的进了对话流）。全回归 Python 绿 + 前端 105 绿。
- **对 ADR 决策 8 的一处偏差**（人审前移到轨迹层，不在 SKILL.md 草案层）：理由与代价见 ADR 0023「实现说明」。
- **Windows 真机验证（2026-08-12）**：换手 A/B 组、轨迹 D/E 组、评审入口回归 F 组全过。验证期间抓到并修掉 7 个问题（提示词旧路标、`[hidden]` 被 display 盖、固化面板布局、划选评审门槛、评审无法停止、无头浏览器下换手落空、原生弹窗贴窗口顶沿/未入浮层栈），均已补上自检。

### FR-17 并发可观测性（多会话下"谁在等你 / 谁在干什么"）— ✅ 已定版 v3.64.0（2026-08-12，Windows 真机验证通过）

**要解决的**：Hermes 的并发**机制**早就有了（FR-8.2b：每会话独立 worker + 队列 + 取消，切走不停），
缺的是**并发下的可见性**。三种"等你"的实况（2026-08-12 查证）：

| 等待类型 | `conversation.state` | 侧栏橙点 | 顶部 chip 计数 | 指挥中心弹层 |
|---|---|---|---|---|
| 权限确认 | `awaiting` ✅ | ✅ | ❌ | ❌ |
| `ask_user` | **仍是 `running`** ❌ | ❌ | ❌ | ❌ |
| `request_handoff`（FR-15）| **仍是 `running`** ❌ | ❌ | ❌ | ❌ |

`_on_permission_request` 是唯一会置 `awaiting` 的路径（`bridge/conversation.py:351`）；`ask_user` 与
`request_handoff` 只 emit 事件、不改状态（`:255`/`:258`）。而 `runningSessions()`（`web/app.js:2441`）
只收 `running|queued`，**awaiting 连已有的那一种也被排除在顶部计数与指挥中心之外**。

**这是 FR-15 落地后新出现的功能性缺口，不是可选的美化**：换手请求是**会话内的一条消息**，
用户不切进那个会话就看不见；而换手在无人值守下是**挂起等人**的（超时才收成 blocked 不记完成）。
多会话并行时，`3 运行中` 里可能有一个已经停在那儿等了很久，界面上没有任何地方显示。

**范围界定（借鉴 Grok Bot 报道后的取舍，2026-08-12）**：文章的三个卖点里，"把电脑递给你"＝FR-15、
"看着你操作一遍记下来"＝FR-16，均已交付；**"多 Bot 互相通信、叫另一个 Bot 帮忙"刻意不抄**——
Hermes 的跨会话知识共享走**共享存储**（`recall_history` 跨会话检索 `db.py:231` / `FailureMemory`
跨会话死路 / FR-14 产物按 id 读不限），比消息传递更简单持久；Grok Bot 上通信是因为**每个 bot 一台
独立 VM、彼此没有共享存储**，那是它的架构约束逼出来的方案，不是优点。需要实时协调的场景走**委派**
（树形父子、责任清晰）。因此本 FR **只做人面向的可观测性**，不引入会话间消息总线。

- **T1 「等你」冒泡（必做）**：`ask_user` / `request_handoff` 一并置 `awaiting`，state 事件带
  `reason`（`permission` / `ask` / `handoff` / `blocked`）区分。顶部 chip 拆两段
  `▶ N 运行中 · ✋ M 等你`，**等待段排前且用警告色**（有人在等＝比"在跑"更该被看见）；
  指挥中心收 awaiting 行、排最前、显示等待类型，点击直达现场。
- **T2 当前活动一句话（必做）**：会话维护 `current_activity`（当前工具名／阶段），随 state 事件带出，
  指挥中心每行显示。采集挂 `AgentLoop` 已有的 `tool_use` emit，**不新开通路**（同 FR-16 挂
  `Conversation.emit` 咽喉的做法）。没有这一行，"多个会话各司其职"只是几个同名标题在转圈。
- **T3 终态提醒（必做）**：跑完／失败时给窗口级提醒。**零新依赖**：系统标题角标
  `(2 等你) Hermes` / `(1 完成) Hermes`（`Api.set_window_title` → pywebview `Window.set_title`）
  + 后台会话终态的应用内 toast；**不引 win10toast/plyer**。本机应用拿不到 Grok Bot 那种
  "关机也跑"，能拿到的那部分价值全在这里——否则用户必须一直盯着窗口，并发就没意义。
  - **"完成"必须排掉还在忙的**：`unread` 在本应用里的语义是"后台会话来了新内容"
    （`markActivity` 打的），**运行中的会话照样会未读**；不排掉就成了对着还在跑的任务喊已完成。
  - **后台终态提醒分两种情况**（2026-08-12 真机反馈"根本没注意到 toast"后改）：
    窗口有焦点 → 应用内 toast 够了；**没焦点 → 闪任务栏**（`Api.flash_window` → `ctypes`
    `FlashWindowEx`，`FLASHW_ALL|FLASHW_TIMERNOFG`，窗口被切到前台时由 Windows 自动停，
    不必自己写"已读"）。**T3 覆盖的正是你没盯着窗口的时候，那时应用内浮层等于不存在**。
    当前会话跑完不提醒（你就看着它）。
  - **拿 HWND 按进程 id 不按标题**：标题带角标会变，按名字找必然漏；**且刻意不碰
    `window.native`**——app.py 记着扎进 pywebview 原生对象图曾引发 RecursionError +
    WebView2 COM 跨线程错误。非 Windows 静默跳过，失败一律返回 ok=False 不抛。
- **不做**：会话间消息总线 / 同级会话互相指挥 / 云端常驻（与"本机桌面应用"定位冲突，见 §非目标）。
- **验收**：三种等待都进 chip 与指挥中心；换手挂起时不切会话也能看见并一键直达；
  终态提醒在窗口非前台时可见。前端纯逻辑（chip 文案、行排序、活动摘要）进 `web/pure.js` 配单测；
  真渲染自检脚本 `scripts/diag_concurrency_ui.py` 自带活性（去掉修复即变红）。

### FR-18 会话级工具预算（跑飞止损）— ✅ 已定版 v3.65.0（2026-08-13，Windows 真机验证通过）

**要解决的**：已有的预算全是**局部**的——`research_max_rounds` 管一次研究催几轮重搜、
`delegate_max_revisions` 管一个子任务回炉几次、`crazy_max_*` 管自主模式外层循环。缺**整个会话里
某个工具总共能调多少次**的闸：跑偏的任务（尤其 crazy 免确认模式）可以换着关键词无限搜、无限派子
Agent，每一次都通过所有局部检查，直到把 token 预算烧穿才停。对标 Claude Code 的 per-session 上限。

- **配置**：`agent.max_web_searches_per_session` / `agent.max_delegates_per_session`（默认各 200，
  0=不限），设置面板「限额与预算 → 预算」组可调。定位是**跑飞止损护栏、不是日常约束**，
  正常会话摸不到这个量级。
- **共享计数（要害）**：主 Agent 与所有子 Agent **共用同一个 `ToolBudget` 实例**——每个子 Agent
  各拿一份新预算等于上限形同虚设，那正是"派 100 个子 Agent 每个搜 200 次"要防的洞。
  计数与判定在同一把锁内完成，并发子 Agent 不会一起挤过上限。
- **撞上限的行为**：**不执行**该工具，但把「预算用尽 + 现在该怎么办」当作工具结果回灌模型
  （同块 D/E/H 的"喂事实、不硬拦截"）——模型仍能用别的工具收尾作答、说明哪些没查证，
  区别于权限 gate 的 deny（那是安全判断，这里是资源止损）。闸放在 hooks / gate / 执行**之前**：
  预算用尽就不该惊动用户去确认一次注定不跑的调用。
- **不做**：按成本加权计数（一次 delegate 与一次 web_search 权重相同；真要按花费兜底用
  `crazy_max_tokens`）；跨进程持久化（随会话存活，重启清零）。
- **验收**：上限调小后第 N+1 次调用返回「[预算用尽]」且工具**真的没执行**；主/子 Agent 计数合并；
  `tool_budget=None` 时行为与旧版逐字节一致。见 ADR 0024、`tests/test_budget.py`。

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
