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
    ACCEPTED, DEFERRED, NEEDUSER, OPEN, REJECTED, Decision, DesignReviewSession,
    apply_review, build_review_prompt, can_start_coding, count_blocking,
    diagnose_decisions, escalate_unresolved, gate_status,
    make_review_fn, parse_decisions, render_consensus, round_snapshot, run_review,
    should_stop,
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


def test_diagnose_decisions_distinguishes_empty_from_nojson():
    # 合法但空数组 → 'empty'（方案无架构级取舍，纯执行清单，不该报"输出非预期"）
    assert diagnose_decisions("这方案没啥架构分歧：[]") == "empty"
    # 抠到至少一条 → 'ok'
    assert diagnose_decisions('前言 [{"id":"d1","title":"存储"}] 收尾') == "ok"
    # 单对象也算 ok
    assert diagnose_decisions('{"id":"d1","title":"存储"}') == "ok"
    # 大白话/截断没闭合，抠不到 JSON → 'nojson'
    assert diagnose_decisions("我觉得这个方案挺好的，可以直接开工。") == "nojson"
    assert diagnose_decisions('[{"id":"d1","title":"存储"') == "nojson"   # 截断未闭合


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

    def fake_review_fn(name, prompt):                       # seam 现在带 reviewer 名
        state["calls"] += 1
        if name == "product" and state["calls"] <= 2:     # 仅 product 首轮提一次
            return '[{"id":"d1","status":"NeedUser","add_blocking":["太大，拆小"]}]'
        return "[]"                                          # 之后无意见 → 零新增 blocking

    ds = [_d("d1", OPEN), _d("d2", ACCEPTED)]
    res = run_review(ds, fake_review_fn, max_rounds=4)
    assert res["stop_reason"] in ("no_new_blocking", "max_rounds")
    g = res["gate"]
    assert g["can_start"] is False                          # d1 NeedUser → 锁
    assert g["blocking_count"] >= 1
    assert "Consensus" in res["consensus"]


def test_run_review_routes_by_reviewer_name_heterogeneous():
    # 异构 seam：引擎按 name 喊 reviewer，接线层可据 name 路由到不同"模型"。
    seen = []

    def routing_review_fn(name, prompt):
        seen.append(name)                                   # 记录两个角色都被分别调用
        return "[]"
    run_review([_d("a", ACCEPTED)], routing_review_fn, max_rounds=2)
    assert "product" in seen and "technical" in seen   # 两脑子分别按名被调，可各接各模型


def test_escalate_open_to_needuser_preserves_others():
    ds = [_d("a", OPEN), _d("b", OPEN, blocking=["未决"]), _d("c", ACCEPTED),
          _d("d", REJECTED), _d("e", DEFERRED)]
    out = {d.id: d for d in escalate_unresolved(ds)}
    assert out["a"].status == NEEDUSER                       # 纯 Open → 升级
    assert out["b"].status == NEEDUSER and out["b"].blocking == ["未决"]  # Open+blocking → 升级且留 blocking
    assert out["c"].status == ACCEPTED                       # 四态不动
    assert out["d"].status == REJECTED and out["e"].status == DEFERRED


def test_run_review_leftover_open_becomes_needuser_actionable():
    # 评审员从不表态 → 收敛时全是 Open；escalate 后应成 NeedUser（前端才有拍板入口），gate 仍锁。
    res = run_review([_d("a", OPEN), _d("b", OPEN)], lambda name, prompt: "[]", max_rounds=2)
    assert all(d.status == NEEDUSER for d in res["decisions"])   # 无死 Open，全部可拍板
    assert res["gate"]["can_start"] is False                     # 依旧不自动放行（守 ADR 0014）


def test_run_reviewers_parallel_preserves_order_and_isolates_failures():
    from agentcore.agent.design_review import _run_reviewers_parallel

    def rf(name, prompt):
        if name == "boom":
            raise RuntimeError("x")
        return name.upper()
    outs = _run_reviewers_parallel(rf, [("a", "p"), ("boom", "p"), ("b", "p")])
    assert outs == ["A", "[]", "B"]                 # 同序返回；中间故障→"[]" 不影响其它


def test_run_reviewers_parallel_timeout_yields_empty():
    import time
    from agentcore.agent.design_review import _run_reviewers_parallel

    def slow(name, prompt):
        time.sleep(0.3)
        return "late"
    outs = _run_reviewers_parallel(slow, [("a", "p")], timeout=0.05)
    assert outs == ["[]"]                            # 超时按空评审跳过，不无限等


def test_make_review_fn_bounds_output_tokens():
    seen = {}

    class P:
        def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
            seen["mt"] = max_tokens
            yield _FakeEv("[]")
    from agentcore.agent.design_review import REVIEW_MAX_TOKENS
    make_review_fn(lambda name: P())("product", "prompt")
    assert seen["mt"] == REVIEW_MAX_TOKENS           # 评审调用限了输出长度（提速）


