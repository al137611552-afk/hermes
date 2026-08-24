"""agent 型 MCP 调用前后的**客观改动清单**（纯逻辑 + 受控 IO 分离）。

**为什么不信 agent 的自述**：Codex 回来的是一段自然语言（"我修好了 X"），
而它到底动了哪些文件，只有 git 说了算。主流做法（Claude Code 的子 agent）也是
"只回摘要"，摘要的可信度就取决于摘要之外还有没有硬事实——这里补的就是那条硬事实。
同一条纪律在评测里叫「判分优先程序化」。

**取前后两次差集**而不是事后取一次：工作区本来就可能是脏的，事后一把梭会把
用户自己没提交的改动算到 agent 头上——那种"自信的错数"比没有更糟（ADR 0025 决策 3）。
"""
from __future__ import annotations

import subprocess

from ..winproc import no_window

MAX_LINES = 20


def status_lines(cwd: str, timeout: float = 10.0) -> "list[str] | None":
    """`git status --porcelain` 的行（受控 IO）。不是 git 仓库 / 没有 git / 超时 → None。

    None 与 `[]` 是**两件事**：前者=测不了（不该显示任何结论），后者=干净。
    """
    if not cwd:
        return None
    try:
        p = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace",
                           **no_window())   # GUI 进程下不给这个标志＝每次委派闪两下黑框
    except Exception:  # noqa: BLE001 — 没装 git / 超时 / 目录没了：一律当"测不了"
        return None
    if p.returncode != 0:
        return None
    return [l for l in (p.stdout or "").splitlines() if l.strip()]


def diff_status(before, after) -> list:
    """这次调用**新增**的改动行（纯函数）。任一侧为 None（测不了）→ 空。

    只看差集：before 里已有的是用户自己的改动，不该记到 agent 头上。
    """
    if before is None or after is None:
        return []
    seen = set(before)
    return [l for l in after if l not in seen]


def render_changes(lines, measurable: bool = True) -> str:
    """改动行 → 附在工具结果后的一段（纯函数）。

    **"没有改动"要说出来**（`measurable=True` 时）：agent 自述"已创建 xxx"而工作区毫无改动，
    是最值得当场看见的一种矛盾——2026-08-21 真机就是这么被漏过去的（它在别处建了整个项目）。
    测不了（不是 git 仓库/没装 git）则保持安静，别把"没测"说成"没改"。
    """
    if not lines:
        return "\n\n[本次改动·git status] 工作区无改动" if measurable else ""
    head = lines[:MAX_LINES]
    more = len(lines) - len(head)
    body = "\n".join("  " + l for l in head)
    tail = f"\n  …还有 {more} 处" if more > 0 else ""
    return f"\n\n[本次改动·git status]（{len(lines)} 处）\n{body}{tail}"
