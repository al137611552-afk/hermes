"""权限确认 gate：逐次确认 + 本会话「全部允许」。

危险工具执行前，agent 循环调用 confirm()。gate 通过注入的 emit 回调把
permission_request 推给前端，并用 threading.Event 阻塞，直到前端调用
bridge.resolve_permission() -> gate.resolve() 唤醒。

线程模型：send_message 在 pywebview 的某个工作线程里同步跑 agent 循环；
前端的 resolve 调用走另一个线程，二者通过 Event 协调。
"""
from __future__ import annotations

import threading
from typing import Callable

from ..permissions import evaluate, is_safe_autorun, suggest_rule

# 前端可回传的决定
ALLOW = "allow"
DENY = "deny"
ALLOW_ALL = "allow_all"
ALLOW_RULE = "allow_rule"   # 「总是允许这类」：把推导的规则加入本会话 allow（FR-11.4）

# 毁灭性命令黑名单：自主/免确认模式下的最后防线，避免无人值守误删/格式化/关机/强推
import re as _re
_DESTRUCTIVE_CMD = _re.compile(
    r"(?:^|[\s;&|(])rm\s+(?:-\w*\s+)*-\w*[rf]"      # rm -rf / rm -fr
    r"|rmdir\s+/s"
    r"|\bdel\s+/[sfq]"                               # del /s /f /q
    r"|\bformat\s+[a-z]:"                            # format c:
    r"|\bmkfs\b|\bfdisk\b"
    r"|\bdd\s+if="
    r"|>\s*/dev/[sh]d"
    r"|:\(\)\s*\{\s*:\s*\|\s*:"                      # fork bomb
    r"|\bshutdown\b|\breboot\b"
    r"|git\s+push\b.*--force"
    r"|git\s+reset\s+--hard",
    _re.I)


def is_destructive(tool_name: str, params: dict) -> bool:
    """命中毁灭性命令黑名单（rm -rf / format / mkfs / dd / fork bomb / 关机 / 强推 / 硬重置等）。"""
    if not isinstance(params, dict):
        return False
    text = " ".join(str(params.get(k, "")) for k in ("command", "cmd", "script"))
    return bool(text.strip()) and bool(_DESTRUCTIVE_CMD.search(text))


class PermissionGate:
    def __init__(self, emit: Callable[[dict], None], allow=None, deny=None,
                 auto_safe: "Callable[[], bool] | None" = None) -> None:
        # emit({"id", "tool", "params", "suggest"}) 负责把请求推给前端
        self._emit = emit
        self._allow_all = False
        # 智能确认分级（Tier1）：闭包现读 config.agent.auto_approve_safe——开则自动放行
        # 「明显安全」的只读/检视/测试 shell 命令、不弹窗（safe-by-default，拿不准仍确认）。
        # 用闭包而非静态布尔，🛠 面板切换即时生效、不必重建 gate。None=不启用该分级。
        self._auto_safe = auto_safe
        # 细粒度规则（FR-11.4）：config 来的 + 本会话「记住此类」追加的，统一在 _allow/_deny
        self._allow: list[str] = list(allow or [])
        self._deny: list[str] = list(deny or [])
        self._seq = 0
        self._pending: dict[int, threading.Event] = {}
        self._decisions: dict[int, str] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        """新会话：复位「本会话全部允许」与会话内追加的规则，并清掉残留等待。

        注意：只清会话态——重建 gate 时 config 规则会重新注入，故这里不必区分来源
        （reset 用于停止/退出场景，本就该回到干净态）。
        """
        with self._lock:
            self._allow_all = False
            for ev in self._pending.values():
                ev.set()
            self._pending.clear()
            self._decisions.clear()

    def confirm(self, tool_name: str, params: dict) -> bool:
        """裁决一次危险操作。deny 规则直接拦截；allow 规则或「全部允许」免确认；
        否则阻塞等用户决定。返回 True=允许执行。"""
        verdict = evaluate(self._allow, self._deny, tool_name, params)
        if verdict == "deny":
            return False
        # 免确认态（crazy / 全部允许）下，毁灭性命令仍强制拦截——无人值守的最后防线。
        # 直接拒绝、不走 _emit（那是权限请求通道，会误触确认态）；模型会收到工具被拒、自行换路。
        if self._allow_all and is_destructive(tool_name, params):
            return False
        if verdict == "allow" or self._allow_all:
            return True
        # 智能确认分级：无显式规则时，「明显安全」的只读/检视/测试命令自动放行、不打断。
        # 仅在开关开启时生效；其余（写文件/编辑/commit/装依赖/拿不准的命令）仍照常弹确认。
        if self._auto_safe is not None and self._auto_safe() and is_safe_autorun(tool_name, params):
            return True

        suggest = suggest_rule(tool_name, params)
        with self._lock:
            self._seq += 1
            req_id = self._seq
            ev = threading.Event()
            self._pending[req_id] = ev

        self._emit({"id": req_id, "tool": tool_name, "params": params, "suggest": suggest})
        ev.wait()  # 等前端 resolve

        with self._lock:
            decision = self._decisions.pop(req_id, DENY)
            self._pending.pop(req_id, None)

        if decision == ALLOW_ALL:
            self._allow_all = True
            return True
        if decision == ALLOW_RULE:
            with self._lock:
                if suggest not in self._allow:
                    self._allow.append(suggest)  # 本会话后续同类调用免确认
            return True
        return decision == ALLOW

    def resolve(self, req_id: int, decision: str) -> bool:
        """前端回调：记录决定并唤醒等待的 confirm()。"""
        with self._lock:
            ev = self._pending.get(req_id)
            if ev is None:
                return False
            self._decisions[req_id] = decision
            ev.set()
        return True
