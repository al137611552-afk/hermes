"""V2 评测任务集的离线自检（ADR 0027 决策 6）。

不调模型、不联网。验两件事：
1. `verify_nudges` 的**非对称判据**（误报硬断言 / 漏报软观测）；
2. **夹具本身是对的**——换手真跑那次的教训：没跑过的任务，其 fixture 也是未验代码，
   "任务挂了"要能分清是被测对象错了还是任务设定错了。

运行：python tests/test_eval_tasks.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from record import INJECTING_NUDGES, NUDGE_EVENTS  # noqa: E402
from tasks import (  # noqa: E402
    TASKS, TIERS, _check_many_modules, _check_unfixable, _setup_good_lib,
    _setup_many_modules, _setup_unfixable, verify_nudges,
)


class _R:
    def __init__(self, answer=""):
        self.answer = answer


# ---- verify_nudges：非对称判据 ------------------------------------------------

def test_false_expectation_is_a_hard_assert():
    """标 False 的 nudge 响了 = 误报 = **FAIL**。这是 L2 反例的全部意义。"""
    ok, why, _ = verify_nudges([("stuck_hint", {})], {"stuck_hint": False})
    assert ok is False and "误报" in why, why


def test_wildcard_forbids_every_injecting_nudge():
    """`{"*": False}` 覆盖所有**会插话**的 nudge——正常任务里响一个都算误报。"""
    for n in INJECTING_NUDGES:
        ok, why, _ = verify_nudges([(n, {})], {"*": False})
        assert ok is False, f"{n} 响了却没被判误报：{why}"


def test_observation_only_events_are_not_false_positives():
    """`learning_shadow` **不注入模型**（loop.py 只 append learning_advice），是纯观测。

    把它也当误报，会让任何一次正常失败都被误判成"detector 乱插话"——
    V2 端到端压测时踩到的真实问题。
    """
    assert "learning_shadow" not in INJECTING_NUDGES
    ok, why, fired = verify_nudges([("learning_shadow", {})], {"*": False})
    assert ok is True, why
    assert fired["learning_shadow"] == 1, "不判误报，但仍要计数留痕"


def test_wildcard_exempts_explicit_soft_expectations():
    """通配不能误伤正例：显式标 True 的那个不在禁止集里。"""
    ok, why, _ = verify_nudges([("stuck_hint", {})], {"stuck_hint": True, "*": False})
    assert ok is True and "stuck_hint×1" in why, why
    # 但同一任务里**别的** nudge 响了，仍然是误报
    ok2, _why2, _ = verify_nudges([("login_hint", {})], {"stuck_hint": True, "*": False})
    assert ok2 is False


def test_true_expectation_never_fails_the_task():
    """标 True 的正例**永不判 FAIL**——漏报取决于模型走不走那条坏路，逼不出来；
    硬判会把"模型这次表现好"误记成"detector 坏了"。价值在触发率，不在单次通过。"""
    ok, why, fired = verify_nudges([], {"deadend_hint": True})
    assert ok is True, why
    assert fired["deadend_hint"] == 0
    assert "deadend_hint×0" in why, "没触发也要留痕，否则算不出触发率"


def test_empty_expectation_asserts_nothing():
    """没写期望的任务（L1 冒烟）不受 nudge 影响，行为与 V2 前一致。"""
    ok, why, _ = verify_nudges([("stuck_hint", {}), ("login_hint", {})], {})
    assert ok is True and why == ""


# ---- 任务注册表体检 -----------------------------------------------------------

def test_registry_keys_match_task_names():
    for key, t in TASKS.items():
        assert key == t.name, (key, t.name)


def test_every_tier_is_known_and_l2_declares_expectations():
    for name, t in TASKS.items():
        assert t.tier in TIERS, (name, t.tier)
        if t.tier == "L2":
            assert t.expect_nudges, f"{name}：L2 任务必须声明 nudge 期望，否则它测不到 detector"


def test_negative_tasks_forbid_everything_positive_tasks_observe_one():
    """命名即契约：neg_* 是硬断言门、pos_* 是仪表。"""
    for name, t in TASKS.items():
        if name.startswith("neg_"):
            assert t.expect_nudges.get("*") is False, name
            assert not any(v is True for v in t.expect_nudges.values()), \
                f"{name}：反例不该带软观测项"
        if name.startswith("pos_"):
            softs = [k for k, v in t.expect_nudges.items() if v is True]
            assert len(softs) == 1, f"{name}：正例应恰好观测一个 nudge，实际 {softs}"
            assert t.expect_nudges.get("*") is False, \
                f"{name}：正例也要禁掉其余 nudge，否则测不出旁边那些有没有乱响"


def test_soft_observed_names_are_real_events():
    """写错事件名会让期望永远不触发、静默失效——钉住它。"""
    for name, t in TASKS.items():
        for k in t.expect_nudges:
            assert k == "*" or k in NUDGE_EVENTS, f"{name}: 未知 nudge 事件 {k!r}"


def test_l2_covers_the_offline_detectors():
    """本批（V2 批 1）覆盖三个可离线构造的 detector；联网侧留批 2。"""
    observed = {k for t in TASKS.values() for k, v in t.expect_nudges.items() if v is True}
    assert {"stuck_hint", "search_hint", "deadend_hint"} <= observed, observed


# ---- 夹具体检：没跑过的 fixture 也是未验代码 ---------------------------------

def test_unfixable_fixture_is_genuinely_unsatisfiable():
    """自相矛盾的测试**必须真的失败**——否则 pos_stuck_unfixable 压根压不出反复修改。"""
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_unfixable(ws)
        p = subprocess.run([sys.executable, "test_src.py"], cwd=ws,
                           capture_output=True, text=True, timeout=60)
        assert p.returncode != 0, "夹具没失败，这个任务就测不到任何东西"
        assert "ALL TESTS PASSED" not in (p.stdout or "")


def test_unfixable_check_rejects_test_tampering():
    """改测试蒙混过关必须判 FAIL（同 bugfix 的既有纪律）。"""
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_unfixable(ws)
        (ws / "test_src.py").write_text("print('ALL TESTS PASSED')\n", encoding="utf-8")
        ok, why = _check_unfixable(ws, _R("这题自相矛盾，无解"))
        assert ok is False and "篡改" in why, why


def test_unfixable_check_requires_honest_admission():
    """没改测试，但宣称做成了 → 也不算过。"""
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_unfixable(ws)
        assert _check_unfixable(ws, _R("已修复，测试通过了"))[0] is False
        assert _check_unfixable(ws, _R("三个断言互相矛盾，无法同时满足"))[0] is True


def test_many_modules_fixture_crosses_the_browse_gate():
    """browse nudge 有**启用门**（`search_nudge_files` 默认 40 个代码文件）。
    夹具造不够文件数，detector 根本不会被启用，这个正例就是个哑弹。"""
    from agentcore.config import AgentConfig
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_many_modules(ws)
        n = len(list((ws / "app").glob("*.py")))
        assert n >= AgentConfig().search_nudge_files, (n, AgentConfig().search_nudge_files)


def test_small_repo_fixture_stays_under_the_browse_gate():
    """反例反过来：必须**低于**门槛，否则"不该触发"就不是一个公平的期望。"""
    from agentcore.config import AgentConfig
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_good_lib(ws)
        n = len(list(ws.glob("**/*.py")))
        assert n < AgentConfig().search_nudge_files, n


def test_many_modules_check_needs_real_coverage():
    """答案要真提到多个模块，防"我大概看了一下"式糊弄。"""
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_many_modules(ws)
        assert _check_many_modules(ws, _R("看了一下，都是些工具模块"))[0] is False
        many = " ".join(f"mod_{i:02d} 负责解析" for i in range(10))
        assert _check_many_modules(ws, _R(many))[0] is True


def test_good_lib_fixture_starts_green():
    """反例任务的起点必须是绿的——起点就红，"全程不该有 nudge" 立不住。"""
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _setup_good_lib(ws)
        p = subprocess.run([sys.executable, "test_lib.py"], cwd=ws,
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and "ALL TESTS PASSED" in p.stdout, p.stderr


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
