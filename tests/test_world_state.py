"""块E 自测：WorldState（单会话事实）+ FailureMemory（跨会话死路记忆）。

运行：python tests/test_world_state.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.world_state import (  # noqa: E402
    FailureMemory, WorldState, fingerprint,
)
from agentcore.agent.taxonomy import ErrorClass  # noqa: E402


# ---- fingerprint：稳定 + 区分同/异路 -----------------------------------------

def test_fingerprint_stable_and_normalized():
    a = fingerprint("run_powershell", {"command": "pytest  -q"})
    b = fingerprint("run_powershell", {"command": "PYTEST -Q"})   # 大小写/空白归一
    assert a == b
    assert len(a) == 16


def test_fingerprint_distinguishes_paths():
    a = fingerprint("run_powershell", {"command": "pytest tests/a"})
    b = fingerprint("run_powershell", {"command": "pytest tests/b"})
    assert a != b
    # 工具名不同也算不同路
    assert fingerprint("read_file", {"path": "x"}) != fingerprint("write_file", {"path": "x"})


def test_fingerprint_ignores_irrelevant_params():
    a = fingerprint("run_powershell", {"command": "ls", "background": True})
    b = fingerprint("run_powershell", {"command": "ls", "background": False})
    assert a == b   # background 不在关键入参里


# ---- WorldState：单会话累积 --------------------------------------------------

def test_worldstate_counts_repeated_failures():
    ws = WorldState()
    fp = fingerprint("run_powershell", {"command": "bad"})
    assert ws.record_failure(fp, [ErrorClass.LOGIC]) == 1
    assert ws.record_failure(fp, [ErrorClass.LOGIC]) == 2
    assert ws.failures_for(fp) == 2
    assert ws.failures_for("other") == 0
    assert ws.classes_for(fp) == ("logic",)


def test_worldstate_need_history_and_invalidate():
    ws = WorldState()
    ws.record_need(ErrorClass.LOGIC)            # 任意带 .value 的枚举都行
    ws.record_need("continue")
    assert ws.need_history == ["logic", "continue"]
    ws.invalidate("用 sqlite fts 全文检索")
    ws.invalidate("用 sqlite fts 全文检索")      # 去重
    assert ws.invalidated == ["用 sqlite fts 全文检索"]
    ws.block("缺少 API key")
    assert ws.blocked == ["缺少 API key"]


# ---- FailureMemory：跨会话持久 ----------------------------------------------

def _mem():
    d = tempfile.mkdtemp()
    return FailureMemory(Path(d) / "fm.db")


def test_failurememory_record_and_count():
    fm = _mem()
    fp = fingerprint("run_powershell", {"command": "x"})
    fm.record(fp, [ErrorClass.NOT_FOUND], decision="RUN_AS_IS")
    fm.record(fp, [ErrorClass.NOT_FOUND], decision="RUN_AS_IS")
    assert fm.count_for(fp) == 2
    assert fm.count_for(fp, ErrorClass.NOT_FOUND) == 2
    assert fm.count_for(fp, ErrorClass.LOGIC) == 0


def test_failurememory_known_deadend_threshold():
    fm = _mem()
    fp = fingerprint("run_powershell", {"command": "y"})
    fm.record(fp, [ErrorClass.LOGIC])
    assert fm.known_deadend(fp, threshold=2) is None      # 1 次 < 阈值
    fm.record(fp, [ErrorClass.LOGIC])
    hit = fm.known_deadend(fp, threshold=2)
    assert hit is not None and hit[0] == 2 and hit[1] == "logic"
    assert fm.known_deadend("never_seen") is None


def test_failurememory_dominant_class():
    fm = _mem()
    fp = fingerprint("read_file", {"path": "z"})
    fm.record(fp, [ErrorClass.NOT_FOUND])
    fm.record(fp, [ErrorClass.NOT_FOUND])
    fm.record(fp, [ErrorClass.SYNTAX])
    total, dom = fm.known_deadend(fp, threshold=2)
    assert total == 3 and dom == "not_found"              # 主分类=出现最多的


def test_failurememory_persists_across_reopen():
    d = tempfile.mkdtemp()
    path = Path(d) / "fm.db"
    fp = fingerprint("run_powershell", {"command": "persist"})
    fm1 = FailureMemory(path)
    fm1.record(fp, [ErrorClass.AMBIGUOUS])
    fm1.record(fp, [ErrorClass.AMBIGUOUS])
    fm1.close()
    fm2 = FailureMemory(path)                              # 重开同文件
    assert fm2.count_for(fp) == 2
    assert fm2.known_deadend(fp, threshold=2) is not None


def test_failurememory_empty_classes_fallback_unknown():
    fm = _mem()
    fp = fingerprint("run_powershell", {"command": "q"})
    fm.record(fp, [])                                     # 无分类 → 记为 unknown
    assert fm.count_for(fp, ErrorClass.UNKNOWN) == 1


# ---- detect_repeated_failure：loop 接线（块E 行为：第二次撞死路→提示换思路）---------

class _Call:
    def __init__(self, cid, name, inp):
        self.id, self.name, self.input = cid, name, inp


def _detect():
    from agentcore.agent.loop import detect_repeated_failure
    return detect_repeated_failure


# 非瞬时失败文本（逻辑错误：测试断言失败）
_FAIL = "==== 1 failed, 2 passed ====\nAssertionError: boom"
# 瞬时失败文本（网络抖动）
_TRANSIENT = "[exit code] 1\n[stderr]\ncurl: (7) Connection refused"
# 成功文本
_OK = "[exit code] 0\n[stdout]\nfine"


def test_detect_second_deadend_nudges():
    detect = _detect()
    world, fm, nudged = WorldState(), _mem(), set()
    call = _Call("1", "run_powershell", {"command": "pytest bad"})
    out = {"1": _FAIL}
    # 第一次：记录但未达阈值 → 无提示
    assert detect([call], out, world, fm, nudged, threshold=2) is None
    assert world.failures_for(fingerprint("run_powershell", {"command": "pytest bad"})) == 1
    # 第二次同一条路：达阈值 → 提示换思路
    msg = detect([call], out, world, fm, nudged, threshold=2)
    assert msg is not None and "换一条思路" in msg


def test_detect_transient_not_a_deadend():
    detect = _detect()
    world, fm, nudged = WorldState(), _mem(), set()
    call = _Call("1", "run_powershell", {"command": "curl x"})
    out = {"1": _TRANSIENT}
    for _ in range(3):
        assert detect([call], out, world, fm, nudged, threshold=2) is None  # 瞬时永不算死路
    assert world.failures_for(fingerprint("run_powershell", {"command": "curl x"})) == 0


def test_detect_success_records_nothing():
    detect = _detect()
    world, fm, nudged = WorldState(), _mem(), set()
    call = _Call("1", "run_powershell", {"command": "echo hi"})
    assert detect([call], {"1": _OK}, world, fm, nudged, threshold=2) is None
    assert world.failures_for(fingerprint("run_powershell", {"command": "echo hi"})) == 0


def test_detect_nudges_once_per_fingerprint():
    detect = _detect()
    world, fm, nudged = WorldState(), _mem(), set()
    call = _Call("1", "run_powershell", {"command": "pytest bad"})
    out = {"1": _FAIL}
    detect([call], out, world, fm, nudged, threshold=2)   # 1
    assert detect([call], out, world, fm, nudged, threshold=2) is not None  # 2 → 提示
    assert detect([call], out, world, fm, nudged, threshold=2) is None      # 同指纹本轮不再重复提示


def test_detect_cross_session_deadend_nudges_first_time():
    # FailureMemory 里已有此路 2 次历史 → 本会话第一次撞即提示（跨会话避坑）
    detect = _detect()
    fm = _mem()
    fp = fingerprint("run_powershell", {"command": "pytest bad"})
    from agentcore.agent.taxonomy import ErrorClass
    fm.record(fp, [ErrorClass.LOGIC]); fm.record(fp, [ErrorClass.LOGIC])
    world, nudged = WorldState(), set()
    call = _Call("1", "run_powershell", {"command": "pytest bad"})
    msg = detect([call], {"1": _FAIL}, world, fm, nudged, threshold=2)
    assert msg is not None and "失败" in msg


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
