"""把前端传来的附件归一为统一消息内容块（content blocks）。

边界归一在此模块完成（见 CONVENTIONS §6「多模态边界归一」）：
图片 -> image block；PDF/文本/代码 -> 文本块。内核与 provider 只见统一 blocks。

内部统一表示沿用 Anthropic 风格 content blocks：
- 图片：{"type":"image","source":{"type":"base64","media_type","data"}}
- 文档：{"type":"text","text":"<document name=...>…</document>"}
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

# 受支持的图片类型（Anthropic / OpenAI 视觉通用）
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_DOC_CHARS = 100_000
DEFAULT_MAX_ATTACHMENTS = 10


@dataclass
class Limits:
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    max_doc_chars: int = DEFAULT_MAX_DOC_CHARS
    max_attachments: int = DEFAULT_MAX_ATTACHMENTS


def build_user_content(text: str, attachments=None, limits: Limits | None = None):
    """构造一条 user 消息的 content。

    attachments: list[{"name","mime","data"(base64)}] 或 None。
    返回 str（无附件，保持 P1 纯文本路径）或 list[dict]（含附件的 blocks）。
    无法处理的附件转成说明性文本块，不中断发送。
    """
    lim = limits or Limits()
    if not attachments:
        return text

    blocks: list[dict] = []
    notes: list[str] = []

    for att in attachments[: lim.max_attachments]:
        name = att.get("name", "未命名")
        mime = (att.get("mime") or "").lower()
        raw_b64 = att.get("data", "")
        try:
            raw = base64.b64decode(raw_b64, validate=False)
        except Exception:  # noqa: BLE001
            notes.append(f"[附件 {name} 解码失败，已忽略]")
            continue

        if mime in _IMAGE_MIMES or name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            block, note = _image_block(name, mime, raw, raw_b64, lim)
        elif mime == "application/pdf" or name.lower().endswith(".pdf"):
            block, note = _pdf_block(name, raw, lim)
        else:
            block, note = _text_block(name, raw, lim)

        if block:
            blocks.append(block)
        if note:
            notes.append(note)

    if len(attachments) > lim.max_attachments:
        notes.append(f"[附件超过 {lim.max_attachments} 个上限，仅处理前 {lim.max_attachments} 个]")

    # 文本放最后，便于模型先看材料再看问题
    leading = "\n".join(notes)
    text_payload = (leading + ("\n\n" if leading and text else "") + (text or "")).strip()
    if text_payload:
        blocks.append({"type": "text", "text": text_payload})

    return blocks if blocks else (text or "")


# ---- 各类附件 -> block --------------------------------------------------
def _image_block(name, mime, raw, raw_b64, lim: Limits):
    if mime not in _IMAGE_MIMES:
        # 按扩展名推断 media_type
        ext = name.lower().rsplit(".", 1)[-1]
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
    if len(raw) > lim.max_image_bytes:
        mb = lim.max_image_bytes / 1024 / 1024
        return None, f"[图片 {name} 超过 {mb:.0f}MB 上限，已忽略]"
    return (
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": raw_b64}},
        None,
    )


def _pdf_block(name, raw, lim: Limits):
    try:
        import io

        from pypdf import PdfReader
    except ImportError:
        return None, f"[未安装 pypdf，无法解析 {name}]"
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:  # noqa: BLE001
        return None, f"[PDF {name} 解析失败：{type(e).__name__}]"
    body = "\n".join(pages).strip()
    if not body:
        return None, f"[PDF {name} 未抽取到文本（可能是扫描件/图片型 PDF）]"
    body = body[: lim.max_doc_chars]
    return {"type": "text", "text": f'<document name="{name}">\n{body}\n</document>'}, None


def _text_block(name, raw, lim: Limits):
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"[附件 {name} 不是受支持的图片/文本/PDF，已忽略]"
    body = body[: lim.max_doc_chars]
    return {"type": "text", "text": f'<document name="{name}">\n{body}\n</document>'}, None
