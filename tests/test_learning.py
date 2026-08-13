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
    # ignore_cleanup_errors：Windows 上 sqlite 连接还开着就删不掉 .db（WinError 32），
    # 而清理失败发生在断言全过之后，不该把测试判红（Linux 允许删已打开的文件，故只在 Windows 现形）。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = Path(d)
        fm = _fm(tmp)
        fm.record("fp-a", ["logic"], detail="assertion failed at line 5")
        aggs = aggregate(fm)
        fm.close()
        a = aggs[0]
        assert "fp-a" in a.fingerprints
        assert any("assertion failed" in e for e in a.examples)


def test_aggregate_empty_memory():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fm = _fm(Path(d))
        assert aggregate(fm) == []
        fm.close()


# ---- propose ------------------------------------------------------------

def test_propose_requires_systemic_failure():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = Path(d) / "strategies.json"
        store = StrategyStore(path)
        s = store.propose(_candidate())
        assert s.status == "proposed" and s.golden_passed is False
        # 重开 → 持久
        store2 = StrategyStore(path)
        got = store2.get(s.id)
        assert got is not None and got.status == "proposed"


def test_store_propose_idempotent_refreshes_evidence():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = StrategyStore(Path(d) / "s.json")
        s1 = store.propose(_candidate())
        c2 = _candidate()
        c2.evidence = {"total": 9, "paths": 5}
        s2 = store.propose(c2)
        assert s1.id == s2.id                       # 同分类同策略，不重复落库
        assert len(store.list()) == 1
        assert store.get(s1.id).evidence["total"] == 9   # 证据已刷新


def test_store_approve_requires_golden():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = StrategyStore(Path(d) / "s.json")
        s = store.propose(_candidate())
        store.approve(s.id, golden_passed=True)
        store.rollback(s.id, reason="撤销采纳")
        assert store.get(s.id).status == "proposed"
        assert store.list("active") == []
        assert store.get(s.id).history[-1]["rollback"] is True


def test_store_list_filter_by_status():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = StrategyStore(Path(d) / "s.json")
        a = store.propose(Candidate("auth", "走 ask_user", "r", {}))
        b = store.propose(Candidate("logic", "先 trace_run", "r", {}))
        store.approve(b.id, golden_passed=True)
        assert {x.id for x in store.list("proposed")} == {a.id}
        assert {x.id for x in store.list("active")} == {b.id}
        assert len(store.list()) == 2


# ---- end-to-end 验收：历史轨迹 → 一条可解释、Golden 验证后生效的策略 ----

def test_end_to_end_one_explainable_strategy():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
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



# ---- 运行时消费（接线）：通路先接好，行为零变化 ----------------------------

def test_render_advice_is_noop_without_active():
    """没有 active 策略 → 必须是彻底的 no-op（空串）。这是"提前接线"敢做的全部理由。"""
    from agentcore.agent.learning import Strategy, render_advice
    assert render_advice([], ["logic"]) == ""
    assert render_advice(None, ["logic"]) == ""
    prop = Strategy(id="S-1", error_class="logic", suggestion="别盲改",
                    rationale="", evidence={}, status="proposed")
    assert render_advice([prop], ["logic"]) == ""          # proposed 没人点头，绝不生效
    ret = Strategy(id="S-2", error_class="logic", suggestion="退役的",
                   rationale="", evidence={}, status="retired")
    assert render_advice([ret], ["logic"]) == ""           # 退役＝关掉
    act = Strategy(id="S-3", error_class="logic", suggestion="先读证据",
                   rationale="", evidence={}, status="active")
    assert render_advice([act], []) == ""                  # 本轮没失败分类 → 不注入
    assert render_advice([act], ["not_found"]) == ""       # 分类对不上 → 不注入


def test_render_advice_matches_and_cites():
    """命中要带出处（策略 id），条数与长度都有上限——注入不能喧宾夺主。"""
    from agentcore.agent.learning import Strategy, render_advice
    mk = lambda i: Strategy(id=f"S-{i}", error_class="logic", suggestion=f"做法{i}",
                            rationale="", evidence={}, status="active")
    text = render_advice([mk(1), mk(2), mk(3)], ["logic"])
    assert "策略 S-1" in text and "策略 S-2" in text, text
    assert "S-3" not in text, text                          # 超出 max_items 被截
    long = Strategy(id="S-9", error_class="logic", suggestion="x" * 900,
                    rationale="", evidence={}, status="active")
    assert len(render_advice([long], ["logic"])) <= 400


def test_shadow_report():
    """影子模式：记下"若有策略会命中谁"，applied 标明这轮到底有没有真生效。"""
    from agentcore.agent.learning import Strategy, shadow_report
    items = [
        Strategy(id="A", error_class="logic", suggestion="", rationale="", evidence={}, status="active"),
        Strategy(id="P", error_class="logic", suggestion="", rationale="", evidence={}, status="proposed"),
        Strategy(id="X", error_class="not_found", suggestion="", rationale="", evidence={}, status="active"),
    ]
    r = shadow_report(items, ["logic"])
    assert r["classes"] == ["logic"] and r["active"] == ["A"] and r["proposed"] == ["P"]
    assert r["applied"] is True
    r2 = shadow_report([items[1]], ["logic"])               # 只有候选、没生效
    assert r2["applied"] is False and r2["proposed"] == ["P"]
    assert shadow_report(items, []) == {}                   # 本轮没失败 → 不发事件


def test_loop_wiring_is_inert_without_strategies():
    """端到端接线：有 store 但没 active 策略时，注入块与事件都不该出现 learning 内容。"""
    import tempfile
    from pathlib import Path
    from agentcore.agent.learning import StrategyStore, render_advice, shadow_report
    from agentcore.agent.loop import detect_repeated_failure
    from agentcore.agent.world_state import FailureMemory, WorldState

    class C:
        def __init__(self, name, inp, cid):
            self.name, self.input, self.id = name, inp, cid

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = StrategyStore(Path(d) / "s.json")           # 空 store
        fm = FailureMemory(Path(d) / "f.db")
        seen = []
        detect_repeated_failure([C("run_bash", {"command": "pytest"}, "t1")],
                                {"t1": "Traceback ... AssertionError: boom"},
                                WorldState(), fm, set(), threshold=2,
                                on_failure=lambda fp, cls, lb: seen.extend(cls))
        assert seen, "回调必须把本轮失败分类交出来，否则块G 拿不到输入"
        assert render_advice(store.list(), seen) == ""      # 零注入
        sr = shadow_report(store.list(), seen)
        assert sr and sr["applied"] is False and sr["active"] == []   # 影子照记


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
