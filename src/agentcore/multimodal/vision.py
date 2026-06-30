"""视觉预处理回退：用 MiniMax VL 端点把图片转成文字描述。

当主模型不支持视觉（如 MiniMax-M2.7）时，agent 先调这里把 image block 转成
文字描述，再把描述当文本喂给主模型——复刻 OpenClaw minimax 插件的思路。
支持视觉的模型（Claude/gpt-4o）不走这里，直接发原图。

端点契约（用户从 OpenClaw 源码提供）：
  POST <endpoint>  body {"prompt","image_url":"data:<mime>;base64,<b64>"}
  -> {"content": "...", "base_resp": {"status_code": 0}}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable


class VisionError(Exception):
    """视觉端点调用失败（网络/鉴权/业务码非 0）。"""


def build_payload(image_b64: str, media_type: str, prompt: str) -> dict:
    """构造 VL 请求体（纯函数，便于测试）。"""
    return {
        "prompt": prompt,
        "image_url": f"data:{media_type};base64,{image_b64}",
    }


def parse_response(raw: bytes) -> str:
    """从 VL 响应中取出描述文本（纯函数，便于测试）。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise VisionError(f"VL 响应解析失败：{e}")
    base = data.get("base_resp") or {}
    if base.get("status_code", 0) not in (0, None):
        raise VisionError(f"VL 业务错误：{base.get('status_code')} {base.get('status_msg', '')}")
    content = data.get("content")
    if not content:
        raise VisionError("VL 响应缺少 content")
    return content


def describe_image(
    image_b64: str,
    media_type: str,
    prompt: str,
    *,
    api_key: str,
    endpoint: str,
    timeout: int = 60,
) -> str:
    """调 MiniMax VL 端点，返回图片的文字描述。"""
    payload = build_payload(image_b64, media_type, prompt)
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "MM-API-Source": "OpenClaw",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_response(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise VisionError(f"VL HTTP {e.code}：{body}")
    except urllib.error.URLError as e:
        raise VisionError(f"VL 连接失败：{e.reason}")


def preprocess_vision(
    content,
    user_text: str,
    describe_fn: Callable[[str, str, str], str],
    base_prompt: str,
):
    """把 content blocks 里的 image 块替换成文字描述块。

    describe_fn(image_b64, media_type, prompt) -> str：注入的描述函数，便于测试与解耦。
    VL 的 prompt = base_prompt + 用户本轮文本（让描述更贴合用户意图）。
    单张图失败转成 [图片识别失败：…] 文本，不中断整轮。纯文本/无图原样返回。
    """
    if not isinstance(content, list):
        return content
    if not any(b.get("type") == "image" for b in content):
        return content

    prompt = base_prompt
    if user_text and user_text.strip():
        prompt = f"{base_prompt}\n\n用户的问题：{user_text.strip()}"

    out = []
    for b in content:
        if b.get("type") != "image":
            out.append(b)
            continue
        src = b.get("source", {})
        try:
            desc = describe_fn(src.get("data", ""), src.get("media_type", "image/png"), prompt)
            out.append({"type": "text", "text": f"[图片描述：{desc}]"})
        except Exception as e:  # noqa: BLE001 — 失败也要让对话继续
            out.append({"type": "text", "text": f"[图片识别失败：{type(e).__name__}: {e}]"})
    return out
