"""FR-9.4a 改动台账：snapshot/changes/diff/revert + 工具挂钩（无网络）。

运行：python tests/test_changes.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.changes import ChangeLedger  # noqa: E402
from agentcore.tools import build_registry  # noqa: E402


# ---- 台账核心 ---------------------------------------------------------------

def test_added_modified_and_diff(tmp: Path):
    led = ChangeLedger(tmp)
    # 修改已有文件
    f = tmp / "a.txt"
    f.write_text("line1\nline2\n", encoding="utf-8")
    led.snapshot("a.txt")
    f.write_text("line1\nline2 changed\n", encoding="utf-8")
    # 新增文件
    led.snapshot("new.txt")
    (tmp / "new.txt").write_text("hello\n", encoding="utf-8")

    chg = {c["path"]: c["status"] for c in led.changes()}
    assert chg == {"a.txt": "modified", "new.txt": "added"}

    d = led.diff("a.txt")
    assert "-line2" in d and "+line2 changed" in d and "a/a.txt" in d
    assert led.diff("不存在.txt") is None


def test_first_snapshot_wins(tmp: Path):
    """同一文件多次改动：基线=第一次改动前，diff/回退相对最初状态。"""
    led = ChangeLedger(tmp)
    f = tmp / "a.txt"
    f.write_text("v1", encoding="utf-8")
    led.snapshot("a.txt")
    f.write_text("v2", encoding="utf-8")
    led.snapshot("a.txt")           # 第二次改前再 snapshot，不应覆盖基线
    f.write_text("v3", encoding="utf-8")
    assert led.revert("a.txt") is True
    assert f.read_text(encoding="utf-8") == "v1"   # 回到最初，不是 v2


def test_changed_back_not_listed(tmp: Path):
    """改了又改回原样：不算改动。"""
    led = ChangeLedger(tmp)
    f = tmp / "a.txt"
    f.write_text("same", encoding="utf-8")
    led.snapshot("a.txt")
    f.write_text("other", encoding="utf-8")
    f.write_text("same", encoding="utf-8")
    assert led.changes() == []
    assert led.diff("a.txt") is None


def test_revert_added_deletes(tmp: Path):
    led = ChangeLedger(tmp)
    led.snapshot("sub/new.txt")
    p = tmp / "sub" / "new.txt"
    p.parent.mkdir()
    p.write_text("x", encoding="utf-8")
    assert led.changes()[0]["status"] == "added"
    assert led.revert("sub/new.txt") is True
    assert not p.exists()            # 新增文件回退=删除
    assert led.changes() == []


def test_revert_all(tmp: Path):
    led = ChangeLedger(tmp)
    a, b = tmp / "a.txt", tmp / "b.txt"
    a.write_text("A", encoding="utf-8")
    led.snapshot("a.txt"); a.write_text("A2", encoding="utf-8")
    led.snapshot("b.txt"); b.write_text("B", encoding="utf-8")
    assert led.revert_all() == 2
    assert a.read_text(encoding="utf-8") == "A" and not b.exists()
    assert led.changes() == []


def test_deleted_status(tmp: Path):
    """基线存在、当前被（外力）删了：状态 deleted，回退能恢复。"""
    led = ChangeLedger(tmp)
    f = tmp / "a.txt"
    f.write_text("keep", encoding="utf-8")
    led.snapshot("a.txt")
    f.unlink()
    assert led.changes() == [{"path": "a.txt", "status": "deleted"}]
    assert led.revert("a.txt") is True
    assert f.read_text(encoding="utf-8") == "keep"


# ---- 工具挂钩：经 registry 的 write/edit 自动入账 ---------------------------

def test_tools_feed_ledger(tmp: Path):
    led = ChangeLedger(tmp)
    reg = build_registry(tmp, change_tracker=led.snapshot)
    # write 新文件
    reg.get("write_file").run({"path": "x.py", "content": "def f(): pass\n"})
    # 先建好再 edit
    reg.get("write_file").run({"path": "y.py", "content": "old = 1\n"})
    reg.get("edit_file").run({"path": "y.py", "old_string": "old = 1", "new_string": "old = 2"})
    chg = {c["path"]: c["status"] for c in led.changes()}
    assert chg["x.py"] == "added"
    assert chg["y.py"] == "added"     # y.py 也是本对话新建的 → 基线=不存在
    # 对"会话前就存在"的文件：edit 应记 modified
    (tmp / "z.py").write_text("a = 1\n", encoding="utf-8")
    reg.get("edit_file").run({"path": "z.py", "old_string": "a = 1", "new_string": "a = 9"})
    assert {c["path"]: c["status"] for c in led.changes()}["z.py"] == "modified"


def test_no_tracker_no_crash(tmp: Path):
    """不传 change_tracker（如旧用法）也照常工作。"""
    reg = build_registry(tmp)
    reg.get("write_file").run({"path": "p.txt", "content": "ok"})
    assert (tmp / "p.txt").read_text(encoding="utf-8") == "ok"


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
