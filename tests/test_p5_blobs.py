"""P5.1 图像外置 blob 存储自测（无 GUI、无网络）。

运行：python tests/test_p5_blobs.py
"""
from __future__ import annotations

import base64
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.store import blobs  # noqa: E402
from agentcore.store.db import Store  # noqa: E402

_RAW = b"\x89PNG-fake-image-bytes-\x00\x01\x02"
_B64 = base64.b64encode(_RAW).decode()


def _img(b64=_B64):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def test_dehydrate_rehydrate_roundtrip(tmp: Path):
    bd = tmp / "blobs"; bd.mkdir()
    deh = blobs.dehydrate(_img(), bd)
    assert deh["source"]["type"] == blobs.BLOB_SOURCE
    assert "data" not in deh["source"] and deh["source"]["ref"].endswith(".png")
    assert len(list(bd.iterdir())) == 1  # 图片字节落盘
    # 还原后与原 base64 一致
    reh = blobs.rehydrate(deh, bd)
    assert reh["source"]["type"] == "base64" and reh["source"]["data"] == _B64


def test_dedup_same_image(tmp: Path):
    bd = tmp / "blobs"; bd.mkdir()
    blobs.dehydrate(_img(), bd)
    blobs.dehydrate(_img(), bd)  # 同图第二次
    assert len(list(bd.iterdir())) == 1  # sha256 去重，只一份


def test_sibling_blocks_only_image_externalized(tmp: Path):
    """截屏回合：tool_result(文本) + image 并列，只有 image 被外置。"""
    bd = tmp / "blobs"; bd.mkdir()
    content = [
        {"type": "tool_result", "tool_use_id": "c1", "content": "已截屏"},
        _img(),
        {"type": "text", "text": "看看屏幕"},
    ]
    deh = blobs.dehydrate(content, bd)
    assert deh[0] == content[0]  # tool_result 原样
    assert deh[1]["source"]["type"] == blobs.BLOB_SOURCE
    assert deh[2] == content[2]
    assert blobs.rehydrate(deh, bd)[1]["source"]["data"] == _B64


def test_store_roundtrip_no_base64_in_db(tmp: Path):
    db = tmp / "h.db"
    store = Store(db, externalize_images=True)
    sid = store.create_session("t", "m")
    store.add_message(sid, "user", [_img(), {"type": "text", "text": "hi"}])
    # 读出还原为 base64
    msgs = store.get_messages(sid)
    assert msgs[0]["content"][0]["source"]["data"] == _B64
    # DB 里不含原 base64，只含 blob 引用
    raw = sqlite3.connect(str(db)).execute("SELECT content FROM messages").fetchone()[0]
    assert _B64 not in raw and blobs.BLOB_SOURCE in raw
    store.close()


def test_gc_removes_orphans_keeps_shared(tmp: Path):
    db = tmp / "h.db"
    store = Store(db, externalize_images=True)
    s1 = store.create_session("a", "m")
    s2 = store.create_session("b", "m")
    store.add_message(s1, "user", [_img()])           # 共享同一张图
    store.add_message(s2, "user", [_img()])
    other = _img(base64.b64encode(b"another-img").decode())
    store.add_message(s1, "user", [other])            # s1 独有的另一张
    bd = tmp / "blobs"
    assert len(list(bd.iterdir())) == 2

    store.delete_session(s1)  # 删 s1 -> 独有图成孤儿被删；共享图仍被 s2 引用，保留
    names = {f.name for f in bd.iterdir()}
    assert len(names) == 1
    # 剩下的正是 s2 仍能还原的那张
    assert store.get_messages(s2)[0]["content"][0]["source"]["data"] == _B64
    store.close()


def test_missing_blob_graceful(tmp: Path):
    bd = tmp / "blobs"; bd.mkdir()
    ref_block = {"type": "image", "source": {"type": blobs.BLOB_SOURCE, "media_type": "image/png", "ref": "deadbeef.png"}}
    out = blobs.rehydrate(ref_block, bd)  # 文件不存在
    assert out["type"] == "text" and "缺失" in out["text"]


def test_externalize_off_keeps_inline(tmp: Path):
    db = tmp / "h.db"
    store = Store(db, externalize_images=False)
    sid = store.create_session("t", "m")
    store.add_message(sid, "user", [_img()])
    raw = sqlite3.connect(str(db)).execute("SELECT content FROM messages").fetchone()[0]
    assert _B64 in raw  # 关闭外置：base64 仍内联存
    assert not (tmp / "blobs").exists()
    store.close()


def test_backward_compat_plain_content(tmp: Path):
    """旧库的纯文本 / 无 blob 引用 content：dehydrate/rehydrate 原样不变。"""
    bd = tmp / "blobs"; bd.mkdir()
    plain = [{"type": "text", "text": "纯文本"}]
    assert blobs.dehydrate("字符串", bd) == "字符串"
    assert blobs.rehydrate(plain, bd) == plain


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d)) if "tmp" in inspect.signature(fn).parameters else fn()
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
