# ADR-0005: 工具系统、Agent 主循环与权限 gate

- 状态：已接受
- 日期：2026-06-09

## 背景
P3 要让模型真正读写本地代码、跑命令（PRD G3）。需要：工具系统、tool-use 的
agent 循环、危险操作的权限控制。两个 provider（Anthropic / OpenAI 兼容）的工具
调用格式不同，须在适配层归一。

## 决策
- **统一工具 schema**：Tool 三要素 `name / description / input_schema`（JSON Schema），
  兼容 MCP 习惯。registry 产出 **Anthropic 原生格式**作为内部规范；OpenAI provider
  内部把 schema 与 content blocks 转成 function-calling 形状。
- **内部消息规范**：`Message.content` 升级为 `str | list[dict]`，工具往返用
  Anthropic 风格 content blocks（`tool_use` / `tool_result`）作为单一内部表示。
- **Agent 主循环**（`agent/loop.py`）：plan→act→observe。每轮调 provider，收集
  tool_use → 执行 → 把 tool_result 回灌 → 下一轮；`max_steps` 防死循环。
- **权限 gate**（`agent/gate.py`）：危险工具（write_file / edit_file / run_*）执行前
  阻塞确认，交互为**逐次确认 + 本会话「全部允许」**（threading.Event 协调
  send_message 工作线程与前端 resolve 回调）。
- **平台隔离 & 配置驱动**：shell 工具独立成 `tools/shell.py`，默认 PowerShell，
  shell/超时/工作区/步数上限都来自 `config.yaml` 的 `agent` 段。
- **工作区沙箱**：所有文件工具路径解析后必须落在工作区内，越界拒绝。

## 备选与权衡
- 让每个 provider 各自定义工具格式：调用方要分叉处理 —— 否决。统一为 Anthropic
  原生、在 OpenAI 侧转换，调用方零感知。
- 权限用纯配置白名单（无实时弹窗）：粒度粗、对一次性危险命令不安全 —— 否决，
  改用逐次确认 + 会话级全允许，兼顾安全与效率。
- 全自动批准：违背 PRD FR-3.3「危险操作需确认」—— 否决。

## 结果
- 模型可读/写/编辑文件、grep/glob 搜索、跑 PowerShell；危险操作有 gate。
- 新增工具 = 一个 Tool 子类 + 注册，provider 与 UI 零改动。
- OpenAI 兼容模型的 tool-use 由适配层消化，调用方与 Anthropic 路径一致。
- MiniMax-M2.7（Anthropic 兼容）的 tool-use 实测可用性留待 Windows 验证确认。
