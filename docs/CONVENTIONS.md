# 开发规范（Conventions）

本项目所有代码与文档遵循以下规范。修改规范本身需在 DEVLOG 记录原因。

## 1. 目录与文档

```
hermes-dev/
├─ README.md            项目说明 + 运行步骤
├─ CHANGELOG.md         变更记录（Keep a Changelog 格式）
├─ config.yaml          模型档案
├─ docs/
│  ├─ PRD.md            产品需求文档（单一事实来源）
│  ├─ DEVLOG.md         开发日志（按时间倒序）
│  ├─ CONVENTIONS.md    本文件
│  ├─ ARCHITECTURE.md   架构说明
│  └─ adr/              架构决策记录（一决策一文件）
├─ src/agentcore/       Python 内核
└─ web/                 前端
```

- **PRD** 是需求的单一事实来源；需求变更先改 PRD。
- **DEVLOG** 记录每次开发的「做了什么 / 为什么 / 验证结果 / 遗留问题」。
- **ADR** 记录有长期影响的技术决策，不可删除，只能被新 ADR「取代（Superseded）」。

## 2. 版本与变更

- 遵循 [语义化版本 SemVer](https://semver.org)：`MAJOR.MINOR.PATCH`。
- `CHANGELOG.md` 遵循 [Keep a Changelog](https://keepachangelog.com)：
  分组 `Added / Changed / Fixed / Removed`，最新在上。
- 阶段（P0/P1...）完成且验证通过后，更新 CHANGELOG 与版本号。

## 3. 提交信息（Conventional Commits）

```
<type>(<scope>): <subject>

<body>
```
- type：`feat` `fix` `docs` `refactor` `chore` `test` `build`
- scope：`providers` `bridge` `tools` `ui` `config` `multimodal` …
- 例：`feat(tools): add file read/write with permission gate`

## 4. Python 代码风格

- Python ≥ 3.11，全程 **type hints**。
- 格式化 `ruff format`，静态检查 `ruff check`（行宽 100）。
- 模块顶部 `from __future__ import annotations`。
- 命名：模块/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`。
- 公共函数/类写 docstring（中文可），说明意图而非复述代码。
- 错误处理：面向用户的错误信息要可读，统一冒泡到 UI 显示。

## 5. 前端代码风格

- 原生 ES Module，`"use strict"`。
- 命名 `camelCase`；与 Python 桥交互的事件名集中管理。

## 6. 架构原则（硬约束）

- **模型与能力解耦**：UI/内核只依赖 `providers/base.py` 的 `BaseProvider`。
- **工具插件化**：新工具实现统一 schema 并注册，优先兼容 MCP。
- **多模态边界归一**：非文本输入在 `multimodal/` 转为统一消息格式。
- **平台相关隔离**：Windows 专有逻辑（截图/热键/shell）独立成模块，便于测试与移植。
- **配置驱动**：不在代码里写死模型名、密钥、路径。

## 7. 分段交付流程

1. 写/更新 PRD 中该阶段需求。
2. 分段实现，保持每段可独立验证。
3. 自检（语法/可导入/小测）。
4. 交付给用户在 Windows 真机验证。
5. **遇到问题先与用户确认**，不擅自进入下一阶段。
6. 验证通过 → 更新 DEVLOG + CHANGELOG → 进入下一阶段。
