"""块E：World State + Failure Memory（见 docs/adr/0016-world-state-failure-memory.md）。

两个数据结构，**只物化要学习的事实，不含任何决策引擎**（ADR 0014 不变量③）：

- `WorldState`   —— 单会话内累积的事实：Need 历史、按"指纹"聚合的失败计数、
                   已证伪路径（APPROACH_INVALIDATED 落地的具体描述）、未决阻塞（GOAL_BLOCKED）。
                   纯内存，每轮 run 一个实例，无 IO。
- `FailureMemory` —— 跨会话持久的失败记忆（SQLite）。key=(指纹, 错误分类, 失败的 Decision 标签)，
                   记次数与首/末时间。供"出手前查此路是否已知死"。复用标准库 sqlite3、独立文件、可关。

决策仍由模型做；这里只把"此路已 N 次不通（分类=X）"当**事实**喂回（与 loop.py 的 nudge 同模式），
不替模型选路。瞬时 IO 失败由 block D 自动重试处理，**不进** Failure Memory（它不是死路）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_WS_RE = re.compile(r"\s+")
# 取指纹时看的"关键入参"——能区分"同一条路"的字段。其它入参（如 background 标志）忽略。
_KEY_PARAMS = ("command", "path", "file_path", "pattern", "query", "url", "name")


def fingerprint(tool_name: str, params: "dict | None") -> str:
    """对一次工具调用取稳定指纹 = 工具名 + 归一化的关键入参。

    归一化折叠空白 + 小写，避免无意义差异把"同一条路"分裂成多个指纹。
    返回 16 位十六进制（sha1 截断，碰撞概率可忽略，足够当聚合 key）。
    """
    parts = [tool_name or ""]
    p = params or {}
    for k in _KEY_PARAMS:
        v = p.get(k)
        if v:
            parts.append(f"{k}={_WS_RE.sub(' ', str(v)).strip().lower()}")
    raw = "".join(parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class _FailRec:
    fingerprint: str
    error_classes: tuple
    count: int = 0
    last_detail: str = ""


class WorldState:
    """单会话内的世界状态（纯内存）。每个 AgentLoop.run 建一个。"""

    def __init__(self) -> None:
        self.need_history: list[str] = []          # 逐轮 Need（小而稳，是聚合 key）
        self._failures: dict[str, _FailRec] = {}   # 指纹 -> 失败聚合
        self.invalidated: list[str] = []           # 已证伪路径
        self.blocked: list[str] = []               # 未决阻塞

    def record_need(self, need) -> None:
        v = getattr(need, "value", need)
        if v:
            self.need_history.append(str(v))

    def record_failure(self, fp: str, error_classes=None, detail: str = "") -> int:
        """记一次失败，返回该指纹**本会话累计**失败次数。"""
        classes = tuple(getattr(c, "value", c) for c in (error_classes or []))
        rec = self._failures.get(fp)
        if rec is None:
            rec = _FailRec(fp, classes)
            self._failures[fp] = rec
        rec.count += 1
        if classes:
            rec.error_classes = classes
        if detail:
            rec.last_detail = detail
        return rec.count

    def failures_for(self, fp: str) -> int:
        rec = self._failures.get(fp)
        return rec.count if rec else 0

    def classes_for(self, fp: str) -> tuple:
        rec = self._failures.get(fp)
        return rec.error_classes if rec else ()

    def invalidate(self, approach: str) -> None:
        if approach and approach not in self.invalidated:
            self.invalidated.append(approach)

    def block(self, reason: str) -> None:
        if reason and reason not in self.blocked:
            self.blocked.append(reason)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    fingerprint  TEXT NOT NULL,
    error_class  TEXT NOT NULL,
    decision     TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    count        INTEGER NOT NULL DEFAULT 1,
    first_at     REAL NOT NULL,
    last_at      REAL NOT NULL,
    PRIMARY KEY (fingerprint, error_class, decision)
);
"""


class FailureMemory:
    """跨会话失败记忆（SQLite）。

    key = (指纹, 错误分类, 失败的 Decision 标签)。同一条路反复以同种方式失败 → count 累加。
    `known_deadend()` 供 E3：出手前查"此路是否已知死"。线程安全、独立文件、无新依赖。
    """

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._now = time.time          # 测试可替换

    def record(self, fingerprint: str, error_classes=None, decision: str = "", detail: str = "") -> None:
        """记**一次**失败 = 一行增量。只记主分类（classify 已按优先级排序，第一个=根因主类），
        这样"失败次数"= 失败事件数，不会因一次失败命中多个分类而被重复计数。瞬时 IO 不应进来（调用方过滤）。"""
        classes = [getattr(c, "value", c) for c in (error_classes or [])]
        ec = classes[0] if classes else "unknown"
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE failures SET count=count+1, last_at=?, detail=? "
                "WHERE fingerprint=? AND error_class=? AND decision=?",
                (now, detail, fingerprint, ec, decision))
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO failures(fingerprint,error_class,decision,detail,count,first_at,last_at)"
                    " VALUES(?,?,?,?,1,?,?)",
                    (fingerprint, ec, decision, detail, now, now))
            self._conn.commit()

    def count_for(self, fingerprint: str, error_class=None) -> int:
        ec = getattr(error_class, "value", error_class) if error_class is not None else None
        with self._lock:
            if ec is None:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(count),0) n FROM failures WHERE fingerprint=?",
                    (fingerprint,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(count),0) n FROM failures WHERE fingerprint=? AND error_class=?",
                    (fingerprint, ec)).fetchone()
            return int(row["n"]) if row else 0

    def known_deadend(self, fingerprint: str, threshold: int = 2):
        """此指纹累计失败次数 ≥ threshold → 返回 (总次数, 主分类)；否则 None。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT error_class, SUM(count) n FROM failures WHERE fingerprint=? "
                "GROUP BY error_class ORDER BY n DESC", (fingerprint,)).fetchall()
        if not rows:
            return None
        total = sum(int(r["n"]) for r in rows)
        if total < max(1, threshold):
            return None
        return (total, rows[0]["error_class"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
