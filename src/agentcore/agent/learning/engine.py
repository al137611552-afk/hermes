"""块G Learning Engine 核心：聚合 → 候选 → 策略存储（见 ADR 0017）。

三段，**全离线、纯分析**，不碰运行时控制流：

1. `aggregate(failure_memory)` —— 把 FailureMemory 各行按 `(错误分类, 失败的 Decision)`
   归并成 `Aggregate`：总失败次数、涉及几条不同的路（指纹）、样例 detail。
2. `propose(aggregates, ...)` —— 对"同一分类在 ≥min_paths 条不同路上累计 ≥min_count 次失败"
   的聚合，生成 `Candidate`（人话建议 + 语料证据 + 理由）。证据驱动，不臆造。
3. `StrategyStore` —— 候选/在用/退役策略的持久存储（JSON）。生命周期：
   proposed → (人审 approve + Golden 通过) → active → retire/rollback。
   approve 强制要求 `golden_passed=True`，否则拒绝——把"没过语料门不准上"写进代码。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# 错误分类 -> 该类反复失败时，给人审的"换条思路"建议骨架。
# 只是**建议文案**，不是自动执行；具体采纳与否由人 + Golden 决定。
_SUGGESTION = {
    "not_found": "先核对目标存在性（list/glob/grep 确认路径或名字）再操作，别原样重试同一条路。",
    "auth": "走 ask_user 让用户补凭证/完成登录，别反复撞需要鉴权的入口。",
    "ambiguous": "先向用户澄清意图/范围，再决定动哪条路。",
    "external_blocked": "改走浏览器直通或换数据源，正面入口已被外部挡住。",
    "logic": "先 trace_run/读证据定位根因，别在同一处反复盲改。",
    "syntax": "回看上一次工具的报错定位语法点，整体改写而非微调重试。",
    "resource": "降资源占用（缩输入/分批/调超时上限）后再试，别等量重试。",
    "unknown": "失败原因未归类，建议人审样例 detail 后再定策略。",
}


@dataclass
class Aggregate:
    """一类失败的聚合视图（聚合 key = 错误分类，可细分到失败的 Decision）。"""
    error_class: str
    total: int                       # 该分类累计失败次数（跨所有路）
    paths: int                       # 涉及多少条不同的路（distinct 指纹）
    decisions: dict                  # 失败时所记的 Decision 标签 -> 次数
    fingerprints: list               # 命中的指纹（语料证据）
    examples: list                   # 样例 detail（去重、截断，最多几条）


@dataclass
class Candidate:
    """一条候选策略（建议，未生效）。"""
    error_class: str
    suggestion: str                  # 人话建议
    rationale: str                   # 为什么建议（证据摘要）
    evidence: dict                   # {total, paths, fingerprints, examples, decisions}


@dataclass
class Strategy:
    """落库的策略，带生命周期与语料证据。"""
    id: str
    error_class: str
    suggestion: str
    rationale: str
    evidence: dict
    status: str = "proposed"         # proposed | active | retired
    golden_passed: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    history: list = field(default_factory=list)   # 状态变迁审计

    def to_dict(self) -> dict:
        return {
            "id": self.id, "error_class": self.error_class,
            "suggestion": self.suggestion, "rationale": self.rationale,
            "evidence": self.evidence, "status": self.status,
            "golden_passed": self.golden_passed,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        return cls(
            id=d["id"], error_class=d.get("error_class", ""),
            suggestion=d.get("suggestion", ""), rationale=d.get("rationale", ""),
            evidence=d.get("evidence", {}), status=d.get("status", "proposed"),
            golden_passed=bool(d.get("golden_passed", False)),
            created_at=d.get("created_at", 0.0), updated_at=d.get("updated_at", 0.0),
            history=list(d.get("history", [])),
        )


def aggregate(failure_memory) -> list:
    """把 FailureMemory 聚合成按"总失败次数"降序的 `Aggregate` 列表。

    瞬时 IO 本就不进 FailureMemory（块E 调用方已过滤），故这里天然不含可重试噪声。
    """
    by_class: dict = {}
    for row in failure_memory.rows():
        ec = row.get("error_class") or "unknown"
        cnt = int(row.get("count", 0) or 0)
        fp = row.get("fingerprint", "")
        dec = row.get("decision") or ""
        detail = (row.get("detail") or "").strip()
        a = by_class.get(ec)
        if a is None:
            a = {"total": 0, "fps": {}, "decisions": {}, "examples": []}
            by_class[ec] = a
        a["total"] += cnt
        a["fps"][fp] = a["fps"].get(fp, 0) + cnt
        if dec:
            a["decisions"][dec] = a["decisions"].get(dec, 0) + cnt
        if detail and detail not in a["examples"] and len(a["examples"]) < 5:
            a["examples"].append(detail[:200])

    out = []
    for ec, a in by_class.items():
        fps = sorted(a["fps"], key=lambda k: a["fps"][k], reverse=True)
        out.append(Aggregate(
            error_class=ec, total=a["total"], paths=len(a["fps"]),
            decisions=dict(a["decisions"]), fingerprints=fps, examples=a["examples"],
        ))
    out.sort(key=lambda x: x.total, reverse=True)
    return out


def propose(aggregates, *, min_count: int = 3, min_paths: int = 2) -> list:
    """从聚合生成候选策略。

    门槛：该分类**跨 ≥min_paths 条不同的路**累计 **≥min_count** 次失败——
    单条路的偶发失败（块D/E 已处理）不升级为"策略"，只有**系统性**的才值得人审。
    """
    cands = []
    for a in aggregates:
        if a.total < min_count or a.paths < min_paths:
            continue
        if a.error_class == "transient_io":     # 双保险：瞬时 IO 永不成策略
            continue
        sug = _SUGGESTION.get(a.error_class, _SUGGESTION["unknown"])
        rationale = (f"「{a.error_class}」类失败在 {a.paths} 条不同的路上累计 {a.total} 次"
                     f"（样例：{a.examples[0] if a.examples else '—'}），呈系统性，非偶发。")
        cands.append(Candidate(
            error_class=a.error_class, suggestion=sug, rationale=rationale,
            evidence={
                "total": a.total, "paths": a.paths,
                "fingerprints": a.fingerprints[:10], "examples": a.examples,
                "decisions": a.decisions,
            },
        ))
    return cands


class StrategyStore:
    """候选/在用/退役策略的持久存储（JSON 单文件，无新依赖、可读可审计）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._now = time.time          # 测试可替换
        self._items: dict = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for d in data.get("strategies", []):
                    s = Strategy.from_dict(d)
                    self._items[s.id] = s
            except Exception:
                self._items = {}      # 损坏文件不致命，当空起

    def _save(self) -> None:
        data = {"strategies": [s.to_dict() for s in self._items.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def _id_for(self, candidate: Candidate) -> str:
        # 同一分类同一建议 = 同一策略（再次提议不重复落库，只刷新证据）
        return f"st-{candidate.error_class}"

    def propose(self, candidate: Candidate) -> Strategy:
        """落一条候选（status=proposed）。已存在则刷新证据/理由，不改其状态。"""
        sid = self._id_for(candidate)
        now = self._now()
        with self._lock:
            s = self._items.get(sid)
            if s is None:
                s = Strategy(
                    id=sid, error_class=candidate.error_class,
                    suggestion=candidate.suggestion, rationale=candidate.rationale,
                    evidence=candidate.evidence, status="proposed",
                    created_at=now, updated_at=now,
                    history=[{"at": now, "to": "proposed"}],
                )
                self._items[sid] = s
            else:
                s.suggestion = candidate.suggestion
                s.rationale = candidate.rationale
                s.evidence = candidate.evidence
                s.updated_at = now
            self._save()
            return s

    def approve(self, strategy_id: str, *, golden_passed: bool, by: str = "human") -> Strategy:
        """人审通过 → active。**强制 golden_passed=True**，否则拒绝（语料门写进代码）。"""
        if not golden_passed:
            raise ValueError("未通过 Golden 验证的策略不准 active（块F 语料门）")
        return self._transition(strategy_id, "active",
                                extra={"by": by, "golden_passed": True})

    def retire(self, strategy_id: str, *, by: str = "human", reason: str = "") -> Strategy:
        """退役 active 策略（事后发现无效/有害）。"""
        return self._transition(strategy_id, "retired", extra={"by": by, "reason": reason})

    def rollback(self, strategy_id: str, *, by: str = "human", reason: str = "") -> Strategy:
        """回滚——把已 active 的策略打回 proposed（撤销采纳，保留证据与审计）。"""
        return self._transition(strategy_id, "proposed",
                                extra={"by": by, "reason": reason, "rollback": True})

    def _transition(self, strategy_id: str, to: str, extra: dict | None = None) -> Strategy:
        now = self._now()
        with self._lock:
            s = self._items.get(strategy_id)
            if s is None:
                raise KeyError(strategy_id)
            s.status = to
            if to == "active":
                s.golden_passed = True
            s.updated_at = now
            s.history.append({"at": now, "to": to, **(extra or {})})
            self._save()
            return s

    def get(self, strategy_id: str):
        return self._items.get(strategy_id)

    def list(self, status: str | None = None) -> list:
        items = list(self._items.values())
        if status is not None:
            items = [s for s in items if s.status == status]
        items.sort(key=lambda s: s.created_at)
        return items

    def active(self) -> list:
        """当前**已生效**策略（运行时若要消费，只读这个；本块暂不接线）。"""
        return self.list("active")
