"""评估/策略契约（docs/adr/0014）自测——纯逻辑。

块 A 验收的一部分：证明 Need 枚举/Evaluation 容器/verdict→Need 映射成立，
且映射与现有 crazy 分支语义一致（不引入行为变化）。

运行：python tests/test_contract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.contract import (  # noqa: E402
    NUDGE_BROWSE, NUDGE_LOGIN, NUDGE_STUCK, Evaluation, Need, verdict_to_need,
)


# ---- Need 枚举：只许是差距，绝不含动作（ADR 决策第 3 条）-----------------------

def test_need_enum_has_no_action_members():
    names = {n.name for n in Need}
    # 这些是 Decision（做法），出现在 Need 里即违反契约
    for forbidden in ("NEED_REPLANNING", "RETRY_SAME", "SWITCH_TOOL", "REPLAN"):
        assert forbidden not in names, f"Need 不应含动作成员 {forbidden}"


def test_need_enum_is_the_agreed_nine():
    assert {n.name for n in Need} == {
        "CONTINUE", "NEED_INFORMATION", "NEED_EXECUTION", "NEED_VALIDATION",
        "PROGRESS_STALLED", "APPROACH_INVALIDATED", "NEED_USER_INPUT",
        "GOAL_BLOCKED", "GOAL_SATISFIED",
    }


def test_need_is_str_for_json():
    assert Need.GOAL_SATISFIED.value == "goal_satisfied"
    assert Need.NEED_USER_INPUT == "need_user_input"   # str 混入，可直接进事件


# ---- verdict → Need 映射：与现有 crazy 分支语义一致 ----------------------------

def test_verdict_to_need_mapping():
    assert verdict_to_need("done") is Need.GOAL_SATISFIED
    assert verdict_to_need("phase_done") is Need.GOAL_SATISFIED   # 子目标达成
    assert verdict_to_need("continue") is Need.CONTINUE
    assert verdict_to_need("need_user") is Need.NEED_USER_INPUT


def test_verdict_to_need_unknown_is_continue():
    # 无标记 / None → 继续推进（与"无标记则再跑一轮"现状一致）
    assert verdict_to_need(None) is Need.CONTINUE
    assert verdict_to_need("") is Need.CONTINUE
    assert verdict_to_need("garbage") is Need.CONTINUE


# ---- Evaluation：只装事实，可序列化成事件 -------------------------------------

def test_evaluation_defaults_empty():
    e = Evaluation()
    assert e.metrics == {} and e.signals == [] and e.issues == []
    assert e.confidence == 1.0


def test_evaluation_as_event_is_plain_dict():
    e = Evaluation(metrics={"pass": 3, "total": 5}, signals=["返回 0 条"],
                   issues=["测试未全过=blocker"], confidence=0.8)
    ev = e.as_event()
    assert ev == {"metrics": {"pass": 3, "total": 5}, "signals": ["返回 0 条"],
                  "issues": ["测试未全过=blocker"], "confidence": 0.8}
    # as_event 出来的是拷贝，改它不动原对象（事件层与内部状态解耦）
    ev["signals"].append("x")
    assert e.signals == ["返回 0 条"]


def test_evaluation_has_no_score_field():
    # Score 只是事实投影、绝不回喂决策，故契约里不存它（ADR 决策第 1 条）
    assert not hasattr(Evaluation(), "score")


# ---- loop.py 各 nudge 对应的 Need（块A 路由：探测→Need→注入）------------------

def test_nudge_needs_are_gaps():
    assert NUDGE_BROWSE is Need.NEED_INFORMATION
    assert NUDGE_STUCK is Need.PROGRESS_STALLED
    assert NUDGE_LOGIN is Need.NEED_USER_INPUT


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
