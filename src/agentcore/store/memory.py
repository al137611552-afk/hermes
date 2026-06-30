"""长期记忆存储层（P6.3 / FR-6.3）。

跨会话、跨重启持久的事实/偏好/项目背景，独立于单会话的消息历史。
用独立的 SQLite 文件（默认 data/memory.db），自带连接 + 锁，与会话 Store 解耦，
可独立开关。无新依赖（标准库 sqlite3）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

# 允许的记忆类别（仅"跨项目通用"的事实；项目专属内容应进项目 hermes.md，不入全局记忆）。
# 其它值归一为 "fact"。注：旧库里可能有 kind="project" 的历史条目，会按 fact 显示。
KINDS = ("user", "preference", "skill", "fact", "principle")  # principle=固化后的框架原则（优先召回）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'fact',
    source     TEXT,                 -- 来源：tool / auto:<session_id>
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- "忘记"墓碑：被 forget 删过的内容（归一后），自动抽取不再重新记住，
-- 避免"刚 forget、离开会话又被自动抽取学回来"。显式 remember 可解除。
CREATE TABLE IF NOT EXISTS forgotten (
    content_norm TEXT PRIMARY KEY,
    created_at   REAL NOT NULL
);
"""

_COLS = "id, content, kind, source, created_at, updated_at"


def normalize_kind(kind: str | None) -> str:
    k = (kind or "").strip().lower()
    return k if k in KINDS else "fact"


def _norm(content: str | None) -> str:
    """归一化用于去重比较：折叠空白 + 小写。"""
    return " ".join((content or "").split()).lower()


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, content: str, kind: str = "fact", source: str | None = None) -> int | None:
        """新增一条记忆；归一后与已有重复、或属"已忘记"内容（仅对自动抽取）则跳过返回 None。

        source 以 "auto" 开头视为自动抽取：若该内容在 forgotten 墓碑里，则不重新记。
        显式 remember（非 auto）不受墓碑限制，并会解除该内容的墓碑。
        """
        content = (content or "").strip()
        if not content:
            return None
        kind = normalize_kind(kind)
        target = _norm(content)
        is_auto = bool(source) and source.startswith("auto")
        now = time.time()
        with self._lock:
            if is_auto and self._conn.execute(
                "SELECT 1 FROM forgotten WHERE content_norm=?", (target,)
            ).fetchone():
                return None  # 用户已忘记的事实，自动抽取不再重新记
            for (existing,) in self._conn.execute("SELECT content FROM memories"):
                if _norm(existing) == target:
                    return None  # 已有等价记忆，不重复记录
            cur = self._conn.execute(
                "INSERT INTO memories(content, kind, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (content, kind, source, now, now),
            )
            if not is_auto:  # 显式记忆 -> 解除该内容的"忘记"封印
                self._conn.execute("DELETE FROM forgotten WHERE content_norm=?", (target,))
            self._conn.commit()
            return cur.lastrowid

    def list(self, limit: int | None = None) -> list[dict]:
        sql = f"SELECT {_COLS} FROM memories ORDER BY updated_at DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return self.list(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM memories WHERE content LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (f"%{q}%", int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def _tombstone_locked(self, content: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO forgotten(content_norm, created_at) VALUES (?,?)",
            (_norm(content), time.time()),
        )

    def delete(self, memory_id: int) -> bool:
        """删一条记忆，并记入"忘记"墓碑（避免自动抽取又学回来）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM memories WHERE id=?", (int(memory_id),)
            ).fetchone()
            cur = self._conn.execute("DELETE FROM memories WHERE id=?", (int(memory_id),))
            if row:
                self._tombstone_locked(row["content"])
            self._conn.commit()
            return cur.rowcount > 0

    def forget_by_query(self, query: str) -> list[str]:
        """删除所有内容包含 query 的记忆，并逐条记入墓碑。返回被删的内容列表。"""
        q = (query or "").strip()
        if not q:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content FROM memories WHERE content LIKE ?", (f"%{q}%",)
            ).fetchall()
            deleted: list[str] = []
            for r in rows:
                self._conn.execute("DELETE FROM memories WHERE id=?", (r["id"],))
                self._tombstone_locked(r["content"])
                deleted.append(r["content"])
            self._conn.commit()
            return deleted

    def contents(self) -> list[str]:
        """所有记忆的文本（最近优先），供抽取去重 / 注入用。"""
        return [m["content"] for m in self.list()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
