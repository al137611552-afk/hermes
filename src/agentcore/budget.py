"""会话级工具调用预算（对标 Claude Code 的 per-session 上限：子 Agent 200 / WebSearch 200）。

**要解决的问题**：现有预算全是「局部」的——`research_max_rounds` 管一次研究里催重搜几轮、
`delegate_max_revisions` 管一个子任务回炉几次、`crazy_max_*` 管自主模式外层循环。缺的是**整个会话
里某个工具总共能调多少次**的闸。跑偏的任务（尤其 crazy 免确认模式）可以换着关键词无限搜、
无限派子 Agent，每一次都通过所有局部检查，直到把 token 预算烧穿才停。

**设计立场（与块 D/E/H 一脉相承：喂事实、不硬拦截）**：撞上限时**不执行**该工具，但把
「这条路的预算用尽了 + 现在该怎么办」当作工具结果回灌模型——模型仍能用别的工具收尾作答，
只是这一条路关了。区别于 gate 的 deny（那是安全判断），这里是**资源止损**。

**必须全会话共享一个实例**（主 Agent 与所有子 Agent 传同一个）：否则每个子 Agent 各拿一份新预算，
上限等于形同虚设——这正是「派 100 个子 Agent 每个搜 200 次」要防的那个洞。

纯逻辑 + 一把锁（子 Agent 并发执行时会并发计数），无 IO，便于单测。
"""
from __future__ import annotations

import threading

# 默认上限。定位是**跑飞止损护栏**、不是日常约束——正常会话摸不到这个量级
# （对齐 Claude Code 的 200；hermes 的并发上限另由 loop 的 _PARALLEL_CAP 管）。
DEFAULT_LIMITS: dict[str, int] = {
    "web_search": 200,
    "delegate": 200,
}


def exhausted_message(tool_name: str, limit: int) -> str:
    """撞上限时回灌给模型的事实（说清现状 + 出路，不只是报错）。"""
    return (
        f"[预算用尽] 本会话 `{tool_name}` 已调用 {limit} 次，达到上限，本次未执行。\n"
        f"这是防跑飞的资源护栏，说明当前思路在原地打转。"
        f"**别再试这个工具**——用已经拿到的信息作答，说清哪些部分没能查证；"
        f"若确实必须继续，请让用户在设置里调高上限。"
    )


class ToolBudget:
    """按工具名计数的会话级预算。limit ≤ 0 表示该工具不限次。"""

    def __init__(self, limits: "dict[str, int] | None" = None) -> None:
        self._limits = dict(limits if limits is not None else DEFAULT_LIMITS)
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def limit_of(self, tool_name: str) -> int:
        """该工具的上限；0/负数/未配置 = 不限。"""
        return int(self._limits.get(tool_name, 0) or 0)

    def used(self, tool_name: str) -> int:
        with self._lock:
            return self._used.get(tool_name, 0)

    def snapshot(self) -> dict[str, int]:
        """已用计数快照（观测/测试用）。"""
        with self._lock:
            return dict(self._used)

    def consume(self, tool_name: str) -> "str | None":
        """记一次调用。返回 None=可以执行；返回字符串=预算已尽，把这段话回灌模型、别执行。

        计数与判定在同一把锁内完成：并发的子 Agent 不会因为「先各自读到 199」而一起挤过上限。
        撞上限的那次**不计入**已用数——已用数就停在 limit 上，语义干净（不会显示 201/200）。
        """
        limit = self.limit_of(tool_name)
        if limit <= 0:
            return None
        with self._lock:
            if self._used.get(tool_name, 0) >= limit:
                return exhausted_message(tool_name, limit)
            self._used[tool_name] = self._used.get(tool_name, 0) + 1
        return None


def build_limits(max_web_searches: int, max_delegates: int) -> dict[str, int]:
    """从 config 的两个字段拼出 limits（0=不限）。集中在此，便于将来加工具时只改一处。"""
    return {"web_search": max(0, int(max_web_searches or 0)),
            "delegate": max(0, int(max_delegates or 0))}
