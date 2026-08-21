"""贴进对话的图片落盘到工作区（纯逻辑 + 受控 IO）。

**为什么必须落盘**：Codex（以及任何以 CLI/子进程形态接进来的 agent）**只认文件路径**——
它的 MCP 工具 schema 里压根没有图片入参（2026-08-21 实测），CLI 那边是 `codex exec -i <FILE>`。
所以"用户贴图 → 子 agent 看图"这条路上，落盘是唯一通路。

运行：python tests/test_image_store.py
"""
from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.multimodal.store import (  # noqa: E402
    ATTACH_DIR, attachment_name, render_saved_note, save_images,
)

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


def test_name_is_shell_safe_and_never_collides():
    """文件名会被拼进命令行交给 agent，只留安全字符；**带时间戳而不是覆盖同名**——
    同一会话里贴第二张 screenshot.png 时，覆盖会让"上一张图"凭空失效。"""
    a = attachment_name("屏幕截图 2026-08-21.png", "image/png", "20260821-101500", 1)
    b = attachment_name("屏幕截图 2026-08-21.png", "image/png", "20260821-101500", 2)
    assert a != b and a.startswith("20260821-101500-1-") and a.endswith(".png")
    assert " " not in a
    # 没有扩展名/未知 mime 也要给出可用的名字
    assert attachment_name("", "", "T", 1).endswith(".png")
    assert attachment_name("shot.webp", "image/webp", "T", 1).endswith(".webp")


def test_only_images_are_saved():
    ws = tempfile.mkdtemp()
    got = save_images([{"name": "a.png", "mime": "image/png", "data": _PNG},
                       {"name": "note.txt", "mime": "text/plain", "data": _PNG},
                       {"name": "b.jpg", "mime": "", "data": _PNG}], ws, stamp="T")
    assert len(got) == 2 and all(p.startswith(ATTACH_DIR) for p in got), got
    assert all((Path(ws) / p).is_file() for p in got)


def test_broken_attachment_never_blocks_the_message():
    """图存不下来不该让整条消息发不出去。"""
    ws = tempfile.mkdtemp()
    assert save_images([{"name": "x.png", "mime": "image/png", "data": "!!!不是base64"}],
                       ws, stamp="T") == []
    assert save_images(None, ws) == [] and save_images([{"a": 1}], ws) == []
    assert save_images([{"name": "a.png", "mime": "image/png", "data": _PNG}], "") == []


def test_note_tells_the_model_to_pass_the_path():
    """模型自己看得见图（视觉直传），但派给子 agent 时只能给路径——
    不点破的话它多半会**转述**图片内容，转述必然丢细节。"""
    note = render_saved_note([".hermes/attachments/T-1-a.png"])
    assert "T-1-a.png" in note and "路径" in note
    assert render_saved_note([]) == ""


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
