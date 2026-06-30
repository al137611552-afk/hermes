"""评估/策略分层契约（见 docs/adr/0014）。

整个 Hermes 执行内核共享的稳定数据契约——不止 crazy 模式，搜索/视觉/研究
等所有 Skill 都走同一条：

    Tool → Evaluation(事实) → Policy → Need(差距) → Planner → [Decision + 工具]

本模块只放**最稳定**的两件东西：差距枚举 `Need` 与事实容器 `Evaluation`，
外加把现有判断（crazy verdict、loop.py 各 nudge）映射成 Need 的纯函数。

块 A 纪律：这是**行为等价重构**的地基。本模块不引入任何新能力、不改变任何
现有行为；只是把散落的"判断"收拢到一个可枚举、可观测、可被 Learning 聚合的
契约上。Decision（做法）多数时候仍由模型直接产出，这里不建决策引擎。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Need(str, Enum):
    """世界现在缺什么——**差距**，不是动作。

    纪律（ADR 0014 决策第 3 条）：枚举里**只许出现世界状态的差距**，绝不许出现
    动作。`NEED_REPLANNING / RETRY_SAME / SWITCH_TOOL` 都是 Planner 的**做法
    （Decision）**，不在此列；"路径在失败"是事实 → `PROGRESS_STALLED /
    APPROACH_INVALIDATED`，"所以重规划/换工具"是 Decision。

    继承 str 便于直接进事件 JSON / 日志（`need.value` 即字符串）。
    """

    CONTINUE = "continue"                       # 正常推进，无缺口
    NEED_INFORMATION = "need_information"        # 缺信息/上下文（要去查、去读）
    NEED_EXECUTION = "need_execution"            # 缺一次执行（要去跑、去改、去调）
    NEED_VALIDATION = "need_validation"          # 缺验证（结果未被确认）
    PROGRESS_STALLED = "progress_stalled"        # 路径在原地打转（事实：N 次无进展）
    APPROACH_INVALIDATED = "approach_invalidated"  # 当前路径被证伪（事实：此路不通）
    NEED_USER_INPUT = "need_user_input"          # 缺人类输入/授权
    GOAL_BLOCKED = "goal_blocked"                # 外部硬阻塞，自身无法推进
    GOAL_SATISFIED = "goal_satisfied"            # 目标（或子目标）已达成


@dataclass
class Evaluation:
    """一次行动后的**结构化事实**——不是 Score、不是 Need、不是动作。

    （ADR 0014 决策第 1 条）
    - `metrics` / `signals` 是**事实核**：同样的世界状态永远是同一份。
    - `issues` 是**默认策略层**：把事实按阈值/严重度归类（"测试未全过=blocker"），
      可被上层 Policy 覆盖；Evaluator 给的只是合理默认。
    - Score 只是事实的投影（展示/排序用），**绝不回喂决策**，故这里不存 score。
    """

    metrics: dict[str, float] = field(default_factory=dict)   # 可度量事实：耗时、命中数、通过数/总数、退出码
    signals: list[str] = field(default_factory=list)          # 离散观察：'端口被占用'、'返回 0 条'、'编译报错 E0432'
    issues: list[str] = field(default_factory=list)           # 默认策略归类：'测试未全过=blocker'
    confidence: float = 1.0                                   # 评估自身的置信度 [0,1]

    def as_event(self) -> dict:
        """转成可直接 emit / 落日志的纯 dict（前端观测用）。"""
        return {
            "metrics": dict(self.metrics),
            "signals": list(self.signals),
            "issues": list(self.issues),
            "confidence": self.confidence,
        }


# ── crazy verdict → Need 映射（块 A：仅观测，不改分支行为）────────────────────
#
# crazy 每轮末尾的标记是当前唯一已落地的"判断"。这里把它映射到稳定 Need，作为
# 可观测、可被 Learning 聚合的 key。注意 verdict 还携带**scope**（done=整体目标、
# phase_done=子目标），Need 故意抽掉了 scope；run_autonomous 的分支仍按 verdict 走
# （done→验收门、phase_done→重规划），保持行为逐字节等价——Need 在旁记账，不夺权。
_VERDICT_NEED = {
    "done": Need.GOAL_SATISFIED,         # 整体目标自报达成（仍需块2验收门确认）
    "phase_done": Need.GOAL_SATISFIED,   # 子目标达成（块4 据此重规划——重规划是 Decision）
    "continue": Need.CONTINUE,           # 正常推进
    "need_user": Need.NEED_USER_INPUT,   # 撞岔路/目标模糊，缺人类拍板（块3）
}


def verdict_to_need(verdict: "str | None") -> Need:
    """把 crazy verdict 字符串映射成 Need（纯函数，无副作用）。

    未知 / None → CONTINUE（无明确缺口即继续推进，与现有"无标记则再跑一轮"一致）。
    """
    return _VERDICT_NEED.get(verdict or "", Need.CONTINUE)


# ── loop.py 各 nudge → Need 映射 ─────────────────────────────────────────────
#
# loop.py 的三个情境探测器各自对应一个**单一 Need**。块 A 把它们的"该不该提示"
# 与"提示什么"从一坨字符串逻辑，重构成"探测事实 → 归到 Need → 按 Need 取注入
# 文案"。文案逐字不变（见 NUDGE_INJECTION），故行为等价。
#
#   detect_browse_nudge  → NEED_INFORMATION       （信息收集低效，该按意图检索）
#   detect_stuck_edit    → PROGRESS_STALLED        （反复改同一处仍失败）
#   detect_login_wall    → NEED_USER_INPUT         （登录墙，必须让用户登录）
NUDGE_BROWSE = Need.NEED_INFORMATION
NUDGE_STUCK = Need.PROGRESS_STALLED
NUDGE_LOGIN = Need.NEED_USER_INPUT
