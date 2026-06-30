"""会话持久化存储层（SQLite，标准库 sqlite3，无新依赖）。

存两张表：sessions（会话）+ messages（消息）。消息 content 以 JSON 文本存，
读时还原成 Message.content（str | list[dict]）。pywebview 在工作线程里调用，
故连接 check_same_thread=False + 一把锁串行化访问。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import blobs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    model      TEXT,
    workspace  TEXT,                 -- 绑定的工作区路径；NULL = 用默认 workspaces_root/<id>
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS session_tasks (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    tasks      TEXT NOT NULL,        -- JSON 数组：[{content, status}]（整份替换式更新）
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_notes (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    notes      TEXT NOT NULL,        -- 工作笔记 Markdown（整份替换；FR-11.3a）
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    payload    TEXT NOT NULL,        -- JSON：{tasks, notes, files:{rel:content|null}}（FR-11.6）
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path, *, externalize_images: bool = True) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 图像外置 blob（P5.1）：放在 db 同级的 blobs/ 下
        self._externalize = externalize_images
        self._blobs_dir = db_path.parent / "blobs"
        if externalize_images:
            self._blobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：给旧库的 sessions 表补列（向后兼容）。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "workspace" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN workspace TEXT")
        if "pinned" not in cols:   # P3：会话置顶
            self._conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

    # ---- 会话 ------------------------------------------------------------
    def create_session(self, title: str, model: str | None, workspace: str | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions(title, model, workspace, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (title or "新会话", model, workspace, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_session_workspace(self, session_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT workspace FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row["workspace"] if row else None

    def get_session_model(self, session_id: int) -> str | None:
        """该会话绑定的模型档案名（每会话可不同，跨重载存活）。NULL/无 = 用全局默认。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT model FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row["model"] if row else None

    def set_session_model(self, session_id: int, model: str | None) -> None:
        """更新会话绑定的模型（用户为该会话切模型时）。不动 updated_at（避免改模型把会话顶到列表最前）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET model=? WHERE id=?", (model, session_id)
            )
            self._conn.commit()

    def get_session_title(self, session_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT title FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row["title"] if row else None

    def list_sessions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, model, workspace, created_at, updated_at, pinned "
                "FROM sessions ORDER BY pinned DESC, updated_at DESC"   # 置顶组在前，组内按最近更新
            ).fetchall()
        return [dict(r) for r in rows]

    def set_session_pinned(self, session_id: int, pinned: bool) -> None:
        """会话置顶/取消置顶（P3）。不动 updated_at，避免置顶把会话顶到最近。"""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET pinned=? WHERE id=?", (1 if pinned else 0, session_id)
            )
            self._conn.commit()

    def rename_session(self, session_id: int, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, time.time(), session_id),
            )
            self._conn.commit()

    def set_session_workspace(self, session_id: int, workspace: str | None) -> None:
        """更新会话绑定的工作区路径（改标题联动重命名工作区文件夹时写回新路径）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET workspace=?, updated_at=? WHERE id=?",
                (workspace, time.time(), session_id),
            )
            self._conn.commit()

    def touch_session(self, session_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id)
            )
            self._conn.commit()

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            # 显式删消息/任务以兼容未启用外键级联的环境
            self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM session_tasks WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM session_notes WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()
            if self._externalize:
                self._gc_blobs_locked()

    def _gc_blobs_locked(self) -> None:
        """删除不再被任何消息引用的孤儿 blob（须在持锁状态下调用）。"""
        referenced: set[str] = set()
        for (content_json,) in self._conn.execute("SELECT content FROM messages"):
            blobs.collect_refs(json.loads(content_json), referenced)
        blobs.gc(self._blobs_dir, referenced)

    def session_exists(self, session_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return row is not None

    # ---- 消息 ------------------------------------------------------------
    def add_message(self, session_id: int, role: str, content) -> None:
        stored = blobs.dehydrate(content, self._blobs_dir) if self._externalize else content
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, role, json.dumps(stored, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def get_messages(self, session_id: int) -> list[dict]:
        """返回 [{role, content}]，content 已还原为 str | list[dict]（含图片 rehydrate）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        out = []
        for r in rows:
            content = json.loads(r["content"])
            if self._externalize:
                content = blobs.rehydrate(content, self._blobs_dir)
            out.append({"role": r["role"], "content": content})
        return out

    def truncate_messages_after(self, session_id: int, keep: int) -> None:
        """只保留该会话前 `keep` 条消息（按 id 升序），删除其后全部。
        用于「重新生成 / 编辑重发」的覆盖式截断（外置图片 blob 暂不回收，量小可忽略）。"""
        if keep < 0:
            keep = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM messages WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
            doomed = [(r["id"],) for r in rows[keep:]]
            if not doomed:
                return
            self._conn.executemany("DELETE FROM messages WHERE id=?", doomed)
            self._conn.commit()

    def search_messages(self, query: str, limit: int = 10) -> list[dict]:
        """跨会话按关键词检索历史消息（content LIKE 任一词命中）——细节的无损来源，供 recall_history 工具。
        返回 [{session_id, title, role, text}]，text 为消息里的纯文本片段（截断）。"""
        import re as _re
        q = (query or "").strip()
        if not q:
            return []
        terms = [t for t in _re.split(r"[\s,，。、;；:：/()（）]+", q) if len(t) >= 2][:6] or [q]
        where = " OR ".join("m.content LIKE ?" for _ in terms)
        params = [f"%{t}%" for t in terms] + [int(limit)]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT m.session_id, s.title, m.role, m.content FROM messages m "
                f"JOIN sessions s ON m.session_id = s.id WHERE {where} "
                f"ORDER BY m.id DESC LIMIT ?",
                params,
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            c = json.loads(r["content"])
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = " ".join(b.get("text", "") for b in c
                                if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = ""
            text = text.strip()
            if text:
                out.append({"session_id": r["session_id"], "title": r["title"],
                            "role": r["role"], "text": text[:400]})
        return out

    # ---- 任务清单（FR-9.1，按会话整份替换式存取） ------------------------
    def set_tasks(self, session_id: int, tasks: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_tasks(session_id, tasks, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET tasks=excluded.tasks, "
                "updated_at=excluded.updated_at",
                (session_id, json.dumps(tasks, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def get_tasks(self, session_id: int) -> list[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT tasks FROM session_tasks WHERE session_id=?", (session_id,)
            ).fetchone()
        return json.loads(row["tasks"]) if row else []

    # ---- 工作笔记（FR-11.3a，按会话整份替换式存取） ----------------------
    def set_notes(self, session_id: int, notes: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_notes(session_id, notes, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET notes=excluded.notes, "
                "updated_at=excluded.updated_at",
                (session_id, notes or "", time.time()),
            )
            self._conn.commit()

    def get_notes(self, session_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT notes FROM session_notes WHERE session_id=?", (session_id,)
            ).fetchone()
        return row["notes"] if row else ""

    # ---- 检查点（FR-11.6，按会话存快照 payload） -------------------------
    def add_checkpoint(self, session_id: int, label: str, payload: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO checkpoints(session_id, label, payload, created_at) VALUES (?,?,?,?)",
                (session_id, label or "检查点",
                 json.dumps(payload, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_checkpoints(self, session_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label, created_at FROM checkpoints "
                "WHERE session_id=? ORDER BY id DESC", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_checkpoint(self, checkpoint_id: int, payload: dict) -> None:
        """更新某检查点的 payload（P12 自动打点：回合内累加改动文件）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE checkpoints SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), checkpoint_id),
            )
            self._conn.commit()

    def prune_checkpoints(self, session_id: int, keep: int = 30) -> int:
        """只保留某会话最近 keep 个检查点（自动打点会累积），删更早的，返回删除数。"""
        with self._lock:
            old = self._conn.execute(
                "SELECT id FROM checkpoints WHERE session_id=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                (session_id, keep),
            ).fetchall()
            ids = [r["id"] for r in old]
            if ids:
                self._conn.executemany(
                    "DELETE FROM checkpoints WHERE id=?", [(i,) for i in ids])
                self._conn.commit()
            return len(ids)

    def get_checkpoint(self, checkpoint_id: int) -> "dict | None":
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, label, payload FROM checkpoints WHERE id=?", (checkpoint_id,)
            ).fetchone()
        if not row:
            return None
        return {"session_id": row["session_id"], "label": row["label"],
                "payload": json.loads(row["payload"])}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def make_title(text: str, limit: int = 24) -> str:
    """用首条用户文本生成会话标题。"""
    t = (text or "").strip().replace("\n", " ")
    if not t:
        return "新会话"
    return t[:limit] + ("…" if len(t) > limit else "")
