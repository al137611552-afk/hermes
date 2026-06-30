# ADR-0010: Agent 主动截屏工具

- 状态：已接受
- 日期：2026-06-09

## 背景
P5（PRD FR-5.1）原计划做「全局热键截屏 + 区域选择」喂给模型。评审发现：人工截图
场景下，Windows 自带 `Win+Shift+S` 已能截图到剪贴板，再经 P4 的粘贴链路 + 视觉模型
（ark-kimi）即可识图——自建截图 UI 基本是在复刻系统工具，价值有限。

真正不可替代的是**让 Agent 自己截屏**：模型在工具循环里主动调用，无需用户手动截/贴，
支撑「看下我屏幕上哪里报错，直接帮我改」这类自主任务。故 P5 重定为：放弃手动截图 UI，
只做一个 `take_screenshot` 工具。

## 决策
- **截屏作为工具进 Agent 循环**：`ScreenshotTool`（name `take_screenshot`），模型按需调用。
- **平台代码隔离 + 优雅降级**：用 Pillow `ImageGrab.grab()` 截全屏（唯一平台相关代码，
  集中在 `tools/screenshot.py`）。无图形界面/无授权时抛 `ToolError` 回灌模型，不崩。
  截图长边缩到 1568px 以内，控制 token 与传输体积。
- **隐私敏感 -> 危险工具**：`dangerous=True`，每次执行前过既有权限 gate 由用户授权；
  另有 `config.agent.screenshot` 总开关（false 则根本不注册该工具）。
- **图片注入方式：tool_result 的同消息并列块（关键）**。实测火山方舟端点**不解析
  `tool_result` 内嵌的 image**（模型反馈「看不到截图」）。但把 image 作为**并列块**
  放进 tool_result 所在的同一条 user 消息（`content = [tool_result, image, ...]`），
  模型即可正常识图。为此扩展工具框架：工具可返回 `ToolOutput(text, blocks)`，agent
  循环把 `blocks` 追加到本轮 tool_result 的 user 消息里。普通工具仍返回 `str`，向后兼容。
- **前端**：`tool_result` 事件带 `image`（data url）时，在工具块内展示缩略图。

## 备选与权衡
- **手动截图按钮 / 全局热键 / 区域框选 UI**：与系统 `Win+Shift+S` 重复，且需更多 Linux
  测不了的平台代码——否决。区域裁剪等后续若需要，可在前端用 canvas 对已截全屏裁剪实现。
- **image 直接塞进 tool_result.content**：最符合 Anthropic 语义，但本端点不解析——否决。
- **截屏后追加一条独立 user 消息带图**：也实测可行，但产生连续两条 user 消息、结构更乱；
  选「同消息并列块」更紧凑、保持工具往返结构。

## 已知限制
- 需**视觉模型**（如 ark-kimi）才能理解截图；非视觉主模型（如 MiniMax-M2.7）调用本工具
  会拿到看不懂的图。第一版不按模型能力自动过滤，仅在工具描述里标注。
- 截图含 base64 会随会话落库，长期可能让 DB 膨胀（与 P6.1 已记的图像存储问题同源）。
- `ImageGrab` 截全屏会包含 Hermes 窗口本身；多显示器行为依赖 Pillow 默认。
- 平台截屏真机行为（Windows 权限、多屏）需真机验证，开发机无显示只能验证降级路径。

## 结果
- Agent 可在循环中主动截屏看屏，复用现有权限 gate 与图像识别链路。
- 工具框架获得返回富内容块（image 等）的通用能力（`ToolOutput`），为后续工具复用。
- 真实端点（ark-kimi）端到端验证：模型调用 take_screenshot → 收到截图 → 准确描述内容。
