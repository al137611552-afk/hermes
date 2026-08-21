"""多模态输入归一：附件 -> 统一消息内容块；视觉预处理回退。"""
from __future__ import annotations

from .ingest import Limits, build_user_content
from .store import render_saved_note, save_images
from .vision import VisionError, describe_image, preprocess_vision

__all__ = [
    "render_saved_note", "save_images",
    "Limits",
    "build_user_content",
    "VisionError",
    "describe_image",
    "preprocess_vision",
]
