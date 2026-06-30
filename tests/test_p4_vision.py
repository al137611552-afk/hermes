"""P4.1 视觉预处理回退自测（mock，不联网）。

运行：python tests/test_p4_vision.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.multimodal.vision import (  # noqa: E402
    VisionError,
    build_payload,
    parse_response,
    preprocess_vision,
)

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}


# ---- 纯函数：payload / parse ------------------------------------------
def test_build_payload():
    p = build_payload("QUJD", "image/png", "描述")
    assert p["prompt"] == "描述"
    assert p["image_url"] == "data:image/png;base64,QUJD"


def test_parse_response_ok():
    raw = '{"content": "一只猫", "base_resp": {"status_code": 0, "status_msg": "success"}}'.encode()
    assert parse_response(raw) == "一只猫"


def test_parse_response_biz_error():
    raw = b'{"content": "", "base_resp": {"status_code": 1004, "status_msg": "auth fail"}}'
    try:
        parse_response(raw)
    except VisionError:
        return
    raise AssertionError("业务错误码未抛 VisionError")


def test_parse_response_missing_content():
    try:
        parse_response(b'{"base_resp": {"status_code": 0}}')
    except VisionError:
        return
    raise AssertionError("缺 content 未抛错")


# ---- preprocess_vision -------------------------------------------------
def test_preprocess_replaces_image():
    calls = []

    def fake(b64, mt, prompt):
        calls.append((b64, mt, prompt))
        return "这是一张报错截图"

    content = [IMG, {"type": "text", "text": "这个错怎么解决"}]
    out = preprocess_vision(content, "这个错怎么解决", fake, "描述图片")
    # image 被换成文字描述块；原文本保留
    assert out[0]["type"] == "text" and "这是一张报错截图" in out[0]["text"]
    assert out[1]["text"] == "这个错怎么解决"
    # 用户文本拼进了 prompt
    assert "这个错怎么解决" in calls[0][2]
    assert calls[0][0] == "QUJD" and calls[0][1] == "image/png"


def test_preprocess_multiple_images():
    out = preprocess_vision([IMG, IMG], "", lambda b, m, p: "X", "P")
    assert all(b["type"] == "text" for b in out)
    assert len(out) == 2


def test_preprocess_failure_fallback():
    def boom(b64, mt, prompt):
        raise VisionError("超时")

    out = preprocess_vision([IMG], "q", boom, "P")
    assert out[0]["type"] == "text" and "图片识别失败" in out[0]["text"]


def test_preprocess_no_image_passthrough():
    content = [{"type": "text", "text": "纯文本"}]
    assert preprocess_vision(content, "q", lambda *a: "X", "P") is content


def test_preprocess_str_passthrough():
    assert preprocess_vision("你好", "你好", lambda *a: "X", "P") == "你好"


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
