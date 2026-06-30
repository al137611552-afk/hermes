"""工作笔记工具（FR-11.3a）：让 Agent 在长任务里沉淀「已确认事实/已做决定/进展/坑」。

对标 Manus 的 todo.md 思路、与任务清单（update_tasks）平行：
- 任务清单 = 待办（要做什么）；工作笔记 = 过程中确认下来的事实与决定（已知什么）。
- 整份替换式：每次传完整笔记（Markdown）。存到会话级（session_notes 表），注入 system
  「[工作笔记]」块——**抗上下文压缩、跨重启**：旧的工具往返即便被压缩丢弃，结论仍在笔记里。

非危险（只写应用内部，不碰文件/命令），不过权限 gate。纯逻辑 build_notes_block 便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import Tool, ToolError

MAX_NOTES_CHARS = 8000   # 笔记上限（防注入 system 撑爆预算）


def build_notes_block(notes: str) -> "str | None":
    """把当前笔记拼成注入 system 的块；空则不注入。"""
    notes = (notes or "").strip()
    if not notes:
        return None
    return (
        "[工作笔记] 这是你为当前任务维护的工作记忆（用 update_notes 更新）——"
        "已确认的事实、已做的决定、关键进展与要避开的坑。继续推进时以此为准，"
        "完成一个阶段就把结论补进来（即使对话被压缩，这里也不会丢）：\n" + notes
    )


@dataclass
class NotesBinding:
    """笔记工具运行所需的会话上下文（store + 当前 session 取值 + 事件回调）。"""
    store: object                      # store.Store（有 set_notes/get_notes）
    session_getter: Callable[[], "int | None"]
    emit: Callable[[str, object], None]


class UpdateNotesTool(Tool):
    name = "update_notes"
    description = (
        "维护当前任务的「工作笔记」（Markdown）：记录已确认的事实、已做的决定、关键进展、"
        "要避开的坑——供长任务里跨多轮、跨上下文压缩持续参考。**整份替换**：每次传完整笔记内容"
        "（在上一版基础上增删）。与任务清单互补：清单记待办，笔记记已知与决定。"
        "适合较复杂/长的任务；简单任务不必用。\n"
        "**调试便签（debug 时强烈建议用）**：定位 bug 时在笔记里维护一节「## 调试便签」，按结构记："
        "`现象`（什么输入→什么错误/错值）、`假设`（当前怀疑哪里）、`证据`（trace_run/测试/报错看到的事实）、"
        "`已排除`（试过且否定的方向，**别再重复试**）、`下一步验证`（接下来要确认什么）。"
        "每轮更新它——跨轮不丢线索、不绕回死路，是把「反复改不好」收敛掉的关键。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "notes": {"type": "string",
                      "description": "完整的工作笔记（Markdown，整份替换上一版；传空字符串可清空）"},
        },
        "required": ["notes"],
    }

    def __init__(self, binding: NotesBinding) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._b = binding

    def run(self, params: dict) -> str:
        notes = params.get("notes")
        if notes is None:
            raise ToolError("notes 不能缺省（传空字符串可清空笔记）")
        if len(notes) > MAX_NOTES_CHARS:
            raise ToolError(f"笔记过长（上限 {MAX_NOTES_CHARS} 字符），请精简到关键事实与决定")
        sid = self._b.session_getter()
        if sid is None:
            raise ToolError("当前会话尚未保存，无法保存工作笔记")
        self._b.store.set_notes(sid, notes)
        self._b.emit("notes_updated", {"chars": len(notes)})
        return "工作笔记已更新。" if notes.strip() else "工作笔记已清空。"
