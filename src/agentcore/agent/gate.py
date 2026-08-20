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
ALLOW_RULE = "allow_rule"   # 「总是允许这类」：加入 allow 并**持久化**（FR-11.4 / 11.4b）

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
                 auto_safe: "Callable[[], bool] | None" = None,
                 on_rule_added: "Callable[[str], None] | None" = None) -> None:
        # emit({"id", "tool", "params", "suggest"}) 负责把请求推给前端
        self._emit = emit
        self._allow_all = False
        # 智能确认分级（Tier1）：闭包现读 config.agent.auto_approve_safe——开则自动放行
        # 「明显安全」的只读/检视/测试 shell 命令、不弹窗（safe-by-default，拿不准仍确认）。
        # 用闭包而非静态布尔，🛠 面板切换即时生效、不必重建 gate。None=不启用该分级。
        self._auto_safe = auto_safe
        # 「总是允许这类」落盘回调（FR-11.4b）。以前只加进本会话、**重启就丢**——
        # 用户以为放行了、下次照旧弹窗，于是养成闭眼点同意的习惯，反而更危险。
        # None＝不持久化（单测/子 Agent 场景）。
        self._on_rule_added = on_rule_added
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

    # 裁决结果（explain 的返回值）。UI 用它解释"为什么没问你"——三种免确认原因长得一模一样，
    # 不说清楚用户只能猜（真机验证时就栽在这：以为漏了确认，其实是命中只读白名单）。
    DENY_RULE = "deny_rule"          # deny 规则拦截
    DESTRUCTIVE = "destructive"      # 免确认态下的毁灭性命令，强制拦
    BY_RULE = "rule"                 # 命中 allow 规则
    BY_SESSION = "session"           # 本会话「全部允许」/ 自主模式
    BY_SAFE = "safe"                 # 智能确认分级：只读命令自动放行
    ASK = "ask"                      # 要问用户
    AUTO_REASONS = {
        BY_RULE: "命中放行规则",
        BY_SESSION: "本会话已全部允许",
        BY_SAFE: "只读命令，智能确认分级自动放行",
    }

    def explain(self, tool_name: str, params: dict, always_ask: bool = False) -> str:
        """这次调用会怎么被裁决（不阻塞、无副作用）。confirm 与 UI 共用同一套判定，避免两处漂移。

        `always_ask`＝**高影响力工具**（agent 型 MCP server：一次调用就是一个自主 agent
        跑几分钟、可能改一堆文件）。它**不吃 allow 规则、也不吃「本会话全部允许」**——
        那个开关的心智模型是"这些零碎命令我都认"，是为单点、可逆、几秒钟的操作设计的，
        用它顺带放开一个自主 agent，粒度显然不对（2026-08-20 真机：用户点过「全部允许」后
        Codex 全程零确认地跑完）。deny 与毁灭性拦截仍然优先——放行档次只降不升。
        """
        verdict = evaluate(self._allow, self._deny, tool_name, params)
        if verdict == "deny":
            return self.DENY_RULE
        # 免确认态（crazy / 全部允许）下，毁灭性命令仍强制拦截——无人值守的最后防线。
        if self._allow_all and is_destructive(tool_name, params):
            return self.DESTRUCTIVE
        if always_ask:
            return self.ASK
        if verdict == "allow":
            return self.BY_RULE
        if self._allow_all:
            return self.BY_SESSION
        # 智能确认分级：无显式规则时，「明显安全」的只读/检视/测试命令自动放行、不打断。
        # 仅在开关开启时生效；其余（写文件/编辑/commit/装依赖/拿不准的命令）仍照常弹确认。
        if self._auto_safe is not None and self._auto_safe() and is_safe_autorun(tool_name, params):
            return self.BY_SAFE
        return self.ASK

    def auto_reason(self, tool_name: str, params: dict, always_ask: bool = False) -> str:
        """免确认的人话原因；要问用户则返回空串。"""
        return self.AUTO_REASONS.get(self.explain(tool_name, params, always_ask), "")

    def confirm(self, tool_name: str, params: dict, always_ask: bool = False) -> bool:
        """裁决一次危险操作。deny 规则直接拦截；allow 规则或「全部允许」免确认；
        否则阻塞等用户决定。返回 True=允许执行。

        `always_ask` 见 `explain`：高影响力工具每次都问，且**不提供「总是允许这类」**——
        对 `codex__codex` 这种没有 path/command 参数的工具，`suggest_rule` 给出的是**裸工具名**，
        点一次就等于"以后这个自主 agent 干什么都不用问"，而且会落盘、重启仍生效。
        """
        why = self.explain(tool_name, params, always_ask)
        if why in (self.DENY_RULE, self.DESTRUCTIVE):
            # 直接拒绝、不走 _emit（那是权限请求通道，会误触确认态）；模型会收到工具被拒、自行换路。
            return False
        if why != self.ASK:
            return True

        suggest = "" if always_ask else suggest_rule(tool_name, params)
        with self._lock:
            self._seq += 1
            req_id = self._seq
            ev = threading.Event()
            self._pending[req_id] = ev

        # always=True 时前端要隐藏「总是允许这类」「本会话全部允许」并说明原因——
        # 否则用户点了却仍然每次弹，看起来像 bug
        self._emit({"id": req_id, "tool": tool_name, "params": params, "suggest": suggest,
                    "always": bool(always_ask)})
        ev.wait()  # 等前端 resolve

        with self._lock:
            decision = self._decisions.pop(req_id, DENY)
            self._pending.pop(req_id, None)

        if decision == ALLOW_ALL:
            self._allow_all = True
            return True
        if decision == ALLOW_RULE:
            with self._lock:
                new_rule = suggest not in self._allow
                if new_rule:
                    self._allow.append(suggest)  # 本会话后续同类调用立即免确认
            if new_rule and self._on_rule_added:
                try:
                    self._on_rule_added(suggest)   # 落盘：下次启动仍然放行，且面板里可见可撤
                except Exception:  # noqa: BLE001 — 落盘失败不该让这次操作失败
                    pass
            return True
        return decision == ALLOW

    def set_rules(self, allow=None, deny=None) -> None:
        """热更新规则（🔐 权限面板增删后调用），运行中的会话立即生效、不必重启。"""
        with self._lock:
            if allow is not None:
                self._allow = list(allow)
            if deny is not None:
                self._deny = list(deny)

    def rules(self) -> "tuple[list[str], list[str]]":
        with self._lock:
            return list(self._allow), list(self._deny)

    def resolve(self, req_id: int, decision: str) -> bool:
        """前端回调：记录决定并唤醒等待的 confirm()。"""
        with self._lock:
            ev = self._pending.get(req_id)
            if ev is None:
                return False
            self._decisions[req_id] = decision
            ev.set()
        return True
