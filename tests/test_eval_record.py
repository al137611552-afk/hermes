"""V1 Run Record + 对比报告的离线自检（ADR 0027 决策 3）。

不调模型、不碰网络——只验证**指标提炼与对比口径**本身。事件流用合成的，
因为口径正确与否跟真跑无关，而真跑既慢又要 key。

运行：python tests/test_eval_record.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from record import (  # noqa: E402
    NUDGE_EVENTS, build_record, config_snapshot, git_sha, load_run,
    model_identity, new_run_id, summarize_events, write_record,
)
from report import aggregate, compare, flat_metrics, render, render_single  # noqa: E402


def _events(**over):
    """一段有代表性的合成事件流：2 次工具调用（1 次带 logic 失败）、1 个子任务、2 个 nudge。"""
    ev = [
        ("tool_use", {"id": "t1", "name": "run_bash", "input": {"command": "pytest"}}),
        ("tool_result", {"id": "t1", "name": "run_bash", "ok": False,
                         "eval": {"score": 0.2, "error_classes": ["logic", "syntax"]}}),
        ("tool_use", {"id": "t2", "name": "read_file", "input": {"path": "a.py"}}),
        ("tool_result", {"id": "t2", "name": "read_file", "ok": True}),
        ("stuck_hint", {"text": "同一文件反复改"}),
        ("deadend_hint", {"text": "此路已 2 次不通"}),
        ("subagent_start", {"id": 1, "role": "researcher", "task": "x"}),
        ("subagent_done", {"id": 1, "ok": False, "summary": "失败"}),
        ("tool_retry", {"id": "t1", "name": "run_bash"}),
        ("usage", {"input": 100, "output": 50, "cache_read": 10, "cache_write": 0,
                   "steps": 4, "max_steps": 25, "measured": True,
                   "model": "deepseek-v4-flash", "provider": "anthropic"}),
    ]
    ev.extend(over.get("extra", []))
    return ev


class _FakeAgent:
    def __init__(self):
        self.workspace = "/tmp/heval_xyz/ws"
        self.failure_memory_db = "/x/failures.eval.db"
        self.permissions = {"allow": ["run_bash"]}
        self.max_steps = 25
        self.deadend_threshold = 2

    def model_dump(self):
        return dict(vars(self))


class _FakeModel:
    provider = "anthropic"
    model = "deepseek-v4-flash"


class _FakeCfg:
    def __init__(self):
        self.agent = _FakeAgent()
        self.active_model = "ds"

    def get_model(self, name):
        return _FakeModel()


class _FakeResult:
    def __init__(self, events, elapsed=12.5, answer="done", error=""):
        self.events = events
        self.elapsed = elapsed
        self.answer = answer
        self.error = error


# ---- summarize_events：指标提炼 ---------------------------------------------

def test_summarize_counts_core_metrics():
    m = summarize_events(_events())
    assert m["tool_calls"] == 2, m
    assert m["tool_retries"] == 1, m
    assert m["subagents"] == 1 and m["subagent_failed"] == 1, m
    assert m["steps"] == 4 and m["max_steps"] == 25, m
    assert m["tools"] == {"run_bash": 1, "read_file": 1}, m


def test_summarize_extracts_error_classes():
    """错误分类从 tool_result 的 eval 里取——这是喂 Learning 的预览。"""
    m = summarize_events(_events())
    assert m["error_classes"] == {"logic": 1, "syntax": 1}, m


def test_summarize_counts_every_nudge_kind():
    """**八种 nudge 一个都不能漏**——这是 V5 调阈值的唯一输入。"""
    ev = [(n, {"text": "x"}) for n in NUDGE_EVENTS]
    m = summarize_events(ev)
    assert m["nudges"] == {n: 1 for n in NUDGE_EVENTS}, m
    assert m["nudges_total"] == len(NUDGE_EVENTS)
    # 合成流里只触发了两种时，其余必须是 0 而不是缺键（缺键会让对比表漏行）
    m2 = summarize_events(_events())
    assert set(m2["nudges"]) == set(NUDGE_EVENTS), m2["nudges"]
    assert m2["nudges_total"] == 2 and m2["nudges"]["stuck_hint"] == 1, m2


def test_summarize_sums_usage_across_agents():
    """usage 每个 AgentLoop.run 发一次（主 + 各子 Agent）→ token 与步数是**跨 agent 合计**。"""
    ev = _events(extra=[("usage", {"input": 20, "output": 5, "steps": 2,
                                   "max_steps": 10, "measured": True})])
    m = summarize_events(ev)
    assert m["usage_events"] == 2, m
    assert m["tokens"]["input"] == 120 and m["tokens"]["output"] == 55, m
    assert m["steps"] == 6, m


def test_summarize_propagates_unmeasured_tokens():
    """任一段用量是估算的 → 整条记录标 measured=False。估算不能冒充实测（ADR 0025 决策 3）。"""
    ev = _events(extra=[("usage", {"input": 1, "output": 1, "steps": 1, "measured": False})])
    assert summarize_events(ev)["tokens_measured"] is False


def test_summarize_tolerates_junk_events():
    """事件流里混进非 dict 负载（如 chunk 是纯字符串）不能炸。"""
    m = summarize_events([("chunk", "文本"), ("thinking", "想"), ("error", "boom")])
    assert m["errors"] == 1 and m["tool_calls"] == 0


# ---- 可比性三件套 -----------------------------------------------------------

def test_config_snapshot_drops_volatile_keys():
    """每跑必变的临时路径要剔掉，否则两份记录**永远**判为配置不同、掩盖真差异。"""
    snap = config_snapshot(_FakeCfg())
    assert "workspace" not in snap["agent"], snap
    assert "failure_memory_db" not in snap["agent"], snap
    assert snap["agent"]["max_steps"] == 25 and snap["active_model"] == "ds"


def test_model_identity_records_real_model_id():
    """档名会漂，按档名对比等于没对比——必须记真实 model_id。"""
    mi = model_identity(_FakeCfg())
    assert mi["profile"] == "ds" and mi["model_id"] == "deepseek-v4-flash"


def test_model_identity_leaves_none_when_unresolvable():
    """取不到就留 None，**不编造**。"""
    class Bad:
        active_model = "x"

        def get_model(self, n):
            raise KeyError(n)
    assert model_identity(Bad())["model_id"] is None


def test_git_sha_returns_dict_and_never_raises():
    g = git_sha(ROOT)
    assert set(g) == {"sha", "dirty", "branch"}
    assert git_sha("/definitely/not/a/repo/xyz")["sha"] is None


# ---- 落盘 / 读回 ------------------------------------------------------------

def test_record_roundtrip_and_repeat_does_not_overwrite():
    """--repeat 同名任务跑多遍要各存一份，别互相覆盖（覆盖了就没法算 pass@N）。"""
    cfg = _FakeCfg()
    with tempfile.TemporaryDirectory() as d:
        for i in range(3):
            rec = build_record(task="bugfix", title="修 bug", prompt="p",
                               passed=(i != 1), why="w", result=_FakeResult(_events()),
                               cfg=cfg, git={"sha": "abc123"}, tag="base")
            write_record(d, rec)
        recs = load_run(d)
        assert len(recs) == 3, [r["task"] for r in recs]
        assert sum(1 for r in recs if r["passed"]) == 2
        assert all(r["git"]["sha"] == "abc123" for r in recs)
        assert recs[0]["metrics"]["tool_calls"] == 2
        # 必须是合法 JSON 且可读（人要能直接打开看）
        f = sorted(Path(d).glob("*.json"))[0]
        json.loads(f.read_text(encoding="utf-8"))


def test_load_run_skips_corrupt_file():
    """单条记录坏了不该毁掉整份报告。"""
    with tempfile.TemporaryDirectory() as d:
        rec = build_record(task="t", title="x", prompt="p", passed=True, why="w",
                           result=_FakeResult(_events()), cfg=_FakeCfg())
        write_record(d, rec)
        (Path(d) / "broken.json").write_text("{ not json", encoding="utf-8")
        assert len(load_run(d)) == 1


def test_new_run_id_is_sortable_and_sanitized():
    a = new_run_id("base line/../x")
    assert a.count("_") == 1 and "/" not in a and ".." not in a
    assert new_run_id()[:2] == "20"


# ---- 聚合与对比 -------------------------------------------------------------

def _rec(task="bugfix", passed=True, nudges=None, **metric_over):
    cfg = _FakeCfg()
    ev = _events()
    for n in (nudges or []):
        ev.append((n, {"text": "x"}))
    r = build_record(task=task, title="t", prompt="p", passed=passed, why="w",
                     result=_FakeResult(ev), cfg=cfg)
    r["metrics"].update(metric_over)
    return r


def test_aggregate_pass_rate_over_repeats():
    """pass 一列是 pass@1 的**比率**（n 次里过了几次），不是布尔——几个百分点才看得出来。"""
    agg = aggregate([_rec(passed=True), _rec(passed=True), _rec(passed=False)])
    assert agg["bugfix"]["n"] == 3
    assert abs(agg["bugfix"]["pass"] - 2 / 3) < 1e-9, agg


def test_flat_metrics_splits_nudges_and_error_classes():
    """nudge 与错误分类要按类拆开——合成一个总数就没法归因了。"""
    f = flat_metrics(_rec())
    assert f["nudge.stuck_hint"] == 1 and f["nudge.login_hint"] == 0
    assert f["err.logic"] == 1
    assert f["tokens"] == 160   # 100+50+10+0


def test_flat_metrics_surfaces_nudge_violation():
    """误报要成为一等指标：改动一旦开始让 detector 乱插话，diff 表必须报出来。"""
    r = _rec()
    assert flat_metrics(r)["nudge_violation"] == 0.0, "无核验信息时应记 0，别污染对比"
    r["nudge_check"] = {"ok": False, "why": "误报：stuck_hint×1"}
    assert flat_metrics(r)["nudge_violation"] == 1.0
    rows = dict((k, (b, h, d)) for k, b, h, d in
                compare(aggregate([_rec()]), aggregate([r]))["bugfix"])
    assert rows["nudge_violation"][2] == 1.0, rows


def test_compare_hides_identical_metrics():
    """无差异的指标不占版面，否则真正的变化被一屏 0 淹没。"""
    base = aggregate([_rec()])
    rows = compare(base, aggregate([_rec()]))["bugfix"]
    assert rows == [], rows


def test_compare_surfaces_a_changed_detector():
    """**ADR 0027 V1 验收判据**：故意让某个 detector 多触发 → 对比表里对应那一列必须明显变化。

    这是报表的**活性自检**——报表本身也可能是坏的，得有东西守着它真能报出差异。
    """
    base = aggregate([_rec(nudges=[])])
    head = aggregate([_rec(nudges=["stuck_hint", "stuck_hint", "stuck_hint"])])
    rows = dict((k, (b, h, d)) for k, b, h, d in compare(base, head)["bugfix"])
    assert "nudge.stuck_hint" in rows, rows
    b, h, d = rows["nudge.stuck_hint"]
    assert b == 1 and h == 4 and d == 3, rows["nudge.stuck_hint"]
    assert "nudges_total" in rows
    print("✓ 报表活性：detector 触发次数变化能被对比表报出来（V1 验收）")


def test_compare_handles_task_only_in_one_side():
    """一边新增了任务：照列，另一边记 None，不能崩也不能假装为 0。"""
    cmp_ = compare(aggregate([_rec(task="a")]), aggregate([_rec(task="a"), _rec(task="b")]))
    assert "b" in cmp_
    assert all(b is None for _k, b, _h, _d in cmp_["b"]), cmp_["b"]


def test_render_outputs_text_without_crashing():
    base, head = aggregate([_rec()]), aggregate([_rec(passed=False, nudges=["login_hint"])])
    out = render(compare(base, head), "run-a", "run-b")
    assert "run-a" in out and "nudge.login_hint" in out and "pass" in out
    assert "总 pass@1" in render_single(base, "run-a")


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
