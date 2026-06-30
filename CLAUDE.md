# hermes-dev 项目标准

继承全局 `~/.claude/CLAUDE.md`；以下是本项目特有约定。

## 一句话
Windows 桌面多模态编程 Agent：pywebview 外壳 + Web 前端 + Python 内核。
三栏 UI：会话栏 / 对话 / 工作区文件预览。

## 常用命令
- 安装（含依赖）：`pip install -e .`
- 启动应用：`python -m agentcore.app`（Windows 上 `python` 不行就用 `py -m agentcore.app`；
  不要依赖 `hermes-dev` 入口脚本，它常没进 PATH）
- 跑全部测试：`for t in tests/test_*.py; do python "$t"; done`（每个是独立 runner，不依赖 pytest）
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
同步改：`pyproject.toml` version + `CHANGELOG.md`（[Unreleased]→版本号+日期）+
`DEVLOG.md`（状态改“已验证通过”）+ `PRD.md`（对应 FR 状态）。CHANGELOG 遵循 Keep a Changelog + SemVer。

## 配置与密钥
- 模型档案在 `config.yaml`；密钥只在 `.env`，**绝不写进代码或文档**。
- 默认模型 `ark-kimi`（kimi-k2.6，原生视觉，`ARK_API_KEY`）。
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
- **WebView2 滚动坑（v3.37 踩过）**：对话区 `.chat` 是滚动容器，某些 CSS 会让 WebView2 在内容**异步重排**时把 `scrollHeight` 暂时算塌（→ 滚轮跳回顶部、几轮后自愈），**Chromium/Playwright 复现不出**（WebView2 专属）。已知触发：① 给元素只设 `overflow-x:auto`（`overflow-y` 连带变 `auto`、成滚动容器）；② `<hr>` 用 `border:none;border-top` 重构盒子；③ 嵌套列表加 `margin`（嵌套 margin 合并）；④ 给 `table` 加 `display:block`。**规避**：宽表格用外层 `.table-wrap` div 滚动（别动 table 的 display）；列表只用 `padding` 缩进别用 margin；hr 只改色别重构；`.chat` 已加 `overflow-anchor:none`。改对话区 CSS 后**务必真机滚长对话验**，别只信 Chromium 截图。

## 非目标
语音输入、跨平台原生、多用户。
