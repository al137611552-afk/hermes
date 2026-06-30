"""Policy：把事实/分类映射成**做法（Decision）**（见 docs/adr/0014）。

块D 落地第一条、也是最便宜的一条 `Need→Decision` 硬规则：**瞬时 IO 失败 → 退避
重试**。这条规则的存在本身是个论证——决策层**不必是大引擎**，确定性、可解释的几
条硬规则就能覆盖最高频的情形；其余交给模型（Planner）。

纪律：
- 只对 `TRANSIENT_IO` 触发——网络抖动/超时/端口占用这类"重试常能过"的失败；其它类
  （LOGIC/SYNTAX/AUTH…）重试无意义，绝不重试。
- 撞重试上限即停（返回 None）——上层据"失败仍在"自然升级处理（概念上 → GOAL_BLOCKED），
  Policy 不在这里伪造 Need。
- Decision 只是个**标签 + 参数**（`RETRY_WITH_BACKOFF` + delay），不建独立引擎。
"""
from __future__ import annotations

from dataclasses import dataclass

from .taxonomy import ErrorClass


@dataclass
class RetryDecision:
    """一次"该重试"的决策。label 供 Learning/日志聚合，delay 是本次重试前的退避秒。"""

    attempt: int                       # 这是第几次重试（1-based）
    delay: float                       # 重试前等待秒（指数退避）
    reason: str = "transient_io"       # 触发原因（错误类）
    label: str = "RETRY_WITH_BACKOFF"  # Decision 标签（块G Learning 聚合用）


def decide_retry(
    error_classes: "list[ErrorClass]",
    attempts_done: int,
    *,
    max_attempts: int,
    backoff_base: float,
) -> "RetryDecision | None":
    """瞬时 IO 失败 → 退避重试，否则 None。

    - `error_classes`：本次失败的分类（块C `classify` 的结果）。
    - `attempts_done`：到目前为止已执行的次数（含首次，≥1）。
    - `max_attempts`：最多重试几次（不含首次）；故允许重试当 `attempts_done <= max_attempts`。
    - 退避：第 n 次重试前等 `backoff_base * 2^(n-1)`（n=attempts_done）。
    """
    if ErrorClass.TRANSIENT_IO not in (error_classes or []):
        return None
    if max_attempts <= 0 or attempts_done > max_attempts:
        return None
    delay = max(0.0, backoff_base) * (2 ** (attempts_done - 1))
    return RetryDecision(attempt=attempts_done, delay=delay)
