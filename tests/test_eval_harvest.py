"""块 V4（harvest：喂饱 Learning）的离线自检。

不调模型、不联网。验三件事：
1. **合并语义与 FailureMemory 主键同口径**——口径一偏，"跨几条路失败了几次"这个
   `propose()` 唯一的门槛就算错了，候选要么凭空冒出来要么永远出不来；
2. **合并是纯函数**——绝不回写进任何一次跑用过的库（回写就会污染下一次回放：
   死路文案里嵌着累计次数，库一变文案就变，cassette 全 miss，块 V3 踩过）；
3. **收割范围的挑法**：回放只能收录过音的，真跑才连不可回放的一起收。

运行：python tests/test_eval_harvest.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from agentcore.agent.learning import aggregate, propose  # noqa: E402
from agentcore.agent.world_state import FailureMemory  # noqa: E402
from harvest import merge_rows, pick_tasks, render_report  # noqa: E402
from tasks import TASKS  # noqa: E402


def _row(fp, ec="logic", dec="run_bash", detail="", count=1, first=10.0, last=20.0):
    return {"fingerprint": fp, "error_class": ec, "decision": dec, "detail": detail,
            "count": count, "first_at": first, "last_at": last, "source": "eval"}


# ---- 合并语义 -----------------------------------------------------------------

def test_merge_sums_on_the_same_primary_key():
    """键 = (指纹, 分类, decision)，与 FailureMemory 的主键逐字一致。"""
    out = merge_rows([_row("a", count=2, first=5.0, last=9.0)],
                     [_row("a", count=3, first=7.0, last=30.0)])
    assert len(out) == 1
    assert out[0]["count"] == 5
    assert out[0]["first_at"] == 5.0 and out[0]["last_at"] == 30.0


def test_merge_keeps_different_keys_apart():
    out = merge_rows([], [_row("a"), _row("a", ec="syntax"), _row("a", dec="edit_file"),
                          _row("b")])
    assert len(out) == 4, out


def test_merge_prefers_a_non_empty_detail():
    """样例 detail 是给人审看的证据，空的那条不该盖掉有内容的。"""
    out = merge_rows([_row("a", detail="")], [_row("a", detail="AssertionError: x")])
    assert out[0]["detail"] == "AssertionError: x"
    out = merge_rows([_row("a", detail="有内容")], [_row("a", detail="")])
    assert out[0]["detail"] == "有内容"


def test_merge_is_pure():
    """纯函数：不改入参。合并结果只用于事后分析，绝不回写进跑过的库——
    回写会改变死路文案里的累计次数，让下一次回放全 miss（块 V3 的第三个发现）。"""
    base = [_row("a", count=1)]
    inc = [_row("a", count=1)]
    merge_rows(base, inc)
    assert base[0]["count"] == 1 and inc[0]["count"] == 1


def test_merge_sorts_by_count_desc():
    out = merge_rows([], [_row("a", count=1), _row("b", count=9), _row("c", count=4)])
    assert [r["fingerprint"] for r in out] == ["b", "c", "a"]


# ---- 与 propose 门槛的口径一致（防 schema/口径漂移）--------------------------

def test_merged_rows_round_trip_through_failure_memory():
    """合并结果灌回 FailureMemory 后，`aggregate`/`propose` 数出来的必须是同一回事。

    这条钉的是**跨模块口径**：harvest 自己数一遍、引擎再数一遍，两边对不上，
    报告里的"13 次 / 7 条路"就是假的。
    """
    rows = merge_rows([], [_row("fp1", count=2), _row("fp2", count=1),
                           _row("fp3", ec="syntax", count=5)])
    with tempfile.TemporaryDirectory() as d:
        fm = FailureMemory(Path(d) / "h.db", source="eval")
        try:
            for r in rows:
                for _ in range(r["count"]):
                    fm.record(r["fingerprint"], [r["error_class"]],
                              decision=r["decision"], detail=r["detail"])
            aggs = {a.error_class: a for a in aggregate(fm)}
        finally:
            fm.close()
    assert aggs["logic"].total == 3 and aggs["logic"].paths == 2
    assert aggs["syntax"].total == 5 and aggs["syntax"].paths == 1
    cands = {c.error_class for c in propose(list(aggs.values()), min_count=3, min_paths=2)}
    assert cands == {"logic"}, cands          # syntax 只有 1 条路 → 不过门（单路偶发不升级为策略）


def test_transient_io_never_becomes_a_strategy():
    """双保险仍在：瞬时 IO 是块D 自动重试的活，永远不该升级成"策略"。"""
    rows = merge_rows([], [_row(f"fp{i}", ec="transient_io", count=3) for i in range(4)])
    with tempfile.TemporaryDirectory() as d:
        fm = FailureMemory(Path(d) / "h.db", source="eval")
        try:
            for r in rows:
                for _ in range(r["count"]):
                    fm.record(r["fingerprint"], [r["error_class"]], decision=r["decision"])
            aggs = aggregate(fm)
        finally:
            fm.close()
    assert propose(aggs) == []


# ---- 收割范围 -----------------------------------------------------------------

def test_replay_harvest_only_takes_recorded_tasks():
    take, skip = pick_tasks(list(TASKS), tier=None, live=False)
    reasons = dict(skip)
    for name, t in TASKS.items():
        if not t.replayable:
            assert name in reasons and "不可回放" in reasons[name], name
            assert name not in take
    assert take, "一个可收割的任务都没有？"


def test_live_harvest_takes_everything_including_unreplayable():
    take, _skip = pick_tasks(list(TASKS), tier=None, live=True)
    assert set(take) == set(TASKS), "真跑收割不该漏掉不可回放的任务（它们照样产语料）"


def test_tier_filter():
    take, _ = pick_tasks(list(TASKS), tier="L3", live=True)
    assert take and all(TASKS[n].tier == "L3" for n in take)


# ---- 报告 ---------------------------------------------------------------------

class _C:
    def __init__(self, ec, ev):
        self.error_class, self.suggestion, self.rationale, self.evidence = ec, "建议", "依据", ev


class _A:
    def __init__(self, ec, total, paths):
        self.error_class, self.total, self.paths = ec, total, paths


def test_report_renders_evidence():
    md = render_report(
        [_C("logic", {"total": 13, "paths": 7, "fingerprints": ["aa", "bb"],
                      "examples": ["AssertionError: x"], "decisions": {"run_bash|after_nudge": 3}})],
        [_A("logic", 13, 7), _A("unknown", 2, 1)],
        {"live": False, "tasks": 18, "repeat": 1, "rows": 14, "failures": 19, "run_id": "r1"})
    assert "离线回放" in md and "13 次失败 / 7 条不同的路" in md
    assert "after_nudge" in md, "「被提示过仍走同一条路」是回答『提示到底有没有用』的关键证据"
    assert "不自动采纳" in md, "生命周期纪律必须写在报告里（ADR 0027 决策 7）"
    assert "| `unknown` | 2 | 1 | — |" in md, "未过门的分类也要列出来，别只报好消息"


def test_empty_report_says_go_widen_the_task_set():
    """一条候选都没有时，报告必须指向**回 V2 补任务**，而不是"调低门槛"。

    调低门槛只会批量生成垃圾候选——ADR 0014 已经论证过这条，报告不能反着教人。
    """
    md = render_report([], [_A("logic", 2, 1)], {"live": False, "tasks": 3, "repeat": 1})
    assert "失败面不够宽" in md and "别调低门槛" in md


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
