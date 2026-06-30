"""截屏工具（P5）：让 Agent 主动截取用户屏幕。

与「人工 Win+Shift+S 粘贴」不同，本工具进入 Agent 工具循环，模型可在需要时主动
调用（如「我看看你屏幕上的报错」），无需用户手动截图。

平台相关代码隔离于此：用 Pillow ImageGrab 截全屏。Windows / macOS 原生支持；
Linux 需 X 环境，开发机无显示时优雅抛 ToolError（回灌模型，不崩）。

隐私敏感（会看到用户整个屏幕），dangerous=True，执行前过权限 gate 由用户授权。
截图作为 image 块随 tool_result 注入（详见 ToolOutput / ADR-0010），需视觉模型
（如 ark-kimi）才能理解内容。
"""
from __future__ import annotations

import base64
import io

from .base import Tool, ToolError, ToolOutput

# 截图长边上限，超出则等比缩小，控制 token 与传输体积（Anthropic 图像长边量级）。
_MAX_EDGE = 1568


class ScreenshotTool(Tool):
    name = "take_screenshot"
    description = (
        "截取用户当前屏幕并返回截图。当需要查看用户屏幕上的界面、报错信息、设计稿等"
        "可视内容时调用。需用户授权后才执行；需要支持视觉的模型才能理解截图内容。"
    )
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    dangerous = True

    def run(self, params: dict) -> ToolOutput:
        try:
            from PIL import ImageGrab
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"截图功能需要 Pillow，但导入失败：{e}")

        try:
            img = ImageGrab.grab()
        except Exception as e:  # noqa: BLE001 — 无显示/无权限等
            raise ToolError(
                f"截屏失败（当前环境可能无图形界面或未授予屏幕录制权限）：{type(e).__name__}: {e}"
            )

        img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, _MAX_EDGE / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

        buf = io.BytesIO()
        img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }
        return ToolOutput(
            text=f"已截取屏幕（原始分辨率 {w}×{h}），截图已附在本条消息中。",
            blocks=[block],
        )
