"""检查点快照（FR-11.6）：把"本对话改过的文件 + 任务清单 + 工作笔记"打包，支持一键回退。

- capture：对改动台账（ChangeLedger）追踪的每个文件，记其**当前内容**（不存在记 None）；
  连同传入的 tasks/notes 一起组成 payload。git 无关，与 ledger 同口径（run_powershell 改的不计）。
- restore：把文件写回快照内容（None=删除），返回回退的文件数；任务/笔记由调用方还原。

纯逻辑 + 受控 IO（读/写盘），便于单测。回退是破坏性操作，仅经用户确认触发（见 bridge）。
"""
from __future__ import annotations

from pathlib import Path

MAX_FILE_BYTES = 2_000_000   # 超大文件不快照（与 ledger 同口径）


def capture_files(workspace: Path, relpaths) -> dict[str, "str | None"]:
    """记录给定相对路径文件的当前内容（不存在/超大→None；纯函数式快照）。"""
    workspace = Path(workspace)
    snap: dict[str, "str | None"] = {}
    for rel in relpaths:
        rel = (rel or "").replace("\\", "/").strip()
        if not rel or rel in snap:
            continue
        p = workspace / rel
        try:
            if p.is_file() and p.stat().st_size <= MAX_FILE_BYTES:
                snap[rel] = p.read_text(encoding="utf-8", errors="replace")
            else:
                snap[rel] = None   # 不存在 = 当时也"没有"，回退到此即删除
        except OSError:
            snap[rel] = None
    return snap


def make_payload(files: dict, tasks: list, notes: str) -> dict:
    return {"files": files, "tasks": tasks or [], "notes": notes or ""}


def restore_files(workspace: Path, files: dict) -> int:
    """把文件回写到快照状态（content=None → 删除）。返回实际改动的文件数。"""
    workspace = Path(workspace)
    n = 0
    for rel, content in (files or {}).items():
        p = workspace / rel
        try:
            if content is None:
                if p.is_file():
                    p.unlink()
                    n += 1
            else:
                cur = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
                if cur != content:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    n += 1
        except OSError:
            continue
    return n
