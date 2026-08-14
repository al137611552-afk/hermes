"""用量台账（ADR 0025 决策 1/7）：**只记事实，不记金额**。

独立 SQLite 文件（默认 `data/usage.db`），自带连接 + 锁，与会话 Store 解耦。

**为什么独立**：用量是**审计性质**的数据，不该跟着会话生命周期走——删一个会话不该把账
一起删掉，否则"这个月花了多少"随时会因清理会话而变小，统计再次不可信。

**为什么不存金额**：价格会变、会填错、有折扣（协议价/阶梯价/时段折扣）。金额一旦落库，
将来改价目表就**无法重算历史**——你会得到一堆按不同时期价格算出的、互不可比的数字。
成本由上层查价目表 join 出来（P2）。

**缓存三态必须分列**（未命中输入 / 写缓存 / 读缓存）：三者**单价不同**，合并即丢失可算性。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,          -- Unix 秒（UTC）
    session_id        INTEGER,                   -- 可空：无头评测/后台任务未必有会话
    steps             INTEGER,                   -- 这一轮走了几步工具往返
    model_profile     TEXT,                      -- 档名（给人看的）。计价**不用**它
    provider          TEXT,                      -- anthropic / openai / ...
    model_id          TEXT,                      -- 真实发给 API 的模型名 —— 计价键（决策 4）
    input_uncached    INTEGER NOT NULL DEFAULT 0,
    input_cache_write INTEGER NOT NULL DEFAULT 0,
    input_cache_read  INTEGER NOT NULL DEFAULT 0,
    output            INTEGER NOT NULL DEFAULT 0,
    reasoning         INTEGER NOT NULL DEFAULT 0,
    measured          INTEGER NOT NULL DEFAULT 1,-- 1=API 实报 0=我方估算（决策 3）
    agent_role        TEXT    NOT NULL DEFAULT 'main',  -- main / delegate:<id> / review:<role>
    harness_version   TEXT,
    request_id        TEXT                       -- 便于跟厂商后台账单对账（决策 6）
);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON usage_log(ts);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_log(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_model   ON usage_log(model_id);

-- 用户手填的价目（ADR 0025 决策 2：用户填的是权威，内置牌价只是兜底）。
-- 与账放同一个库：它们是同一件事的两半，分开放只会出现"账在价目没了"的半残状态。
CREATE TABLE IF NOT EXISTS model_prices (
    model_id         TEXT PRIMARY KEY,   -- 计价键：真实模型名，不是档名（决策 4）
    currency         TEXT NOT NULL,      -- 币种一等字段，不做汇率换算
    price_in         REAL NOT NULL,      -- 每百万 token
    price_out        REAL NOT NULL,
    price_cache_read  REAL,              -- NULL = 没填，算的时候回落输入价并标记 inferred
    price_cache_write REAL,
    as_of            TEXT,               -- 这份价格是哪天的；空=不知道，一律当过期
    note             TEXT,
    updated_at       REAL NOT NULL
);
"""

# token 计数列——聚合时求和的就是这几列
TOKEN_COLS = ("input_uncached", "input_cache_write", "input_cache_read", "output", "reasoning")

# 允许的分组维度（白名单：直接拼进 SQL，不能收外部任意串）
_GROUP_BY = {
    "model_id": "model_id",
    "provider": "provider",
    "agent_role": "agent_role",
    "session_id": "session_id",
    "day": "date(ts, 'unixepoch', 'localtime')",   # 本地日期，按天看花销更符合直觉
}


def provider_kind(obj) -> str:
    """从 provider 实例推出厂商标识：`AnthropicProvider` → `anthropic`。

    不给 Provider 基类加字段，避免为一个统计需求改动模型适配层的公共契约。
    """
    name = type(obj).__name__ if not isinstance(obj, str) else obj
    return name.removesuffix("Provider").removesuffix("_p").lower() or "unknown"


def parse_usage_event(event: str, data) -> "tuple[str, dict] | None":
    """从事件流里认出用量事件，返回 `(agent_role, payload)`；不是用量事件返回 None。

    **两支都要认**：主 Agent 直接发 `usage`；子 Agent 的被包成
    `subagent_event {id, event, data}`——**外层事件名不是 `usage`**。
    漏掉后一支就会把委派花的钱统统算丢，而委派恰恰是重活。

    纯逻辑，好脱离 GUI/模型单测（本函数是 P1 里最容易写错的一处）。
    """
    if event == "usage":
        return ("main", data) if isinstance(data, dict) else None
    if event == "subagent_event" and isinstance(data, dict) and data.get("event") == "usage":
        payload = data.get("data")
        if isinstance(payload, dict):
            return (f"delegate:{data.get('id')}", payload)
    return None


class UsageStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record(
        self,
        *,
        model_id: str | None,
        provider: str | None = None,
        model_profile: str | None = None,
        session_id: int | None = None,
        steps: int | None = None,
        input_uncached: int = 0,
        input_cache_write: int = 0,
        input_cache_read: int = 0,
        output: int = 0,
        reasoning: int = 0,
        measured: bool = True,
        agent_role: str = "main",
        harness_version: str | None = None,
        request_id: str | None = None,
        ts: float | None = None,
    ) -> int:
        """记一行。全零也记——"这轮没花 token"和"这轮没记录"是两回事。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO usage_log (ts, session_id, steps, model_profile, provider, model_id,"
                " input_uncached, input_cache_write, input_cache_read, output, reasoning,"
                " measured, agent_role, harness_version, request_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts if ts is not None else time.time(), session_id, steps, model_profile,
                 provider, model_id, int(input_uncached), int(input_cache_write),
                 int(input_cache_read), int(output), int(reasoning),
                 1 if measured else 0, agent_role, harness_version, request_id),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def totals(
        self,
        *,
        group_by: str | None = None,
        since: float | None = None,
        until: float | None = None,
        session_id: int | None = None,
    ) -> list[dict]:
        """按维度汇总 token。**不算钱**——成本是上层套价目表的事（决策 1）。

        每行附带 `rows`（轮数）与 `estimated_rows`（其中有几行是估算的）——
        估算行不可用于对账，UI 要能据此标注（决策 3）。
        """
        if group_by is not None and group_by not in _GROUP_BY:
            raise ValueError(f"不支持的分组维度：{group_by}（可选：{sorted(_GROUP_BY)}）")
        sums = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in TOKEN_COLS)
        where, params = [], []
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts < ?")
            params.append(until)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        if group_by:
            expr = _GROUP_BY[group_by]
            sql = (f"SELECT {expr} AS bucket, {sums}, COUNT(*) AS rows,"
                   f" COALESCE(SUM(1 - measured),0) AS estimated_rows"
                   f" FROM usage_log{clause} GROUP BY {expr} ORDER BY {expr}")
        else:
            sql = (f"SELECT {sums}, COUNT(*) AS rows,"
                   f" COALESCE(SUM(1 - measured),0) AS estimated_rows FROM usage_log{clause}")
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- 价目（用户手填为权威）---------------------------------------------

    def set_price(self, model_id: str, price) -> None:
        """写/覆盖一条用户价目（`price` 是 `pricing.Price`）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_prices (model_id, currency, price_in, price_out,"
                " price_cache_read, price_cache_write, as_of, note, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(model_id) DO UPDATE SET currency=excluded.currency,"
                " price_in=excluded.price_in, price_out=excluded.price_out,"
                " price_cache_read=excluded.price_cache_read,"
                " price_cache_write=excluded.price_cache_write,"
                " as_of=excluded.as_of, note=excluded.note, updated_at=excluded.updated_at",
                (model_id, price.currency, float(price.input), float(price.output),
                 price.cache_read, price.cache_write, price.as_of or "", price.note or "",
                 time.time()),
            )
            self._conn.commit()

    def delete_price(self, model_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM model_prices WHERE model_id = ?", (model_id,))
            self._conn.commit()

    def user_prices(self) -> dict:
        """全部用户价目 → `{model_id: Price}`（`source="user"`、`verified=True`）。

        标 `verified=True` 是因为**它是人亲手填的**——这正是 ADR 认可的权威来源；
        内置牌价才是未核实的那一类。
        """
        from .pricing import Price
        with self._lock:
            rows = self._conn.execute("SELECT * FROM model_prices").fetchall()
        return {
            r["model_id"]: Price(
                currency=r["currency"], input=r["price_in"], output=r["price_out"],
                cache_read=r["price_cache_read"], cache_write=r["price_cache_write"],
                as_of=r["as_of"] or "", source="user", verified=True, note=r["note"] or "",
            )
            for r in rows
        }

    def totals_with_cost(self, **kw) -> dict:
        """按 `model_id` 汇总 token 并折算成本（**分币种**，不合并）。

        没有价格的桶照样出 token，只是不出金额——这是 ADR 决策 3 的落点：
        **宁可少一个数，不给一个错的数**。
        """
        from .pricing import resolve_price, summarize_costs
        kw.pop("group_by", None)
        buckets = self.totals(group_by="model_id", **kw)
        rows = [{**b, "model_id": b["bucket"]} for b in buckets]
        users = self.user_prices()
        summary = summarize_costs(rows, resolve=lambda mid: resolve_price(mid, users))
        return {"buckets": buckets, **summary}

    def recent(self, limit: int = 50) -> list[dict]:
        """最近若干行明细（对账用：决策 6）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage_log ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
