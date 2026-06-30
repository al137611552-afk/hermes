# 架构说明（Architecture）

> 概览见 PRD；本文件描述运行时结构与扩展点。最后更新 2026-06-08。

## 分层

```
┌─ 交互层 (web/)  pywebview 窗口内的 Web 前端
│   多模态输入 / 流式渲染 / 模型切换
├─ 桥 (bridge/api.py)  JS <-> Python，事件流推送
├─ Agent 内核 (agent/)  对话循环 + 权限 gate（P3）；上下文/记忆规划中
├─ 模型适配层 (providers/)  统一 BaseProvider 接口 + tool-use（P3）
├─ 工具系统 (tools/)  文件/shell/搜索（P3）；MCP 规划中
├─ 多模态 (multimodal/)  图片/文档 -> 统一内容块（P4）；截图规划中（P5）
├─ 持久化 (store/)  会话历史 SQLite（P6.1）
└─ 配置 (config.py)
```

## 当前已实现的数据流（P1）

```
用户输入
  → web/app.js: api.send_message(text)
    → bridge/Api.send_message(): 入 history
      → providers.build_provider(config, active_model)
        → BaseProvider.stream_chat(messages, system)
          ← StreamEvent('text'|'done'|'error')
      → Api._emit() → window.evaluate_js('__onAgentEvent')
  → web/app.js: 累积文本 + renderMarkdown 流式渲染
```

## 关键扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新模型厂商 | `providers/` 加实现 + 在 `__init__.py` 注册 |
| 新工具 | `tools/` 实现统一 schema + 注册（P3） |
| 新流事件类型（如 tool_use） | `providers/base.py:StreamEvent` + 桥 + 前端 `__onAgentEvent` |
| 多模态输入 | `multimodal/` 转换为 `Message.content` 内容块（P4） |
| 会话持久化字段 | `store/db.py` 的表结构 + `Store` CRUD（P6.1） |
| 平台相关能力 | 独立模块（如 `multimodal/screenshot.py`），便于隔离测试 |

## 约束

见 `CONVENTIONS.md` 第 6 节「架构原则」。核心：模型与能力解耦、配置驱动、平台相关隔离。
