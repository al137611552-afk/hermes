"""Windows 下别让子进程弹出控制台窗口——所有 spawn 点共用的一个 helper。

**症状**：hermes 是**没有控制台的 GUI 进程**（pywebview 窗口 / windowed exe）。
Windows 从这种进程起子进程时，只要不给 `CREATE_NO_WINDOW`，系统就会**给它新建一个
控制台窗口**，子进程一结束窗口立刻消失——用户看到的就是"黑框一闪"。

**真机上最密的一处是 agent 型 MCP 调用**：`gitwatch` 在每次调用**前后各跑一次**
`git status`（v3.72.1 加的"不信 agent 自述、用 git 对账"），于是**委派一次闪两下**
（2026-08-24 用户反馈）。codex server 本身的启动不闪——mcp SDK 自己给了这个标志。

纪律早就有（`shell.py`/`procs.py` 起 shell 时就带着"防黑窗"的注释），**是后加的那批
spawn 点没跟上**。所以收成一个 helper，并配一道**扫描全部 spawn 点**的闸
（`tests/test_winproc.py`）——发现口径是扫源码而不是手抄清单，下一个新加的调用点天然被逮到。
"""
from __future__ import annotations

import os
import subprocess


def no_window(win: "bool | None" = None) -> dict:
    """要展开进 `subprocess.run/Popen` 的 kwargs：Windows 上给 `CREATE_NO_WINDOW`，别处空字典。

    `win` 只是给测试留的注入口（None＝看真实平台）；每次返回**新字典**，调用方可随手改。

    注意它只管"不新建窗口"，**不脱离控制台**——真正需要交互的命令仍会挂住，
    那类问题归 `hardened_env()`（非交互硬化）管，两件事别混。
    """
    is_win = (os.name == "nt") if win is None else bool(win)
    if not is_win:
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
