# ADR 0020 — 应用内更新（T1：源码自更新）

- 状态：草案（实现完成，待 Windows 真机验证后转正）
- 日期：2026-07-03

## 背景
用户每出新版本都要手动"下载代码 → 本地 python → 本地打包"，繁琐。期望主流程序的体验：**提示有更新 → 一键更新**。

用户有两个使用场景：
- **T1 自用**：本机是 `git clone` + `pip install -e .` 的**源码装**，机器上已有 git 和 python。
- **T2 分发给他人**：已自建 GitHub 下载链接（打包 exe），本 ADR 不覆盖 exe 的换文件更新。

## 决策
先做 **T1 源码自更新**（最省事、能立即解自用痛点，且核心版本检查逻辑 T2 也复用）：

1. **检查更新**：查 GitHub **tags API**（`/repos/OWNER/REPO/tags`，仅需 push git tag，无需建 Release），取最新 SemVer tag，与本地 `importlib.metadata` 版本比对。网络失败**静默**（不打扰）。
2. **提示**：有更新 → 前端面板弹条幅「发现新版本 vX.Y.Z · 立即更新 / 稍后」。
3. **一键更新**：`git pull --ff-only` + `pip install -e .`，复用**已加固的非交互环境**（`GIT_TERMINAL_PROMPT=0` 等，见 [shell 硬化]）避免 `git pull` 卡凭据。完成后提示**重启生效**。

### 关键取舍
- **纯逻辑（版本解析/比较）与 IO（HTTP/跑命令）分离**，前者无头全单测，后者可注入 `fetch`/`run` 便于测试。
- **`--ff-only` 不自动 stash**：本地有未提交改动/分叉时**明确报错让用户手动处理**，绝不隐式 stash/丢改动。
- **不碰用户数据**：`.env`（gitignored）、`data/`（gitignored）、`config.yaml`（若用户改过且上游也改 → ff 失败并提示，不强覆盖）。
- **重启**：MVP 只提示"请重启"，不自动重启（Windows 下重启需谨慎，留后续）。
- **只信 HTTPS + 官方仓库**；tags API 匿名限流 60/hr，更新检查够用。
- **打包 exe（非 git 仓库）**：`apply_update` 首步 `git rev-parse` 判定，非仓库时明确提示"请用下载页更新"，不报错崩溃。

## 影响
- 新增 `src/agentcore/updater.py`（纯逻辑 + IO 分离）、`Api.check_update/apply_update`、前端条幅（`pure.js` 出 HTML + `app.js` 接线）。
- 决策内核（golden）无涉。
- 待 Windows 真机验：真机点「立即更新」跑通 git pull + pip install + 重启生效。

## 备选（未采纳）
- exe 换文件更新（T2）：需先有无密钥打包流水线 + Windows"退出后换文件重启"更新器，ROI 集中在分发给非开发者，用户已自建下载链接，暂缓。