def test_run_review_survives_reviewer_exception():
    def boom(name, prompt):
        raise RuntimeError("评审员炸了")
    res = run_review([_d("a", ACCEPTED)], boom, max_rounds=2)
    assert res["stop_reason"] == "max_rounds"               # 故障被吞，不中断
    assert res["gate"]["can_start"] is False                # 没签字


def test_build_review_prompt_carries_decisions_and_json_spec():
    p = build_review_prompt("你是评审员", [_d("d1", OPEN, ["未决X"])])
    assert "id=d1" in p and "未决X" in p and "结论 JSON 数组" in p


# ── IO 适配器 make_review_fn（假 provider，不碰网络）─────────────────────────
class _FakeEv:
    def __init__(self, text):
        self.type, self.text = "text", text


class _FakeProvider:
    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        self.seen.append(messages[0].content)
        yield _FakeEv(self.reply)


def test_make_review_fn_calls_provider_and_returns_text():
    p = _FakeProvider('[{"id":"d1","status":"Accepted"}]')
    rf = make_review_fn(lambda name: p)
    assert rf("product", "评一下") == '[{"id":"d1","status":"Accepted"}]'
    assert p.seen and "评一下" in str(p.seen[0])


def test_make_review_fn_routes_by_name_heterogeneous():
    # 异构：product → provider A，technical → provider B（接线层据 name 选不同模型档案）
    a = _FakeProvider('[{"id":"x","status":"Accepted"}]')
    b = _FakeProvider('[{"id":"x","status":"NeedUser"}]')
    rf = make_review_fn(lambda name: a if name == "product" else b)
    assert "Accepted" in rf("product", "p") and "NeedUser" in rf("technical", "p")


def test_make_review_fn_none_provider_skips():
    rf = make_review_fn(lambda name: None)        # 没配该角色模型 → 跳过不阻断
    assert rf("technical", "p") == "[]"


# ── DesignReviewSession 状态机 ──────────────────────────────────────────────
def test_session_from_proposal_parses_decisions():
    s = DesignReviewSession.from_proposal('[{"id":"d1","title":"存储","current_choice":"SQLite"}]')
    assert len(s.decisions) == 1 and s.decisions[0].id == "d1"
    assert s.can_start() is False                 # Open → 阻塞


def test_session_review_then_resolve_then_sign_opens_gate():
    s = DesignReviewSession([_d("d1", OPEN), _d("d2", ACCEPTED)], max_rounds=3)

    def rf(name, prompt):
        return '[{"id":"d1","status":"NeedUser","add_blocking":["要拍板"]}]' if name == "product" \
            else "[]"
    s.review(rf)
    assert s.gate()["blocking_count"] >= 1 and s.can_start() is False   # d1 待拍板
    # 用户拍板 d1 → 清空 blocking、定稿；签字 → 开
    assert s.resolve("d1", ACCEPTED, current_choice="方案X") is True
    assert s.decisions[0].current_choice == "方案X" and s.decisions[0].blocking == []
    assert s.can_start() is False                 # 还没签字
    s.sign()
    assert s.can_start() is True


def test_session_resolve_rejects_bad_status_and_resets_signature():
    s = DesignReviewSession([_d("d1", ACCEPTED)])
    s.sign()
    assert s.can_start() is True
    assert s.resolve("d1", "魔幻态") is False       # 非四态 → 拒绝、不改
    assert s.resolve("nope", ACCEPTED) is False     # 未知 id → False
    assert s.can_start() is True                    # 上面都没改 → 签字仍有效
    assert s.resolve("d1", DEFERRED) is True        # 合法改动
    assert s.signed is False and s.can_start() is False  # 改后作废签字（防签完偷改）


def test_run_review_emits_streaming_events():
    # v4 实时流式：run_review 逐轮/逐角色发进度事件，供 conversation 转前端分屏
    events = []

    def rf(name, prompt):
        return '```json\n[{"id":"d1","status":"Accepted"}]\n```' if name == "product" else "[]"
    run_review([_d("d1", OPEN)], rf, max_rounds=2,
               on_event=lambda kind, p: events.append((kind, p)))
    kinds = [k for k, _ in events]
    assert "round_start" in kinds and "reviewer_done" in kinds and "converged" in kinds
    assert any(p.get("reviewer") == "product" for k, p in events if k == "reviewer_done")


def test_apply_review_parses_prose_then_json():
    # v4：reviewer 先写散文意见、末尾给 ```json 结论——取最后一个数组，散文里的方括号不该被误取
    ds = [_d("d1", OPEN)]
    verdict = ("我认为当前选择在维护成本上有风险，建议升级给用户拍板。\n"
               '```json\n[{"id":"d1","status":"NeedUser","add_blocking":["需拍板"]}]\n```')
    out = apply_review(ds, verdict)
    assert out[0].status == "NeedUser" and out[0].blocking == ["需拍板"]


def _run_all():
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
