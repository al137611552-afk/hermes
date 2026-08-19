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


def test_failure_record_carries_decision_label():
    """失败记录要带「做法」标签：用了哪个工具 + 是不是提示过仍走同一条路。

    以前这个字段从来没传过，块G（Learning）的 evidence["decisions"] 恒为空 —— 等于
    "改进 Need→Decision" 的语料里没有 Decision。这条钉住它不再退化。
    """
    import tempfile
    from pathlib import Path
    from agentcore.agent.loop import detect_repeated_failure
    from agentcore.agent.world_state import FailureMemory, WorldState

    class C:
        def __init__(self, name, inp, cid):
            self.name, self.input, self.id = name, inp, cid

    # ignore_cleanup_errors：Windows 上 sqlite 连接还开着就删不掉 .db（WinError 32），
    # 而清理失败发生在断言全过之后，不该把测试判红（Linux 允许删已打开的文件，故只在 Windows 现形）。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        fm = FailureMemory(Path(td) / "f.db")
        world = WorldState()
        nudged = set()
        call = C("run_bash", {"command": "pytest -q"}, "t1")
        out = {"t1": "Traceback ... AssertionError: boom"}

        detect_repeated_failure([call], out, world, fm, nudged, threshold=2)
        rows = fm.rows()
        assert len(rows) == 1, rows
        assert rows[0]["decision"] == "run_bash", rows[0]      # 首次：只记工具
        assert rows[0]["error_class"], rows[0]                 # 分类照旧

        # 同一条路再失败一次：这次是"已知死路还再走"，标签要能看出来
        detect_repeated_failure([call], out, world, fm, nudged, threshold=2)
        labels = sorted(r["decision"] for r in fm.rows())
        assert labels == ["run_bash", "run_bash|after_nudge"], labels

        # 换个工具做同一件事：标签跟着变，聚合时才分得清"换没换路"
        call2 = C("run_powershell", {"command": "pytest -q"}, "t2")
        detect_repeated_failure([call2], {"t2": "Traceback ... AssertionError: boom"},
                                world, fm, nudged, threshold=2)
        assert any(r["decision"] == "run_powershell" for r in fm.rows()), fm.rows()
        print("✓ 失败记录带做法标签（工具名 + after_nudge），块G 的 evidence 不再是空的")


# ---- ADR 0027 V0：指纹路径归一 + 语料来源 -----------------------------------

def test_fingerprint_normalizes_separators():
    """同一个文件的两种写法（反斜杠/正斜杠）必须同指纹——Windows 与 POSIX 混写很常见。"""
    a = fingerprint("read_file", {"path": "src" + chr(92) + "a.py"})
    b = fingerprint("read_file", {"path": "src/a.py"})
    assert a == b


def test_fingerprint_folds_workspace_in_path_param():
    """同一相对路径在两个不同工作区下 → 同指纹（传了 workspace 才归一）。"""
    with tempfile.TemporaryDirectory() as w1, tempfile.TemporaryDirectory() as w2:
        a = fingerprint("read_file", {"path": str(Path(w1) / "calc.py")}, w1)
        b = fingerprint("read_file", {"path": str(Path(w2) / "calc.py")}, w2)
        assert a == b
        # 工作区内不同文件仍要分得开（别归一过头把所有路合成一条）
        c = fingerprint("read_file", {"path": str(Path(w1) / "other.py")}, w1)
        assert a != c


def test_fingerprint_folds_workspace_inside_command():
    """command 里嵌着的绝对路径也要折——路径在命令行中间，切不出来只能替换。"""
    with tempfile.TemporaryDirectory() as w1, tempfile.TemporaryDirectory() as w2:
        a = fingerprint("run_bash", {"command": f"pytest {w1}/test_calc.py -q"}, w1)
        b = fingerprint("run_bash", {"command": f"pytest {w2}/test_calc.py -q"}, w2)
        assert a == b


