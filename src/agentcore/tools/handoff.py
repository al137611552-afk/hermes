"""request_handoff 工具（ADR 0023 决策 1~3）：agent 碰到**不可代办**的人类环节时把控制权交还用户。

与 ask_user 的分工是**语义与行为都不同**，故不复用（ADR 0023 决策 1）：
- ask_user  = 让人**拍板**（选项择一）；无人值守下按合理默认**自动放行**。
- handoff   = 让人**动手**（登录 / SSO / 验证码 / 支付 / 真人身份验证）；
              无人值守下**绝不放行**——原理上不可代办，自动放行只能产出假成功（决策 2）。

三条结构性约束（不靠提示词自觉）：
1. `verify` 是**必填**参数：请求换手时就得说清"换手后我怎么确认真成了"。
2. 换手结束后由 binding **自动重读现场**（observer 回调，浏览器场景=browser_snapshot），
   把**实际状态**拼进 tool_result 回灌——模型拿到的是现场，不是"用户说做完了"（决策 3）。
3. 无人值守下等待超时即置 `blocked`，crazy 外层据此收在「阻塞：待人工换手」，不记完成。

阻塞 / resolve 机制同 ask.py / 权限 gate：emit 事件推给前端，threading.Event 阻塞，前端 resolve 唤醒。
"""
from __future__ import annotations

import threading
from typing import Callable

from .base import Tool, ToolError

# 无人值守下等人来接管的上限（秒）。到点不是"放行继续"，而是置 blocked、把任务收成阻塞态。
UNATTENDED_WAIT_SECONDS = 600

OBSERVE_MAX_CHARS = 3000   # 换手后自动重读的现场状态，回灌前截断（防吃满上下文）


def compose_result(outcome: str, verify: str, note: str = "", observed: str = "") -> str:
    """把换手结果拼成回灌给模型的 tool_result（纯函数，便于单测）。

    outcome：done=用户称已完成 / skipped=用户做不了或跳过 / blocked=无人值守没人接管。
    结论一律要求模型**先验证再继续**——用户点了"我做完了"不等于真做成了（最常见的失败模式）。
    """
    verify = (verify or "").strip() or "重新读取一次现场状态确认"
    note = (note or "").strip()
    if outcome == "skipped":
        head = ("[换手未完成] 用户表示**做不了 / 跳过**这一步。依赖这一步的后续动作**不要继续**，"
                "也不要假装它已完成：改走不需要它的路径，或如实说明卡在哪、需要什么才能继续。")
    elif outcome == "blocked":
        head = ("[换手未完成·无人值守] 当前无人接管，等待已超时。**任务在此阻塞**：不要绕过、不要编造结果、"
                "更不要据此判定任务完成。请把已完成部分收尾并如实说明卡在「需要人工换手」这一步。")
    else:
        head = ("[换手已交回] 用户表示**已完成**你请求的人工操作。但用户点了完成**不代表真的成了**——"
                f"**下一步必须先验证**：{verify}。验证不通过就再次 request_handoff 说明差在哪，不要假设成功。")
    parts = [head]
    if note:
        parts.append(f"用户补充：{note}")
    if observed:
        parts.append("换手后自动重读的现场状态（未经模型加工）：\n" + observed[:OBSERVE_MAX_CHARS])
    return "\n".join(parts)


