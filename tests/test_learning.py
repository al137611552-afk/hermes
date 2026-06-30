"""块G：Learning Engine 自检（离线聚合 → 候选 → 策略生命周期）。

独立 runner：`python tests/test_learning.py`。用临时 SQLite/JSON，不碰网络、不连真 server。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.learning import (  # noqa: E402
    Candidate, StrategyStore, aggregate, propose,
)
from agentcore.agent.world_state import FailureMemory  # noqa: E402


def _fm(tmp: Path) -> FailureMemory:
    return FailureMemory(tmp / "failures.db")


# ---- aggregate ----------------------------------------------------------

def test_aggregate_groups_by_class():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        # 两条不同的路都因 not_found 失败
        fm.record("fp-a", ["not_found"], decision="", detail="no such file: x")
        fm.record("fp-a", ["not_found"], decision="", detail="no such file: x")
        fm.record("fp-b", ["not_found"], decision="", detail="cannot find y")
        fm.record("fp-c", ["auth"], decision="", detail="401")
        aggs = aggregate(fm)
        fm.close()
        by = {a.error_class: a for a in aggs}
        assert by["not_found"].total == 3          # 2 + 1
        assert by["not_found"].paths == 2          # fp-a, fp-b
        assert by["auth"].total == 1 and by["auth"].paths == 1
        # 按 total 降序
        assert aggs[0].error_class == "not_found"


def test_aggregate_collects_evidence():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        fm.record("fp-a", ["logic"], detail="assertion failed at line 5")
        aggs = aggregate(fm)
        fm.close()
        a = aggs[0]
        assert "fp-a" in a.fingerprints
        assert any("assertion failed" in e for e in a.examples)


def test_aggregate_empty_memory():
    with tempfile.TemporaryDirectory() as d:
        fm = _fm(Path(d))
        assert aggregate(fm) == []
        fm.close()


# ---- propose ------------------------------------------------------------

def test_propose_requires_systemic_failure():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        # 单条路失败 3 次：paths=1 < min_paths → 不升级为策略
        fm.record("fp-a", ["not_found"])
        fm.record("fp-a", ["not_found"])
        fm.record("fp-a", ["not_found"])
        cands = propose(aggregate(fm))
        fm.close()
        assert cands == []


def test_propose_generates_candidate_for_systemic():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        # 跨 3 条路、共 4 次 not_found → 系统性
        fm.record("fp-a", ["not_found"], detail="no file a")
        fm.record("fp-a", ["not_found"], detail="no file a")
        fm.record("fp-b", ["not_found"], detail="no file b")
        fm.record("fp-c", ["not_found"], detail="no file c")
        cands = propose(aggregate(fm))
        fm.close()
        assert len(cands) == 1
        c = cands[0]
        assert c.error_class == "not_found"
        assert "核对" in c.suggestion              # 来自 _SUGGESTION 骨架
        assert c.evidence["paths"] == 3 and c.evidence["total"] == 4
        assert "fp-a" in c.evidence["fingerprints"]


def test_propose_never_promotes_transient():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        # 即便有人把 transient_io 写进了 memory，也绝不成策略
        fm.record("fp-a", ["transient_io"])
        fm.record("fp-b", ["transient_io"])
        fm.record("fp-c", ["transient_io"])
        cands = propose(aggregate(fm))
        fm.close()
        assert all(c.error_class != "transient_io" for c in cands)


def test_propose_thresholds_tunable():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        fm.record("fp-a", ["syntax"])
        fm.record("fp-b", ["syntax"])
        # 默认 min_count=3 → 总 2 次不够
        assert propose(aggregate(fm)) == []
        # 放宽门槛 → 出
        cands = propose(aggregate(fm), min_count=2, min_paths=2)
        fm.close()
        assert len(cands) == 1


# ---- StrategyStore lifecycle -------------------------------------------

def _candidate() -> Candidate:
    return Candidate(error_class="not_found", suggestion="先核对存在性",
                     rationale="系统性", evidence={"total": 4, "paths": 3})


def test_store_propose_and_persist():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "strategies.json"
        store = StrategyStore(path)
        s = store.propose(_candidate())
        assert s.status == "proposed" and s.golden_passed is False
        # 重开 → 持久
        store2 = StrategyStore(path)
        got = store2.get(s.id)
        assert got is not None and got.status == "proposed"


def test_store_propose_idempotent_refreshes_evidence():
    with tempfile.TemporaryDirectory() as d:
        store = StrategyStore(Path(d) / "s.json")
        s1 = store.propose(_candidate())
        c2 = _candidate()
        c2.evidence = {"total": 9, "paths": 5}
        s2 = store.propose(c2)
        assert s1.id == s2.id                       # 同分类同策略，不重复落库
        assert len(store.list()) == 1
        assert store.get(s1.id).evidence["total"] == 9   # 证据已刷新


def test_store_approve_requires_golden():
    with tempfile.TemporaryDirectory() as d:
        store = StrategyStore(Path(d) / "s.json")
        s = store.propose(_candidate())
        # 没过 Golden 不准 active —— 语料门写进代码
        try:
            store.approve(s.id, golden_passed=False)
            assert False, "应拒绝未过 Golden 的 approve"
        except ValueError:
            pass
        assert store.get(s.id).status == "proposed"
        # 过了 Golden → active
        store.approve(s.id, golden_passed=True)
        assert store.get(s.id).status == "active"
        assert store.get(s.id).golden_passed is True
        assert store.active() and store.active()[0].id == s.id


def test_store_retire_and_rollback():
    with tempfile.TemporaryDirectory() as d:
        store = StrategyStore(Path(d) / "s.json")
        s = store.propose(_candidate())
        store.approve(s.id, golden_passed=True)
        # 退役
        store.retire(s.id, reason="实测无效")
        assert store.get(s.id).status == "retired"
        assert store.active() == []
        # 审计留痕
        hist = [h["to"] for h in store.get(s.id).history]
        assert hist == ["proposed", "active", "retired"]


def test_store_rollback_to_proposed():
    with tempfile.TemporaryDirectory() as d:
        store = StrategyStore(Path(d) / "s.json")
        s = store.propose(_candidate())
        store.approve(s.id, golden_passed=True)
        store.rollback(s.id, reason="撤销采纳")
        assert store.get(s.id).status == "proposed"
        assert store.list("active") == []
        assert store.get(s.id).history[-1]["rollback"] is True


def test_store_list_filter_by_status():
    with tempfile.TemporaryDirectory() as d:
        store = StrategyStore(Path(d) / "s.json")
        a = store.propose(Candidate("auth", "走 ask_user", "r", {}))
        b = store.propose(Candidate("logic", "先 trace_run", "r", {}))
        store.approve(b.id, golden_passed=True)
        assert {x.id for x in store.list("proposed")} == {a.id}
        assert {x.id for x in store.list("active")} == {b.id}
        assert len(store.list()) == 2


# ---- end-to-end 验收：历史轨迹 → 一条可解释、Golden 验证后生效的策略 ----

def test_end_to_end_one_explainable_strategy():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fm = _fm(tmp)
        # 历史：external_blocked 在多条路上反复出现（如正面入口被挡）
        for fp in ("fp-1", "fp-2", "fp-3"):
            fm.record(fp, ["external_blocked"], detail=f"403 blocked at {fp}")
        cands = propose(aggregate(fm))
        fm.close()
        assert len(cands) == 1
        c = cands[0]
        # 可解释：建议 + 理由 + 语料证据齐全
        assert c.suggestion and c.rationale and c.evidence["paths"] == 3
        assert "浏览器" in c.suggestion
        # 落库 → 人审 + Golden 后生效
        store = StrategyStore(tmp / "s.json")
        s = store.propose(c)
        store.approve(s.id, golden_passed=True, by="reviewer")
        assert store.active()[0].suggestion == c.suggestion


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    fns.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