def test_fingerprint_without_workspace_keeps_old_behavior():
    """workspace=None 时不做路径折叠——存量调用方与旧库口径不变。"""
    with tempfile.TemporaryDirectory() as w1, tempfile.TemporaryDirectory() as w2:
        a = fingerprint("read_file", {"path": str(Path(w1) / "calc.py")})
        b = fingerprint("read_file", {"path": str(Path(w2) / "calc.py")})
        assert a != b


def test_fingerprint_prefers_longest_workspace_prefix():
    """嵌套工作区：长前缀先替，否则留下半截相对路径、指纹又分裂。"""
    with tempfile.TemporaryDirectory() as outer:
        inner = Path(outer) / "sub"
        inner.mkdir()
        a = fingerprint("read_file", {"path": str(inner / "a.py")}, str(inner))
        b = fingerprint("read_file", {"path": "a.py"}, str(inner))
        assert a == b, "工作区内的绝对路径应折成与相对写法一致"


def test_fingerprint_folds_both_raw_and_resolved_workspace_forms():
    """工作区有**两种字面形态**时（软链名 vs 真实名），两种都要能折上。

    这正是 Windows 8.3 短名（`RUNNER~1` vs 长名）与 macOS `/var` vs `/private/var` 依赖的同一条机制：
    实际出现在工具入参里的往往只是其中一种，只认一种就照样分裂成两个指纹。
    用软链在 POSIX 上把这条路径真正压出来（Windows 建软链要提权，跳过）。
    """
    import os
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as base:
        real = Path(base) / "real_ws"
        real.mkdir()
        link = Path(base) / "link_ws"
        link.symlink_to(real, target_is_directory=True)

        # 工作区用软链名给出：入参里无论出现软链名还是真实名，都应折成同一个相对形
        via_link = fingerprint("read_file", {"path": str(link / "calc.py")}, str(link))
        via_real = fingerprint("read_file", {"path": str(real / "calc.py")}, str(link))
        rel = fingerprint("read_file", {"path": "calc.py"}, str(link))
        assert via_link == rel, "软链名形态没折上"
        assert via_real == rel, "resolve() 后的真实名形态没折上（8.3 短名同源）"
    print("✓ 工作区双形态（软链/真实名）都能折——8.3 短名走的是同一条机制")


def test_same_failure_across_runs_aggregates_to_one_row():
    """**ADR 0027 V0 验收判据**：两次不同临时工作区跑同一个失败 →
    `rows()` 是**一行 count=2**，不是两行 count=1。

    这条不过，攒再多语料块G 的 propose() 双门也是失真的（背景见 ADR 0027）。
    """
    with tempfile.TemporaryDirectory() as db_dir:
        fm = FailureMemory(Path(db_dir) / "f.db")
        try:
            for _ in range(2):
                with tempfile.TemporaryDirectory() as ws:   # 每跑一个新工作区，模拟评测
                    fp = fingerprint("run_bash", {"command": f"pytest {ws}/test_calc.py"}, ws)
                    fm.record(fp, [ErrorClass.LOGIC], decision="run_bash")
            rows = fm.rows()
            assert len(rows) == 1, rows
            assert rows[0]["count"] == 2, rows
        finally:
            fm.close()
    print("✓ 跨跑同一失败聚合成一行（ADR 0027 V0 验收）")


def test_without_normalization_temp_paths_would_split():
    """反证（钉住这条修的到底是什么）：不传 workspace 时，同一失败在两个临时工作区
    分裂成两行 count=1——`paths` 虚高、每指纹 total 恒 1，双门两个方向同时失真。"""
    with tempfile.TemporaryDirectory() as db_dir:
        fm = FailureMemory(Path(db_dir) / "f.db")
        try:
            for _ in range(2):
                with tempfile.TemporaryDirectory() as ws:
                    fp = fingerprint("run_bash", {"command": f"pytest {ws}/test_calc.py"})
                    fm.record(fp, [ErrorClass.LOGIC], decision="run_bash")
            assert len(fm.rows()) == 2
        finally:
            fm.close()


