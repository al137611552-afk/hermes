"""本机可用浏览器探测（Playwright channel 选型）。

**要解决的问题**（2026-08-26 用户真机反馈）：开了浏览器穿透但没勾有头模式，遇到登录墙时
面板提示「切到有头并打开这页」，点了之后——本机没装 Chrome，浏览器根本弹不出来。
而当时的链路里**没有任何一环在看浏览器到底在不在**：
  · `browser_mcp_args()` 写死 `--browser chrome`；
  · 切有头只 `_reconnect_mcp()`，不检查；
  · `browser_mcp_done` 按「工具数 > 0」判成功——**MCP 连上 ≠ 浏览器能起来**
    （同一类坑此前踩过一次：装好 23 个工具，`browser_navigate` 报 chrome-for-testing not installed）。
于是失败只表现为"点了没反应"，模型看不到原因、只能猜，最后猜出代价最大的那条出路：重启。

**做法**：Windows 上 **Edge 是系统自带的**，而 `msedge` 正是 @playwright/mcp 合法的 channel 值。
所以"没装 Chrome"根本不必让用户去装——直接回退 Edge 就能用。

纯逻辑：路径存在性由调用方注入（`exists`），可脱离真实文件系统单测。
"""
from __future__ import annotations

import os

# Playwright 的 channel 合法值里，我们只用这两个（chromium 不是合法值，firefox/webkit 不带登录态生态）。
CHROME = "chrome"
EDGE = "msedge"

# Windows 上的常规安装位置。**用户级安装（LOCALAPPDATA）也要找**——不少人装 Chrome 时
# 没有管理员权限，装的就是这一份；只看 Program Files 会把它判成"没装"。
_WIN_CHROME = (
    r"{ProgramFiles}\Google\Chrome\Application\chrome.exe",
    r"{ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    r"{LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
)
_WIN_EDGE = (
    r"{ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    r"{ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    r"{LOCALAPPDATA}\Microsoft\Edge\Application\msedge.exe",
)
# Linux/macOS：开发机上跑得到，免得本模块在非 Windows 上变成永远"找不到"。
_NIX_CHROME = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
               "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_NIX_EDGE = ("/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")


def _expand(tpl: str, env) -> str:
    """把 {VAR} 展开成环境变量值；缺变量返回空串（视作该候选不可用）。"""
    out = tpl
    for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        token = "{" + key + "}"
        if token in out:
            val = env.get(key)
            if not val:
                return ""
            out = out.replace(token, val)
    return out


def find_browsers(exists=None, env=None, windows: "bool | None" = None) -> dict:
    """探测本机浏览器 → {"chrome": 路径或空, "msedge": 路径或空}。

    只看**文件在不在**，不去起进程——探测本身必须快且无副作用（它会被放在
    切有头模式的路径上，那里用户正等着窗口弹出来）。
    """
    exists = exists or os.path.exists
    env = env if env is not None else os.environ
    win = os.name == "nt" if windows is None else windows
    cands = {CHROME: _WIN_CHROME if win else _NIX_CHROME,
             EDGE: _WIN_EDGE if win else _NIX_EDGE}
    found = {}
    for name, paths in cands.items():
        hit = ""
        for tpl in paths:
            p = _expand(tpl, env) if win else tpl
            if p and exists(p):
                hit = p
                break
        found[name] = hit
    return found


def pick_channel(found: dict) -> "str | None":
    """选 channel：**Chrome 优先**（穿透效果与 UA 伪装此前都是按 Chrome 调的），
    没有就用 Edge。两个都没有返回 None——这时**不该假装能用**。"""
    if found.get(CHROME):
        return CHROME
    if found.get(EDGE):
        return EDGE
    return None


def explain(found: dict, windows: "bool | None" = None) -> str:
    """给人看的一句话结论 + **可执行的出路**（没有出路的报错等于没报）。"""
    win = os.name == "nt" if windows is None else windows
    ch = pick_channel(found)
    if ch == CHROME:
        return f"已找到 Google Chrome（{found[CHROME]}）"
    if ch == EDGE:
        return (f"未找到 Google Chrome，**将改用本机的 Microsoft Edge**（{found[EDGE]}）——"
                "功能一样，登录态存在 Edge 的独立 profile 里。")
    if win:
        return ("**本机没有找到 Chrome，也没有找到 Edge**，有头模式起不来浏览器。出路："
                "① 装 Google Chrome 后重试；② 或在设置面板重新点一次「🌐 浏览器穿透」"
                "（会自动跑 `playwright install chrome` 下载安装）。")
    return ("本机没有找到 Chrome / Edge（非 Windows 环境）。装一个 Chrome，"
            "或跑 `npx playwright install chrome`。")
