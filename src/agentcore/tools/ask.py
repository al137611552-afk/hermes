"""ask_user 工具（对标 Claude Code 的 AskUserQuestion）：agent 需要用户拍板关键细节时，
给出问题 + 候选选项，用户勾选其一或选「其他」补充，结果回灌给 agent。

阻塞 / resolve 机制同权限 gate：emit 事件推给前端，用 threading.Event 阻塞，
直到前端 resolve 唤醒。
"""
from __future__ import annotations

import threading
from typing import Callable

from .base import Tool, ToolError


class AskUserBinding:
    """阻塞桥：emit 问题给前端、阻塞等用户选择、resolve 唤醒（同 PermissionGate 模式）。"""

    def __init__(self, emit: Callable[[dict], None]) -> None:
        self._emit = emit
        self._seq = 0
        self._pending: dict[int, threading.Event] = {}
        self._answers: dict[int, str] = {}
        self._auto = False
        self._lock = threading.Lock()

    def set_auto(self, on: bool) -> None:
        """自主/crazy 模式：不再阻塞等用户，按合理默认自动放行（无人值守）。"""
        with self._lock:
            self._auto = bool(on)

    def ask(self, question: str, options: list[str]) -> str:
        with self._lock:
            if self._auto:  # 无人值守：不弹问题、不阻塞，按第一项合理默认
                pick = options[0] if options else "按合理默认继续"
                return f"[自主模式] 无人值守，已按合理默认选择：{pick}"
            self._seq += 1
            rid = self._seq
            ev = threading.Event()
            self._pending[rid] = ev
        self._emit({"id": rid, "question": question, "options": options})
        ev.wait()  # 等前端 resolve（用户勾选/补充）
        with self._lock:
            ans = self._answers.pop(rid, "")
            self._pending.pop(rid, None)
        return ans or "（用户未作选择）"

    def resolve(self, rid: int, answer: str) -> bool:
        """前端回调：记录用户选择并唤醒等待的 ask()。"""
        with self._lock:
            ev = self._pending.get(rid)
            if ev is None:
                return False
            self._answers[rid] = answer
            ev.set()
        return True

    def reset(self) -> None:
        """停止/退出：唤醒所有等待，清残留。"""
        with self._lock:
            for ev in self._pending.values():
                ev.set()
            self._pending.clear()
            self._answers.clear()


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "需要用户拍板关键细节/方向时调用：给出一个问题 + 2~4 个候选选项，用户勾选其一或选「其他」自行补充，"
        "返回用户的选择。**适合规划/设计阶段有方向性取舍时**（技术栈、范围、风格的二选一等）——"
        "能自己合理决定的小事别问；一次只问一个关键点，别连环追问。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题（清晰、具体）"},
            "options": {
                "type": "array", "items": {"type": "string"},
                "description": "2~4 个候选答案；前端会另外提供「其他」让用户自行补充",
            },
        },
        "required": ["question", "options"],
    }

    def __init__(self, binding: AskUserBinding) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._b = binding

    def run(self, params: dict) -> str:
        q = (params.get("question") or "").strip()
        opts = [str(o) for o in (params.get("options") or []) if str(o).strip()]
        if not q:
            raise ToolError("question 不能为空")
        if not opts:
            raise ToolError("options 至少给一个；纯开放式问题直接在回复里问即可")
        return self._b.ask(q, opts)
