"""图像外置 blob 存储（P5.1 / FR-5.1.1）。

问题：消息 content 里的图片以 base64 全量存进 DB，长会话（尤其 Agent 自动截图）会让
DB 急剧膨胀、拖慢会话加载。

方案：落库前把 image 块的 base64 抽出来写到 `blobs/<sha256>.<ext>`，DB 只存一个引用
（`source.type == "hermes_blob"`，带 `ref` 文件名）；读出时再 rehydrate 回 base64。
内核 / provider 始终只见完整 base64，存储层透明转换。

- 按内容 sha256 去重：同一张图只存一份。
- 向后兼容：旧库里的 base64 块没有 blob 引用，rehydrate 原样返回，不受影响。
- blob 丢失时优雅降级为文本占位，不抛错。
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

# DB 中外置图片的 source 标记类型（内部用，绝不发给 provider——读出时已 rehydrate 回 base64）。
BLOB_SOURCE = "hermes_blob"

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def _is_base64_image(node) -> bool:
    return (
        isinstance(node, dict)
        and node.get("type") == "image"
        and isinstance(node.get("source"), dict)
        and node["source"].get("type") == "base64"
    )


def _is_blob_image(node) -> bool:
    return (
        isinstance(node, dict)
        and node.get("type") == "image"
        and isinstance(node.get("source"), dict)
        and node["source"].get("type") == BLOB_SOURCE
    )


def _store_blob(block: dict, blobs_dir: Path) -> dict:
    src = block["source"]
    raw = base64.b64decode(src.get("data", ""), validate=False)
    sha = hashlib.sha256(raw).hexdigest()
    mt = src.get("media_type", "image/png")
    ref = f"{sha}.{_EXT.get(mt, 'png')}"
    path = blobs_dir / ref
    if not path.exists():
        path.write_bytes(raw)
    return {"type": "image", "source": {"type": BLOB_SOURCE, "media_type": mt, "ref": ref}}


def _load_blob(block: dict, blobs_dir: Path) -> dict:
    src = block["source"]
    path = blobs_dir / src.get("ref", "")
    if not path.is_file():
        return {"type": "text", "text": "[图片缺失]"}
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": src.get("media_type", "image/png"), "data": b64},
    }


def dehydrate(content, blobs_dir: Path):
    """落库前：把 base64 image 块换成 blob 引用，并把图片字节写盘。"""
    if _is_base64_image(content):
        return _store_blob(content, blobs_dir)
    if isinstance(content, list):
        return [dehydrate(b, blobs_dir) for b in content]
    if isinstance(content, dict):
        return {k: dehydrate(v, blobs_dir) for k, v in content.items()}
    return content


def rehydrate(content, blobs_dir: Path):
    """读出后：把 blob 引用还原成 base64 image 块。"""
    if _is_blob_image(content):
        return _load_blob(content, blobs_dir)
    if isinstance(content, list):
        return [rehydrate(b, blobs_dir) for b in content]
    if isinstance(content, dict):
        return {k: rehydrate(v, blobs_dir) for k, v in content.items()}
    return content


def collect_refs(content, out: set[str]) -> None:
    """收集 content 中引用到的所有 blob 文件名（用于 GC）。"""
    if _is_blob_image(content):
        ref = content["source"].get("ref")
        if ref:
            out.add(ref)
    elif isinstance(content, list):
        for b in content:
            collect_refs(b, out)
    elif isinstance(content, dict):
        for v in content.values():
            collect_refs(v, out)


def gc(blobs_dir: Path, referenced: set[str]) -> int:
    """删除 blobs_dir 下未被引用的孤儿文件，返回删除数。"""
    if not blobs_dir.is_dir():
        return 0
    removed = 0
    for f in blobs_dir.iterdir():
        if f.is_file() and f.name not in referenced:
            f.unlink()
            removed += 1
    return removed
