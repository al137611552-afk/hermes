# Hermes Dev

可自配置模型的多模态编程 Agent（开发中）。跨平台：Windows / macOS / Linux 同一份代码（一份代码、平台差异自动适配）。

当前进度：**P6.1 会话历史持久化**（待验证）；P4 多模态已交付（v0.4.0）。
可选模型（Claude / OpenAI 兼容），桌面窗口，markdown + 代码高亮流式渲染；
模型可读写本地文件、搜索代码、跑 PowerShell 命令，危险操作有权限确认；
可粘贴/拖拽/选择图片与文档（PDF/代码/文本）作为上下文；
对话存 SQLite，重启可恢复，左侧栏多会话切换。

> **图像**：支持视觉的模型（`claude-sonnet`/`gpt`）直接看原图。不支持视觉的模型
> （如 MiniMax-M2.7）可走**视觉预处理回退**（`vision_fallback`，默认**关闭**）——
> 用 VL 端点先把图转文字描述再交给主模型。注意 MiniMax 的 `coding_plan/vlm` 需
> **编程套餐**凭证，普通开放平台 key 调不通；拿到可用凭证后开启 `enabled` 即可。
> 文档/代码/PDF 附件不受影响，正常可用。

## 运行（Windows）

```powershell
# 1. 建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 安装依赖
pip install -e .

# 3. 配置密钥：复制 .env.example 为 .env，填入你的 key
copy .env.example .env
notepad .env

# 4. 启动（推荐用模块方式，避免 hermes-dev 命令未注册到 PATH）
python -m agentcore.app
# 若已注册脚本，也可： hermes-dev
```

> Windows 10/11 自带 WebView2 运行时（pywebview 依赖它）。
> 若提示缺少，可从微软官网安装 “Microsoft Edge WebView2 Runtime”。

## 运行（macOS / Linux）

同一份代码跨平台（pywebview 在 macOS 用系统 WKWebView，Linux 用 GTK）。`shell` 默认 `auto`：macOS/Linux 自动用 `bash`，无需改配置。

```bash
# 1. 建虚拟环境（建议 Python 3.11+；macOS 推荐 Homebrew 的 python3）
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖（macOS 会自动装上 pyobjc / WKWebView 绑定）
pip install -e .

# 3. 配置密钥
cp .env.example .env
open -e .env        # 或 nano .env，填入你的 key（如 ARK_API_KEY）

# 4. 启动
python -m agentcore.app
```

> **macOS 注意**：
> - 截图工具（take_screenshot）首次用需在「系统设置 → 隐私与安全性 → 屏幕录制」里授权终端/Python；未授权会优雅报错、不影响其它功能。
> - 浏览器穿透 / MCP 需本机有 `node`（`brew install node`）。
> - 窗口用系统 WebKit 渲染，无需额外运行时。

> **`.env` 创建小贴士**：用记事本另存易加 `.txt` 后缀或 BOM 导致读不到 key。
> 稳妥做法是在 PowerShell 里直接生成干净文件：
> ```powershell
> "MINIMAX_API_KEY=你的key" | Set-Content -Path .env -Encoding ascii
> ```

## 命令行 / 无头模式（CLI，FR-11.7）

不想开 GUI、想脚本化或接 CI 时，用命令行入口（与 GUI 同款内核）：

```bash
# 免安装：用根目录的 run_cli.py（最省事，无需 pip install）
python run_cli.py "把 src 下的测试跑一遍并报告" -w ./myproj
python run_cli.py "梳理架构给方案" -w . --plan           # 只读规划态：不改文件/不执行
python run_cli.py "修复失败的测试" -w . --json           # 机器可读：结尾一行 JSON
echo "调研这个项目的架构" | python run_cli.py -w ./myproj -   # 从 stdin 读任务

# 已 pip install -e . 之后，也可用注册好的命令：
hermes-cli "把测试跑一遍并报告" -w ./myproj
```

> 注：`python -m agentcore.cli` 仅在 `pip install -e .` 之后可用（需 agentcore 在导入路径上）；
> 没装就用上面的 `python run_cli.py`（它会自动把 `src` 加进路径）。

- 默认助手文本进 **stdout**、工具活动进 **stderr**（`hermes-cli ... > answer.txt` 只取答案）。
- 默认**自动批准**危险操作（等于你本机自己跑命令）；config `agent.permissions.deny` 规则仍拦截。
  想最稳就加 `--plan`（只勘察出方案、绝不改文件）。
- 退出码：成功 `0` / 失败 `1`（可直接用于 CI 判定）。`--json` 输出 `{ok, answer, tools, subagents, elapsed, error}`。

## 配置模型

编辑 `config.yaml`：
- `active_model` 指向默认模型
- `models` 下每个档案：`provider`(anthropic/openai)、`model`、`api_key_env`、可选 `base_url`
- 任何 OpenAI 兼容服务（DeepSeek、各类中转）只需改 `base_url` + `model`

## Agent 工具（P3）

模型可调用工具真正操作本地环境：`read_file` / `write_file` / `edit_file` /
`list_dir` / `grep_search` / `glob_search` / `run_powershell`。

- **工作区沙箱**：工具只能访问 `config.yaml` 的 `agent.workspace`（默认项目根），
  越界路径会被拒绝。
- **权限确认**：写文件、执行命令前会弹确认条（允许 / 拒绝 / 本会话全部允许）。
- `agent` 段可配 `shell`（默认 PowerShell）、`shell_timeout`、`max_steps`。

## 架构

```
web/                前端（pywebview 渲染，含工具块 + 权限确认条）
src/agentcore/
  app.py            入口，起窗口
  config.py         config.yaml + .env（含 AgentConfig）
  providers/        模型适配层（统一接口 + tool-use）
  tools/            工具系统（fs / shell / search + registry）
  agent/            Agent 主循环 + 权限 gate
  multimodal/       附件归一 + 视觉预处理回退
  store/            会话历史持久化（SQLite）
  bridge/api.py     JS <-> Python 桥 + 流式/工具/会话
tests/              纯逻辑自测（无 GUI/网络）
data/               SQLite 数据库（不入库）
```

## 文档

- [docs/PRD.md](docs/PRD.md) — 产品需求文档（需求单一事实来源）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构说明
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — 开发规范
- [docs/DEVLOG.md](docs/DEVLOG.md) — 开发日志
- [docs/adr/](docs/adr/) — 架构决策记录（ADR）
- [CHANGELOG.md](CHANGELOG.md) — 变更记录

## 路线图

- [x] P0 脚手架 + 配置
- [x] P1 单模型流式对话
- [ ] P2 模型适配层完善 + 切换（已可切换，参数面板延后到 P6）
- [x] P3 工具 + Agent 循环（文件/shell/搜索 + 权限确认）
- [x] P4 多模态（图片 / 文档）— 文档真机通过；图像识别需视觉模型
- [ ] P5 截图看屏（依赖视觉模型，暂缓）
- [~] P6 体验扩展 — P6.1 会话持久化实现完成待验证；token 压缩 / 记忆 / MCP 待做
- [ ] P7 PyInstaller 打包
