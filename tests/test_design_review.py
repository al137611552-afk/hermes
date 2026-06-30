"""Architecture Review Mode 引擎（ADR 0019）：Decision/四态共识/可数 gate/可证伪停止条件。

运行：python tests/test_design_review.py
纯逻辑、不碰网络（review_fn 用假桩）。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.design_review import (  # noqa: E402
    ACCEPTED, DEFERRED, NEEDUSER, OPEN, REJECTED, Decision, apply_review,
    build_review_prompt, can_start_coding, count_blocking, gate_status,
    parse_decisions, render_consensus, round_snapshot, run_review, should_stop,
)


def _d(id, status=OPEN, blocking=None, choice="x"):
    return Decision(id=id, title=f"决策{id}", current_choice=choice,
                    status=status, blocking=list(blocking or []))


# ── gate：可数事实，绝不百分比 ──────────────────────────────────────────────
def test_count_blocking_counts_open_needuser_and_open_blocking():
    ds = [
        _d("a", ACCEPTED),                       # 不阻塞
        _d("b", DEFERRED),                       # 不阻塞
        _d("c", NEEDUSER),                       # 阻塞：待用户
        _d("d", ACCEPTED, blocking=["还没想清"]),  # 阻塞：有未决问题
        _d("e", OPEN),                           # 阻塞：未收敛
        _d("f", REJECTED),                       # 不阻塞（已决定不做）
    ]
    assert count_blocking(ds) == 3


def test_gate_locked_until_zero_blocking_and_signed():
    ds = [_d("a", NEEDUSER)]
    assert can_start_coding(ds, user_signed=True) is False   # 有未决 → 锁
    ds = [_d("a", ACCEPTED)]
    assert can_start_coding(ds, user_signed=False) is False  # 没签字 → 锁
    assert can_start_coding(ds, user_signed=True) is True    # 零未决 + 签字 → 开


def test_gate_status_is_honest_count_no_score():
    g = gate_status([_d("a", NEEDUSER), _d("b", OPEN)], user_signed=True)
    assert g["can_start"] is False and g["blocking_count"] == 2
    assert "2 个未决" in g["reason"]
    assert "%" not in g["reason"]                            # 绝不出现百分比
    g2 = gate_status([_d("a", ACCEPTED)], user_signed=False)
    assert g2["blocking_count"] == 0 and "签字" in g2["reason"]


# ── 停止条件：可证伪、可数 ──────────────────────────────────────────────────
def test_stop_on_max_rounds():
    rounds = [round_snapshot([_d("a", OPEN, ["q"])]) for _ in range(3)]
    stop, reason = should_stop(rounds, max_rounds=3)
    assert stop and reason == "max_rounds"


def test_stop_on_no_new_blocking():
    r1 = round_snapshot([_d("a", OPEN, ["q1"])])
    r2 = round_snapshot([_d("a", OPEN, ["q1"])])   # 没有新增 blocking
    stop, reason = should_stop([r1, r2], max_rounds=5)
    assert stop and reason == "no_new_blocking"


def test_no_stop_when_new_blocking_appears():
    r1 = round_snapshot([_d("a", OPEN, ["q1"])])
    r2 = round_snapshot([_d("a", OPEN, ["q1", "q2"])])  # 新增 q2
    stop, _ = should_stop([r1, r2], max_rounds=5)
    assert stop is False


def test_stop_on_wording_only_three_rounds():
    # 架构签名（id|choice|status，不含 rationale/blocking）连续三快照不变 → 边际归零。
    # 为避免 no_new_blocking 抢先触发，每轮都"新增"一个 blocking（签名仍不变）。
    def snap(blocks, rat):
        return round_snapshot([Decision("a", "决策a", "选X", status=OPEN,
                                        blocking=blocks, rationale=rat)])
    r1 = snap(["q1"], "r1")
    r2 = snap(["q1", "q2"], "r2 改措辞")        # 新增 q2 → no_new_blocking 不触发
    r3 = snap(["q1", "q2", "q3"], "r3 又改措辞")  # 新增 q3 → no_new_blocking 不触发
    stop, reason = should_stop([r1, r2, r3], max_rounds=9)
    assert stop and reason == "wording_only"    # 三轮架构签名不变 → 只改措辞，停


# ── 解析 / 合并 ─────────────────────────────────────────────────────────────
def test_parse_decisions_tolerates_wrapping_and_bad_status():
    text = '废话废话 ```json\n[{"id":"d1","title":"存储","current_choice":"SQLite",' \
           '"status":"魔幻态","blocking":"还没定"}]\n``` 收尾'
    ds = parse_decisions(text)
    assert len(ds) == 1 and ds[0].id == "d1" and ds[0].current_choice == "SQLite"
    assert ds[0].status == OPEN                  # 非法 status → Open
    assert ds[0].blocking == ["还没定"]           # str blocking → 单元素列表


def test_apply_review_changes_status_and_blocking():
    ds = [_d("d1", OPEN), _d("d2", OPEN, ["旧问"])]
    review = '[{"id":"d1","status":"NeedUser","add_blocking":["要用户拍 SQLite/DuckDB"]},' \
             '{"id":"d2","status":"Accepted","resolve_blocking":["旧问"]}]'
    out = apply_review(ds, review)
    o = {d.id: d for d in out}
    assert o["d1"].status == NEEDUSER and o["d1"].blocking == ["要用户拍 SQLite/DuckDB"]
    assert o["d2"].status == ACCEPTED and o["d2"].blocking == []   # 旧问被解决


def test_apply_review_ignores_unknown_ids_and_garbage():
    ds = [_d("d1", ACCEPTED)]
    assert apply_review(ds, "not json")[0].status == ACCEPTED      # 垃圾 → 原样
    assert apply_review(ds, '[{"id":"zzz","status":"Rejected"}]')[0].status == ACCEPTED  # 未知 id 不动


# ── Consensus 渲染 ──────────────────────────────────────────────────────────
def test_render_consensus_groups_by_status_no_percent():
    ds = [_d("a", ACCEPTED), _d("b", REJECTED), _d("c", DEFERRED), _d("d", NEEDUSER)]
    out = render_consensus(ds)
    assert "Accepted" in out and "Rejected" in out and "Deferred" in out and "Need User Decision" in out
    assert "%" not in out
    assert "未决阻塞：**1**" in out               # 只有 d(NeedUser) 阻塞


# ── 端到端编排（注入假 review_fn，不碰网络）────────────────────────────────
def test_run_review_converges_and_gates():
    # reviewer 第一轮把 d1 提成 NeedUser；之后不再有新增 → 收敛
    state = {"calls": 0}

    def fake_review_fn(prompt):
        state["calls"] += 1
        if "Execution" in prompt and state["calls"] <= 2:   # 仅 execution 首轮提一次
            return '[{"id":"d1","status":"NeedUser","add_blocking":["太大，拆小"]}]'
        return "[]"                                          # 之后无意见 → 零新增 blocking

    ds = [_d("d1", OPEN), _d("d2", ACCEPTED)]
    res = run_review(ds, fake_review_fn, max_rounds=4)
    assert res["stop_reason"] in ("no_new_blocking", "max_rounds")
    g = res["gate"]
    assert g["can_start"] is False                          # d1 NeedUser → 锁
    assert g["blocking_count"] >= 1
    assert "Consensus" in res["consensus"]


def test_run_review_survives_reviewer_exception():
    def boom(prompt):
        raise RuntimeError("评审员炸了")
    res = run_review([_d("a", ACCEPTED)], boom, max_rounds=2)
    assert res["stop_reason"] == "max_rounds"               # 故障被吞，不中断
    assert res["gate"]["can_start"] is False                # 没签字


def test_build_review_prompt_carries_decisions_and_json_spec():
    p = build_review_prompt("你是评审员", [_d("d1", OPEN, ["未决X"])])
    assert "id=d1" in p and "未决X" in p and "仅输出 JSON" in p


def _run_all():
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