class HandoffBinding:
    """阻塞桥：emit 换手请求给前端、阻塞等用户接管完成、resolve 唤醒（同 AskUserBinding 模式）。

    observer：可选回调 () -> str，换手结束后自动重读现场（浏览器 snapshot 等）。抛错视作没读到。
    """

    def __init__(self, emit: Callable[[dict], None],
                 observer: "Callable[[], str] | None" = None,
                 wait_seconds: float = UNATTENDED_WAIT_SECONDS) -> None:
        self._emit = emit
        self._observer = observer
        self._wait = float(wait_seconds)
        self._seq = 0
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, tuple[str, str]] = {}   # rid -> (outcome, note)
        self._unattended = False
        self._lock = threading.Lock()
        self.blocked = False        # 无人值守下发生过"没人接管"——crazy 外层据此判阻塞
        self.awaiting_verify = ""   # 最近一次换手交回后、模型声明的验证方式（供观测/后续硬门）

    def set_unattended(self, on: bool) -> None:
        """自主/crazy 模式：**不自动放行**（与 ask_user 相反），只是把等待改成有限等待 + 通知。"""
        with self._lock:
            self._unattended = bool(on)

    def reset(self) -> None:
        """停止/退出：唤醒所有等待，清残留（不清 blocked——那是本轮结论，由 crazy 外层读完再清）。"""
        with self._lock:
            for ev in self._pending.values():
                ev.set()
            self._pending.clear()
            self._results.clear()

    def clear_blocked(self) -> None:
        with self._lock:
            self.blocked = False

    def request(self, reason: str, target: str, verify: str) -> str:
        with self._lock:
            self._seq += 1
            rid = self._seq
            ev = threading.Event()
            self._pending[rid] = ev
            unattended = self._unattended
            self.awaiting_verify = verify
        self._emit({"id": rid, "reason": reason, "target": target,
                    "verify": verify, "unattended": unattended})
        # 有人值守：一直等（人就在跟前）；无人值守：有限等待，超时收成阻塞而不是放行
        got = ev.wait(self._wait) if unattended else ev.wait()
        with self._lock:
            outcome, note = self._results.pop(rid, ("", ""))
            self._pending.pop(rid, None)
            if not got or not outcome:
                if unattended:
                    self.blocked = True
                    outcome = "blocked"
                else:                       # 被 reset 唤醒（用户停止对话）
                    outcome = "skipped"
                    note = note or "对话被停止，换手未完成"
        observed = ""
        if outcome == "done" and self._observer is not None:
            try:
                observed = (self._observer() or "").strip()
            except Exception as e:  # noqa: BLE001  读不到现场不该让换手失败
                observed = f"（自动重读现场失败：{e}；请自己动手验证）"
        return compose_result(outcome, verify, note, observed)

    def resolve(self, rid: int, outcome: str, note: str = "") -> bool:
        """前端回调：用户点「我做完了」/「做不了，跳过」，唤醒等待的 request()。"""
        outcome = "done" if str(outcome) == "done" else "skipped"
        with self._lock:
            ev = self._pending.get(rid)
            if ev is None:
                return False
            self._results[rid] = (outcome, str(note or ""))
            ev.set()
        return True


class RequestHandoffTool(Tool):
    name = "request_handoff"
    dangerous = False   # 把控制权交还用户是降风险动作，不过权限 gate（同 ask_user）
    description = (
        "碰到**只有人类能做**的环节时调用，把控制权交还用户并暂停等待：登录 / SSO / 短信或邮箱验证码 / "
        "扫码 / 支付 / 真人身份验证 / 需要用户本人授权的确认页。调用后用户在现场操作，完成后你会拿到"
        "**换手后自动重读的现场状态**。**想到该换手就直接调，别先在回复里问「要不要把这步交给你」**——"
        "调用它本身就是在问，而且会把控制权真正交出去；先用文字问一句就停下，任务只会断在半路。"
        "**不要**用它来问方向性问题（那是 ask_user），也不要用它代替你自己能做的操作。"
        "注意：用户回报「已完成」不等于真的成了，拿到结果后必须按你声明的 verify 先验证。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "reason": {"type": "string",
                       "description": "为什么必须由人来做（一句话，让用户一眼看懂要他干什么）"},
            "target": {"type": "string",
                       "description": "操作对象：具体 URL / 应用名 / 文件路径。必须是真实值，用户据此判断该不该做"},
            "verify": {"type": "string",
                       "description": "换手完成后你将**如何验证**它真的成了（例如：重新 snapshot 看页面是否出现用户名）"},
        },
        "required": ["reason", "target", "verify"],
    }

    def __init__(self, binding: HandoffBinding) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._b = binding

    def run(self, params: dict) -> str:
        reason = (params.get("reason") or "").strip()
        target = (params.get("target") or "").strip()
        verify = (params.get("verify") or "").strip()
        if not reason:
            raise ToolError("reason 不能为空：要让用户一眼看懂需要他做什么")
        if not target:
            raise ToolError("target 不能为空：必须给出真实的 URL / 应用 / 路径，用户据此判断该不该做")
        if not verify:
            raise ToolError("verify 不能为空：先说清换手后你怎么确认它真的成了")
        return self._b.request(reason, target, verify)
