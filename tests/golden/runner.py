"""块F Golden runner —— 重放真实决策函数，比对 cases.py 的期望，回归即报。

可独立跑：`python tests/golden/runner.py`（退出码非零=有回归）；
也被 `tests/test_golden.py` 调用并入"全回归"。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parents[1] / "src"
for _p in (str(_SRC), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cases import CASES  # noqa: E402  (tests/golden 已在 sys.path)


def _check_need(c):
    from agentcore.agent.contract import verdict_to_need
    got = verdict_to_need(c["verdict"]).value
    return got == c["expect"], got


def _check_evaluate(c):
    from agentcore.agent.evaluators import evaluate
    ev = evaluate(c["tool"], c["output"], c.get("params"))
    exp = c["expect"]
    got = {"has_issues": bool(ev.issues) if ev is not None else False}
    ok = got["has_issues"] == exp["has_issues"]
    if "metric" in exp and ev is not None:
        k, v = exp["metric"]
        got[f"metric:{k}"] = ev.metrics.get(k)
        ok = ok and ev.metrics.get(k) == v
    elif "metric" in exp:
        ok = False
    return ok, got


def _check_classify(c):
    from agentcore.agent.taxonomy import classify_text
    cls = classify_text(c["text"])
    got = cls[0].value if cls else None
    return got == c["expect"], got


def _check_retry(c):
    from agentcore.agent.policy import decide_retry
    from agentcore.agent.taxonomy import ErrorClass
    classes = [ErrorClass(x) for x in c["classes"]]
    d = decide_retry(classes, c["attempts"], max_attempts=c["max"], backoff_base=c["base"])
    exp = c["expect"]
    if not exp["retry"]:
        return d is None, {"retry": d is not None}
    got = {"retry": d is not None, "delay": (d.delay if d else None)}
    return (d is not None and d.delay == exp["delay"]), got


def _check_deadend(c):
    from agentcore.agent.loop import detect_repeated_failure
    from agentcore.agent.world_state import WorldState, FailureMemory
    fm = FailureMemory(Path(tempfile.mkdtemp()) / "golden_fm.db")
    world, nudged = WorldState(), set()

    class _Call:
        def __init__(self, i, tool, params):
            self.id, self.name, self.input = str(i), tool, params
    first_at = None
    for i in range(1, c["repeat"] + 1):
        msg = detect_repeated_failure(
            [_Call(i, c["tool"], c["params"])], {str(i): c["output"]},
            world, fm, nudged, threshold=c["threshold"])
        if msg is not None and first_at is None:
            first_at = i
    got = {"first_nudge_at": first_at}
    return first_at == c["expect"]["first_nudge_at"], got


def _check_learn(c):
    # 块G：历史失败行 → 候选策略生成的确定性边界（系统性才升级）。
    from agentcore.agent.learning import aggregate, propose
    from agentcore.agent.world_state import FailureMemory
    fm = FailureMemory(Path(tempfile.mkdtemp()) / "golden_learn.db")
    for r in c["rows"]:
        fm.record(r["fp"], [r["class"]], detail=r.get("detail", ""))
    cands = propose(aggregate(fm), min_count=c.get("min_count", 3),
                    min_paths=c.get("min_paths", 2))
    fm.close()
    classes = sorted(x.error_class for x in cands)
    got = {"classes": classes}
    return classes == c["expect"]["classes"], got


_DISPATCH = {
    "need": _check_need, "evaluate": _check_evaluate, "classify": _check_classify,
    "retry": _check_retry, "deadend": _check_deadend, "learn": _check_learn,
}


def run(cases=None):
    """跑全部语料，返回 {passed, total, failures:[{id,kind,expect,got}]}。"""
    cases = cases if cases is not None else CASES
    failures = []
    passed = 0
    for c in cases:
        fn = _DISPATCH.get(c["kind"])
        if fn is None:
            failures.append({"id": c["id"], "kind": c["kind"], "expect": "<known kind>",
                             "got": "UNKNOWN_KIND"})
            continue
        try:
            ok, got = fn(c)
        except Exception as e:  # noqa: BLE001 —— 异常即回归
            ok, got = False, f"EXC: {type(e).__name__}: {e}"
        if ok:
            passed += 1
        else:
            failures.append({"id": c["id"], "kind": c["kind"],
                             "expect": c["expect"], "got": got})
    return {"passed": passed, "total": len(cases), "failures": failures}


def main():
    res = run()
    for f in res["failures"]:
        print(f"  ❌ {f['id']} [{f['kind']}] expect={f['expect']!r} got={f['got']!r}")
    print(f"\nGolden: {res['passed']}/{res['total']} passed, {len(res['failures'])} 回归")
    return 0 if not res["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
