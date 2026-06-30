"""P4 纯逻辑自测：多模态附件归一 + provider 图片转换（无 GUI、无网络）。

运行：python tests/test_p4.py
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.multimodal import Limits, build_user_content  # noqa: E402
from agentcore.providers.base import Message  # noqa: E402
from agentcore.providers.openai_p import _messages_to_openai  # noqa: E402

# 1x1 PNG
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42m" "NkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def test_no_attachment_returns_str():
    out = build_user_content("你好", None)
    assert out == "你好" and isinstance(out, str)


def test_image_block():
    out = build_user_content("看图", [{"name": "a.png", "mime": "image/png", "data": PNG_B64}])
    assert isinstance(out, list)
    img = [b for b in out if b["type"] == "image"]
    assert len(img) == 1
    assert img[0]["source"]["media_type"] == "image/png"
    assert img[0]["source"]["data"] == PNG_B64
    # 文本块在最后
    assert out[-1]["type"] == "text" and "看图" in out[-1]["text"]


def test_image_too_large_rejected():
    big = base64.b64encode(b"x" * 2048).decode()
    out = build_user_content("q", [{"name": "big.png", "mime": "image/png", "data": big}],
                             Limits(max_image_bytes=1024))
    # 图片被拒，只剩说明 + 文本
    assert not any(b["type"] == "image" for b in out)
    assert any("上限" in b.get("text", "") for b in out)


def test_text_doc_wrapped():
    data = base64.b64encode("print('hi')".encode()).decode()
    out = build_user_content("解释", [{"name": "x.py", "mime": "text/x-python", "data": data}])
    doc = out[0]["text"]
    assert '<document name="x.py">' in doc and "print('hi')" in doc


def test_binary_rejected():
    data = base64.b64encode(b"\x00\x01\x02\xff\xfe").decode()
    out = build_user_content("q", [{"name": "blob.bin", "mime": "application/octet-stream", "data": data}])
    assert any("已忽略" in b.get("text", "") for b in out)


def test_pdf_extraction():
    pypdf = __import__("pypdf")
    import io
    # 用 pypdf 现造一个含文字的单页 PDF
    try:
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        # 空白页抽不出文本 -> 应给出"未抽取到文本"提示
        data = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # noqa: BLE001
        print(f"  skip test_pdf_extraction（pypdf 造 PDF 失败：{e}）")
        return
    out = build_user_content("总结", [{"name": "d.pdf", "mime": "application/pdf", "data": data}])
    # 空白 PDF：无文本块文档，但有说明
    assert any("未抽取到文本" in b.get("text", "") or "d.pdf" in b.get("text", "") for b in out)


def test_max_attachments():
    atts = [{"name": f"{i}.png", "mime": "image/png", "data": PNG_B64} for i in range(5)]
    out = build_user_content("q", atts, Limits(max_attachments=2))
    imgs = [b for b in out if b["type"] == "image"]
    assert len(imgs) == 2
    assert any("上限" in b.get("text", "") for b in out)


def test_openai_image_conversion():
    msg = Message("user", [
        {"type": "text", "text": "看这张图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}},
    ])
    converted = _messages_to_openai([msg])
    assert len(converted) == 1
    content = converted[0]["content"]
    assert isinstance(content, list)
    kinds = {p["type"] for p in content}
    assert kinds == {"text", "image_url"}
    url = [p for p in content if p["type"] == "image_url"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_openai_plain_text_stays_string():
    msg = Message("user", "纯文本")
    out = _messages_to_openai([msg])
    assert out[0]["content"] == "纯文本"


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
