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


def _check_research_judge(c):
    # 块H3a/H3c：web_search 结果经模型裁判 → 三态决策（不对题重搜 / 部分污染萃取 / 对题静默）。
    # 用"假裁判"（注入固定 verdict JSON）锁住决策分类，不连真模型。
    from agentcore.agent.loop import detect_offtarget_research

    class _Call:
        def __init__(self, params):
            self.id, self.name, self.input = "1", "web_search", params
    msg = detect_offtarget_research(
        [_Call({"query": c["query"]})], {"1": c["output"]},
        c["goal"], lambda p, i: c["verdict"], {}, 1)
    got = ("none" if msg is None else
           "salvage" if "部分有效" in msg else
           "offtarget" if "基本不对题" in msg else "other")
    return got == c["expect"], got


def _check_grounding(c):
    # 块H3c：接地/时效闸（纯正则）——做过搜索+时效敏感+既无引用又无声明 → 触发。
    from agentcore.agent.loop import detect_ungrounded_answer
    got = detect_ungrounded_answer(c["goal"], c["answer"], c["did_research"]) is not None
    return got == c["expect"], got


def _check_switch(c):
    # 块H 换源策略阶梯：NO_PROGRESS 时逐级 site→browser→ask_user，越界 None。
    from agentcore.agent.loop import switch_strategy_nudge
    msg = switch_strategy_nudge(c["step"])
    got = ("none" if msg is None else
           "site_filter" if "site:" in msg else
           "browser" if "浏览器直通" in msg else
           "ask_user" if "ask_user" in msg else "other")
    return got == c["expect"], got


def _check_novelty(c):
    # 块H Novelty：搜索结果文本 → 去重/归一后的域名集（确定性事实，无模型无分数）。
    from agentcore.agent.loop import extract_domains
    got = sorted(extract_domains(c["text"]))
    return got == c["expect"], {"domains": got}


def _check_consensus_gate(c):
    # ADR 0019：开工 gate = 可数事实"未决阻塞==0" 且 用户签字。绝不换算百分比。
    # case 给 decisions（[{id,status,blocking}]）+ signed → 期望 can_start（locked/open）。
    from agentcore.agent.design_review import Decision, gate_status
    ds = [Decision(id=d["id"], title=d["id"], status=d.get("status", "Open"),
                   blocking=list(d.get("blocking", []))) for d in c["decisions"]]
    g = gate_status(ds, user_signed=c["signed"])
    assert "%" not in g["reason"]                       # 活性：门理由里出现百分比即回归
    got = "open" if g["can_start"] else "locked"
    return got == c["expect"], {"got": got, "blocking": g["blocking_count"]}


def _check_review_stop(c):
    # ADR 0019：评审停止条件全部可证伪、可数（轮数 / 零新增 blocking / 连两轮只改措辞）。
    from agentcore.agent.design_review import Decision, round_snapshot, should_stop
    rounds = [round_snapshot([Decision(id=d["id"], title=d["id"],
                                       current_choice=d.get("choice", "x"),
                                       status=d.get("status", "Open"),
                                       blocking=list(d.get("blocking", [])))
                              for d in snap])
              for snap in c["rounds"]]
    stop, reason = should_stop(rounds, max_rounds=c.get("max_rounds", 3))
    got = reason if stop else "continue"
    return got == c["expect"], got


_DISPATCH = {
    "need": _check_need, "evaluate": _check_evaluate, "classify": _check_classify,
    "retry": _check_retry, "deadend": _check_deadend, "learn": _check_learn,
    "research_judge": _check_research_judge, "grounding": _check_grounding,
    "switch": _check_switch, "novelty": _check_novelty,
    "consensus_gate": _check_consensus_gate, "review_stop": _check_review_stop,
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
