"""块F：Golden 回归门入口——并入"全回归"（python tests/test_golden.py）。

1) 全部 Golden 语料必须通过（决策内核行为未偏离基线）。
2) 门必须"活的"——能抓回归（注入一条错误期望，runner 应报红），防门形同虚设。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "golden"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runner import run  # noqa: E402


def test_golden_dataset_all_pass():
    res = run()
    assert res["failures"] == [], f"Golden 回归：{res['failures']}"
    assert res["total"] >= 20            # 语料别被悄悄删空


def test_golden_gate_catches_regression():
    # 故意劣化一条期望 → 门必须报红（验收：劣化策略 golden 能红）
    bad = [{"id": "deliberately-wrong", "kind": "need", "verdict": "done",
            "expect": "continue"}]      # done 实际 → goal_satisfied，这里写错
    res = run(bad)
    assert len(res["failures"]) == 1 and res["passed"] == 0


def test_golden_unknown_kind_is_failure():
    res = run([{"id": "x", "kind": "no_such_kind", "expect": None}])
    assert len(res["failures"]) == 1


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
