"""FR-11.6 检查点：capture/restore 纯逻辑 + 工具 + Api 往返（无网络）。

运行：python tests/test_checkpoint.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore import checkpoints as ck  # noqa: E402
from agentcore.store import Store  # noqa: E402


def test_capture_and_restore_roundtrip(tmp: Path):
    (tmp / "a.txt").write_text("v1", encoding="utf-8")
    snap = ck.capture_files(tmp, ["a.txt", "new.txt"])
    assert snap == {"a.txt": "v1", "new.txt": None}
    (tmp / "a.txt").write_text("v2", encoding="utf-8")
    (tmp / "new.txt").write_text("later", encoding="utf-8")
    n = ck.restore_files(tmp, snap)
    assert n == 2
    assert (tmp / "a.txt").read_text(encoding="utf-8") == "v1"   # 改回
    assert not (tmp / "new.txt").exists()                        # 新增→删除
    # 已是快照态：再回退不重复改
    assert ck.restore_files(tmp, snap) == 0


def test_store_checkpoint_roundtrip_and_cascade(tmp: Path):
    st = Store(tmp / "h.db")
    sid = st.create_session("t", "m")
    payload = ck.make_payload({"a.txt": "v1"}, [{"content": "x", "status": "completed"}], "笔记")
    cid = st.add_checkpoint(sid, "里程碑", payload)
    assert st.list_checkpoints(sid)[0]["label"] == "里程碑"
    got = st.get_checkpoint(cid)
    assert got["session_id"] == sid and got["payload"]["notes"] == "笔记"
    st.delete_session(sid)
    assert st.list_checkpoints(sid) == [] and st.get_checkpoint(cid) is None
    st.close()


def test_prune_checkpoints_keeps_recent(tmp: Path):
    """P12 自动打点会累积：prune 只留最近 keep 个。"""
    st = Store(tmp / "h.db")
    sid = st.create_session("t", "m")
    ids = [st.add_checkpoint(sid, f"#{i}", ck.make_payload({}, [], "")) for i in range(8)]
    assert st.prune_checkpoints(sid, keep=3) == 5
    kept = [c["id"] for c in st.list_checkpoints(sid)]
    assert kept == ids[-3:][::-1]                 # 只剩最近 3 个（DESC）
    st.close()


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
