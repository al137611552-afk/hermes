"""块 V5（detector 计分板与阈值扫描）的离线自检。

不调模型、不联网。V5 的全部前提是**detector 是纯函数**，因而可以拿录音里的轨迹
离线重放任意阈值。这里钉住三件让那个前提成立的事：

1. **分批口径**：事件流重组成"步"必须与主循环一致（一次 `_exec_calls` 是一步）。
   拆错步会把 `detect_stuck_edit` 的"本步有没有失败信号"判飞——它看的是**整批**输出。
2. **重放不许留下全局副作用**：`search_hint` 的阈值是模块常量，重放要改它，
   改完必须还原。漏还原会让**同一个进程里后续所有跑**都用错阈值（评测自己污染自己）。
3. **三列表的口径**与纪律文案（误报比漏报贵；第三列是观测不是实验）。

运行：python tests/test_eval_detectors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from detectors import (  # noqa: E402
    REPLAYERS, improved_after, positive_for, render_board, render_sweep, replay_browse,
    replay_deadend, replay_stuck, steps_from_events, task_role,
)
from tasks import TASKS  # noqa: E402


def _use(cid, name, params=None):
    return ("tool_use", {"id": cid, "name": name, "input": params or {}})


def _res(cid, output="", issues=None):
    d = {"id": cid, "output": output}
    if issues is not None:
        d["eval"] = {"issues": list(issues)}
    return ("tool_result", d)


# ---- 分批口径 -----------------------------------------------------------------

def test_two_calls_in_one_batch_stay_one_step():
    """实测的真实顺序是 use/use/result/result（一次 `_exec_calls` 执行一批），
    不是 use/result/use/result。拆成两步会改变 detector 看到的世界。"""
    steps = steps_from_events([_use("a", "read_file"), _use("b", "read_file"),
                               _res("a", "x"), _res("b", "y")])
    assert len(steps) == 1
    assert [c.name for c in steps[0]["calls"]] == ["read_file", "read_file"]
    assert steps[0]["out"] == {"a": "x", "b": "y"}


def test_sequential_calls_split_into_steps():
    steps = steps_from_events([_use("a", "run_bash"), _res("a", "1"),
                               _use("b", "run_bash"), _res("b", "2")])
    assert len(steps) == 2 and steps[1]["out"] == {"b": "2"}


def test_nudges_attach_to_the_step_that_produced_them():
    steps = steps_from_events([_use("a", "edit_file"), _res("a", "AssertionError"),
                               ("stuck_hint", {"text": "…"}),
                               _use("b", "run_bash"), _res("b", "ok")])
    assert len(steps) == 2
    assert steps[0]["nudges"] == ["stuck_hint"] and steps[1]["nudges"] == []


def test_step_grouping_matters_for_stuck_detection():
    """同一步里"改文件 + 另一个调用报失败" → 有失败信号 → 会触发；
    拆成两步则那一步自己没有失败信号 → 不触发。这就是分批口径必须对的原因。"""
    ev = [_use("a", "edit_file", {"path": "x.py"}), _use("b", "run_bash"),
          _res("a", "written"), _res("b", "AssertionError: boom")]
    one = steps_from_events(ev)
    assert len(one) == 1
    assert replay_stuck(one, 1) == 1
    split = [{"calls": [c], "out": {c.id: one[0]["out"][c.id]}, "evals": {}, "nudges": []}
             for c in one[0]["calls"]]
    assert replay_stuck(split, 1) == 0


# ---- 阈值重放：单调、无副作用 --------------------------------------------------

def _edit_fail_steps(n):
    out = []
    for i in range(n):
        cid = f"c{i}"
        out.append({"calls": [type("C", (), {"id": cid, "name": "edit_file",
                                             "input": {"path": "x.py"}})()],
                    "out": {cid: "AssertionError: still failing"}, "evals": {}, "nudges": []})
    return out


def test_lower_threshold_never_fires_less():
    """阈值越低越容易响——单调性是扫描表可读的前提，反了说明重放接错了线。"""
    steps = _edit_fail_steps(4)
    counts = [replay_stuck(steps, t) for t in (1, 2, 3, 4, 5)]
    assert counts == sorted(counts, reverse=True), counts
    assert counts[0] >= 1 and counts[-1] == 0, counts


def test_browse_replay_restores_the_module_constant():
    """`search_hint` 的阈值是模块常量。重放改了它却不还原，**同一进程里后续所有跑
    都会用错阈值**——评测自己污染自己，而且症状是"某些任务偶发不一致"，极难查。"""
    from agentcore.agent import loop as loop_mod

    before = loop_mod._BROWSE_NUDGE_AT
    steps = [{"calls": [type("C", (), {"id": f"c{i}", "name": "read_file",
                                       "input": {"path": f"f{i}.py"}})()],
              "out": {f"c{i}": "..."}, "evals": {}, "nudges": []} for i in range(10)]
    assert replay_browse(steps, 3) == 1
    assert loop_mod._BROWSE_NUDGE_AT == before, "重放没还原模块常量"
    assert replay_browse(steps, 99) == 0
    assert loop_mod._BROWSE_NUDGE_AT == before


def test_deadend_replay_counts_repeated_failures():
    same = [{"calls": [type("C", (), {"id": f"c{i}", "name": "run_bash",
                                      "input": {"command": "acme-build"}})()],
             "out": {f"c{i}": "[exit code] 127\nacme-build: command not found"},
             "evals": {}, "nudges": []} for i in range(3)]
    assert replay_deadend(same, 1) >= 1
    assert replay_deadend(same, 9) == 0


# ---- 第三列：观测口径 ---------------------------------------------------------

def test_improved_after_only_counts_actual_firings():
    steps = steps_from_events([
        _use("a", "run_bash"), _res("a", "boom", issues=["x", "y"]),
        ("stuck_hint", {}),
        _use("b", "run_bash"), _res("b", "ok", issues=[]),
    ])
    assert improved_after(steps, "stuck_hint") == (1, 1)
    assert improved_after(steps, "search_hint") == (0, 0)   # 没响过就没有样本


def test_improved_after_reports_no_improvement():
    steps = steps_from_events([
        _use("a", "run_bash"), _res("a", "boom", issues=["x"]),
        ("deadend_hint", {}),
        _use("b", "run_bash"), _res("b", "boom again", issues=["x", "y"]),
    ])
    assert improved_after(steps, "deadend_hint") == (0, 1)


# ---- 任务身份与报表纪律 -------------------------------------------------------

def test_a_task_can_be_positive_for_one_detector_and_negative_for_the_rest():
    """`{"login_hint": True, "*": False}` 的意思正是「对 login 是正例、对其余是反例」——
    所以身份必须**按 detector 分别问**，不能给任务贴一个全局标签。

    计分板据此把每个 detector 的反例集合定义为"是反例、且不是这个 detector 的正例"，
    否则 `pos_login_wall` 会被自己算成 login_hint 的误报。
    """
    assert task_role("neg_plain_fix") == "neg"
    assert task_role("pos_login_wall") == "neg"          # 对**其余** nudge 而言是反例
    assert positive_for("pos_login_wall", "login_hint") is True    # 对 login 是正例
    assert positive_for("pos_login_wall", "stuck_hint") is False
    assert positive_for("neg_plain_fix", "login_hint") is False


def test_board_puts_false_positives_first():
    md = render_board([{"name": "stuck_hint", "fp": 0, "neg_total": 10, "pos_fired": 0,
                        "pos_total": 1, "improved": 0, "fired": 0}])
    head = md.splitlines()[2]
    assert head.index("误报") < head.index("触发率"), head
    assert "误报比漏报贵" in md
    assert "观测" in md and "样本少时别当结论" in md


def test_sweep_states_what_it_cannot_answer():
    """扫描回答"会不会响"，**不回答"响了之后模型会不会变好"**——那需要真跑。
    报表必须自己说清楚边界，否则读的人会拿它当 A/B 结论。"""
    md = render_sweep({"stuck_hint": {"knob": "stuck_edit_threshold", "current": 3,
                                      "pos_total": 1, "neg_total": 10,
                                      "values": {2: {"pos": 0, "neg": 0},
                                                 3: {"pos": 0, "neg": 0}}}})
    assert "←当前" in md and "不回答" in md and "cassette 当场 miss" in md


def test_every_swept_detector_has_a_real_knob():
    from agentcore.config import AgentConfig

    for det, (knob, _fn, values) in REPLAYERS.items():
        assert det in {"stuck_hint", "search_hint", "deadend_hint"}
        assert len(values) >= 3, det
        if knob.startswith("_"):
            from agentcore.agent import loop as loop_mod
            assert hasattr(loop_mod, knob), knob          # 模块常量
        else:
            assert hasattr(AgentConfig(), knob), knob     # 配置项


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
