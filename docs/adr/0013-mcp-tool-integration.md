# ADR-0013: MCP 工具接入（作为客户端）

- 状态：已接受
- 日期：2026-06-09

## 背景
PRD FR-6.4 要求接入 MCP（Model Context Protocol）工具，让 hermes-dev 能复用生态里现成的
MCP server（filesystem / git / 数据库 / 各类第三方），而不必为每种能力自己写工具。
CONVENTIONS 早就把内置 `Tool` 定义为 `name/description/input_schema` 三要素以兼容 MCP——
本 ADR 把它兑现：hermes-dev 作为 **MCP 客户端**连接 server，把其工具收进 Agent 工具循环。

## 决策
- **仅 stdio 传输、仅 tools**（本期范围）：连接本地子进程 server（npx / uvx / python 启动），
  只接 server 的 tools；HTTP/SSE 远程传输与 resources/prompts 延后。
- **官方 `mcp` Python SDK**：协议握手/编解码交给 SDK，不手搓 JSON-RPC。新增依赖 `mcp>=1.2`。
  默认 `mcp.enabled=false`，且 SDK 仅在 manager 方法内**惰性导入**——未装 SDK 时只要不开启
  MCP，应用照常运行。
- **同步内核 × 异步 SDK 的桥接**：内核（pywebview / AgentLoop / provider）全同步，SDK 是
  asyncio。`McpManager` 起**一个常驻后台线程跑事件循环**，所有 MCP 操作用
  `run_coroutine_threadsafe(...).result(timeout)` 从同步侧阻塞获取。`McpTool.run()` 因而对
  AgentLoop 是普通同步函数。
- **每 server 一个常驻 `_serve` 协程**：stdio_client / ClientSession 的 async context 必须在
  **同一个 task 内进入与退出**（否则 anyio 报 "cancel scope in different task"）。故 `_serve`
  在自己的 task 里：进入 context → initialize → list_tools → 把工具经 `concurrent.future`
  交回主线程 → `await stop_event` 保活；`close()` 置位 stop_event，`_serve` 在原 task 干净退出。
  工具调用 `call_tool` 作为另一协程跑（只用 session 内部流，不碰 cancel scope，安全）。
- **外部工具默认危险**：MCP 工具能力未知（可能读写文件、联网、执行命令），故默认
  `dangerous=True`，逐次过既有权限 gate；server 配 `trust:true` 才免确认。延续项目
  「危险操作要确认」的姿态。
- **命名防撞**：工具名 `服务名__工具名`（双下划线，整体满足 Anthropic 工具名约束
  `[a-zA-Z0-9_-]{1,64}`）；`McpTool` 内部记住原始名，调用时还原。
- **结果转换**（纯函数 `convert_result`，可单测）：TextContent→文本；ImageContent→image 块，
  走截屏那套「同消息并列块」（ADR-0010）；EmbeddedResource/其它→文本占位；`isError`→ok=False
  并以 `ToolError` 回灌模型（AgentLoop 不中断）。
- **故障隔离**：某 server 启动/握手失败只记日志并跳过，不抛、不影响其它 server 与启动；
  运行中调用失败（子进程已退出/超时）转成 `ToolError` 回灌模型。
- **生命周期**：在 `Api.__init__` 连接、收集工具传给 `build_registry`；窗口关闭后
  `app.py` 调 `Api.close()` → `McpManager.close()` 终止子进程、停 loop。

## 备选与权衡
- **手搓 stdio JSON-RPC（零依赖）**：省一个依赖，但 MCP 握手/通知/内容 schema 复杂，易与各
  server 不兼容——否决，用官方 SDK 更稳。
- **每次调用临时连一次 server**：免维护常驻 session，但每次调用重启子进程、丢失 server 内状态、
  延迟高——否决，保持常驻连接。
- **MCP 工具默认免 gate（像内置只读工具）**：更顺手，但外部 server 能力不可控，安全姿态差——
  否决，默认危险 + `trust` 选项。
- **把子包命名为 `mcp`**：与 SDK 顶层包同名，虽然绝对导入不冲突，但易混淆——改名 `mcp_client`。
- **async 用 `asyncio.run()` 每次新 loop**：与常驻 session 不兼容（session 绑定 loop）——
  否决，用常驻后台 loop。

## 已知限制
- 仅 stdio + tools；远程（HTTP/SSE）server 与 resources/prompts 暂不支持。
- server 需用户本机自备运行环境（Node 的 npx / Python 的 uvx 等）；缺环境则该 server 被跳过。
- 工具列表在启动时拉取一次；server 运行期动态增删工具（list_changed 通知）暂不处理。
- 连接是阻塞式（启动时逐个连，受 `connect_timeout` 约束）；server 很多或很慢会拖慢启动。
- 安全边界依赖权限 gate：`trust:true` 的 server 工具不弹确认，用户需自行确保该 server 可信。

## 结果
- 一行配置即可把生态里的 MCP server 工具接进来，模型像用内置工具一样调用（名带 `服务名__` 前缀）。
- 同步内核与异步 SDK 干净桥接；连接/列表/调用/错误/关闭已用真 Python stdio server 端到端验证，
  含坏 server 跳过的故障隔离。纯逻辑单测 12/12。
- 与既有工具/权限/图片并列块机制正交复用，未改 AgentLoop 与 provider。
