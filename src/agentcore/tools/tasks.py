"""任务清单工具（FR-9.1）：让 Agent 把复杂/多步任务拆成可勾选的子任务并随进展更新。

对标 Claude Code 的 TodoWrite——**整份替换式**：每次调用传完整清单，模型自行决定何时拆解、
边做边把某项标 in_progress / completed。非危险操作（只写应用内部的任务表，不碰文件/命令），
不过权限 gate；调用作为工具块在前端可见，并驱动对话区顶部的任务面板。

纯逻辑（normalize/summarize/build_block）与 IO（工具 run）分离，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import Tool, ToolError

STATUSES = ("pending", "in_progress", "delegated", "completed")
MAX_TASKS = 50
_MARK = {"pending": "⬜", "in_progress": "🔄", "delegated": "🤖", "completed": "✅"}


def normalize_tasks(raw) -> list[dict]:
    """校验并归一化任务清单：[{content, status}]。非法输入抛 ToolError。"""
    if not isinstance(raw, list):
        raise ToolError("tasks 必须是任务数组")
    if len(raw) > MAX_TASKS:
        raise ToolError(f"任务过多（上限 {MAX_TASKS} 项），请合并或聚焦当前阶段")
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ToolError(f"第 {i + 1} 项不是对象")
        content = (item.get("content") or "").strip()
        if not content:
            raise ToolError(f"第 {i + 1} 项 content 不能为空")
        status = item.get("status") or "pending"
        if status not in STATUSES:
            status = "pending"
        out.append({"content": content, "status": status})
    return out


def summarize_tasks(tasks: list[dict]) -> str:
    """给模型的一句回执：进度计数 + 当前进行项。"""
    if not tasks:
        return "任务清单已清空。"
    done = sum(1 for t in tasks if t["status"] == "completed")
    doing = [t["content"] for t in tasks if t["status"] == "in_progress"]
    delegated = [t["content"] for t in tasks if t["status"] == "delegated"]
    parts = [f"任务清单已更新：{len(tasks)} 项，已完成 {done}/{len(tasks)}"]
    if doing:
        parts.append("进行中：" + "；".join(doing))
    if delegated:
        parts.append("已委派：" + "；".join(delegated))
    return "。".join(parts) + "。"


def build_task_block(tasks: list[dict]) -> str | None:
    """把当前清单拼成注入 system 的块（抗上下文压缩，让模型不忘自己的计划）。"""
    open_tasks = [t for t in tasks if t["status"] != "completed"]
    if not open_tasks and not tasks:
        return None
    lines = [f"{_MARK[t['status']]} {t['content']}" for t in tasks]
    return (
        "[当前任务清单] 这是你为本任务维护的待办（用 update_tasks 更新）。"
        "**勤于更新**：边做边维护——完成一项立刻标 completed、开始下一项标 in_progress、委派出去的标 delegated"
        "（收到摘要后改 completed）。"
        "**更重要的是：只要工作中冒出新发现、改变了原计划——无论来自你自己执行中的认识，还是子任务带回的结果"
        "（新缺口/新来源、原步骤不再适用、需要补做或拆细某事）——都要立刻用 update_tasks 增删/重排任务，"
        "而不只是给现有项打勾**，让清单始终反映真实的下一步计划：\n"
        + "\n".join(lines)
    )


@dataclass
class TaskBinding:
    """把任务工具运行所需的会话上下文打包注入（store + 当前 session 取值 + 事件回调）。"""
    store: object                      # store.Store（有 set_tasks/get_tasks）
    session_getter: Callable[[], "int | None"]
    emit: Callable[[str, object], None]


class UpdateTasksTool(Tool):
    name = "update_tasks"
    description = (
        "维护当前任务的可勾选清单（用于较复杂/多步的任务：先规划再分步执行）。"
        "**整份替换**：每次传入完整的 tasks 数组（含已完成项），不是增量。"
        "拆解任务时建一份；每开始做一项把它的 status 设为 in_progress，做完设为 completed。"
        "简单的一两步任务无需使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "完整的有序任务清单（整份替换上一版）",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "子任务，一句话、具体可执行"},
                        "status": {
                            "type": "string",
                            "enum": list(STATUSES),
                            "description": "pending 待办 / in_progress 进行中 / "
                                           "delegated 已委派给子 Agent / completed 已完成",
                        },
                    },
                    "required": ["content"],
                },
            }
        },
        "required": ["tasks"],
    }

    def __init__(self, binding: TaskBinding) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._b = binding

    def run(self, params: dict) -> str:
        tasks = normalize_tasks(params.get("tasks"))
        sid = self._b.session_getter()
        if sid is None:
            raise ToolError("当前会话尚未保存，无法保存任务清单")
        self._b.store.set_tasks(sid, tasks)
        self._b.emit("tasks_updated", {"tasks": tasks})
        return summarize_tasks(tasks)