def test_failure_memory_tags_source():
    """来源标记：评测库标 eval，默认库标 real。隔离靠分库，这一列供导出后合并分析区分。"""
    with tempfile.TemporaryDirectory() as d:
        fm = FailureMemory(Path(d) / "eval.db", source="eval")
        try:
            fm.record("fp1", [ErrorClass.LOGIC], decision="run_bash")
            assert fm.rows()[0]["source"] == "eval", fm.rows()
        finally:
            fm.close()
        fm2 = FailureMemory(Path(d) / "real.db")
        try:
            fm2.record("fp1", [ErrorClass.LOGIC])
            assert fm2.rows()[0]["source"] == "real", fm2.rows()
        finally:
            fm2.close()


def test_failure_memory_migrates_legacy_db_without_dropping_rows():
    """旧库（无 source 列）打开后自动补列，**旧数据一行不丢**、按 real 落位。"""
    import sqlite3 as _sq
    with tempfile.TemporaryDirectory() as d:
        dbp = Path(d) / "legacy.db"
        conn = _sq.connect(str(dbp))
        conn.executescript("""
            CREATE TABLE failures (
                fingerprint TEXT NOT NULL, error_class TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL DEFAULT 1,
                first_at REAL NOT NULL, last_at REAL NOT NULL,
                PRIMARY KEY (fingerprint, error_class, decision));
            INSERT INTO failures VALUES('old','logic','run_bash','boom',3,1.0,2.0);
        """)
        conn.commit()
        conn.close()

        fm = FailureMemory(dbp)
        try:
            rows = fm.rows()
            assert len(rows) == 1, rows
            assert rows[0]["count"] == 3 and rows[0]["source"] == "real", rows
            # 迁移后照常累加，不因补列而失效
            fm.record("old", [ErrorClass.LOGIC], decision="run_bash")
            assert fm.rows()[0]["count"] == 4, fm.rows()
        finally:
            fm.close()
    print("✓ 旧库补 source 列不丢数据、可继续累加")



def test_quality_gap_tools_do_not_enter_failure_memory():
    """**"返回了但不达标" ≠ "这条路走不通"**（ADR 0027 决策 11，块 V4a）。

    `web_search` 命中预算 blocker 时曾被记成失败语料。方向是反的——它真正的硬失败
    （超时/无结果）反倒不产 issues；记进来的必然是质量差距，而质量差距已有块H2 专门处置
    （催重搜/换源阶梯）。后果：同一个 query 被当死路累计、与 research_hint 重复插话，
    且 taxonomy 没有"质量不达标"这一类，全落进 unknown（块 V4 收割：6 条路里 5 条是它）。
    """
    from agentcore.agent.loop import _QUALITY_ONLY_TOOLS, detect_repeated_failure

    class C:
        def __init__(self, name, params, cid):
            self.name, self.input, self.id = name, params, cid

    out = ("[搜索结果·bing] 机械键盘 500元以内\n"
           "1. A 键盘\n   http://a\n   ¥899\n"
           "2. B 键盘\n   http://b\n   ¥1299")
    params = {"query": "机械键盘 500元以内"}
    assert "web_search" in _QUALITY_ONLY_TOOLS

    ws = WorldState()
    nudge = detect_repeated_failure([C("web_search", params, "c1")], {"c1": out},
                                    ws, None, set(), threshold=1)
    assert nudge is None, nudge
    assert ws.failures_for(fingerprint("web_search", params)) == 0, "质量差距不该进失败记忆"

    # 别修过头：同样一段"失败"文本走执行类工具，照旧记
    ws2 = WorldState()
    detect_repeated_failure([C("run_bash", {"command": "pytest"}, "c2")],
                            {"c2": "[exit code] 1\n1 failed, 2 passed in 0.3s"},
                            ws2, None, set(), threshold=1)
    assert ws2.failures_for(fingerprint("run_bash", {"command": "pytest"})) == 1, \
        "执行类的失败仍必须记"

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
