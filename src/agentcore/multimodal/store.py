"""把对话里贴进来的图片**落到工作区**，并告诉模型路径（纯逻辑 + 受控 IO 分离）。

**为什么需要**：Codex（以及任何以 CLI/子进程形态接进来的 agent）**只认文件路径**——
它的 MCP 工具 schema 里压根没有图片入参（2026-08-21 实测：只有 prompt/cwd/sandbox 等），
CLI 那边是 `codex exec -i <FILE>`。所以"用户贴图 → agent 看图"这条路上，
**落盘是唯一通路**，不是偷懒。

落在工作区而不是临时目录：agent 的活动范围就是工作区（`clamp_cwd`），
放到 /tmp 它够不着；放工作区里它 `read_file`/`-i` 都能用。
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path

ATTACH_DIR = ".hermes/attachments"
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
_SAFE = re.compile(r"[^\w.-]+", re.UNICODE)


def attachment_name(original: str, mime: str, stamp: str, index: int) -> str:
    """图片落盘用的文件名（纯函数）。

    **带时间戳而不是覆盖同名**：同一个会话里贴第二张 `screenshot.png` 时，
    覆盖掉第一张会让"上一张图"在对话里凭空失效。
    文件名里只留安全字符——它会被拼进 shell 命令行交给 agent。
    """
    ext = _EXT.get((mime or "").lower(), "")
    base = (original or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not ext:
        for k, v in _EXT.items():
            if base.lower().endswith(v):
                ext = v
                break
    ext = ext or ".png"
    stem = _SAFE.sub("_", base[: -len(ext)] if base.lower().endswith(ext) else base).strip("_")
    stem = stem or "image"
    return f"{stamp}-{index}-{stem}{ext}"


def render_saved_note(paths: list) -> str:
    """告诉模型"图存哪了"（纯函数）。没存下东西就返回空串。

    **写清楚"给 agent 要用路径"**：模型看得见图（视觉模型直传），但它派给 Codex 时
    只能给路径——不点破的话，它多半会把图片当成自己看到的内容去转述，转述必然丢细节。
    """
    if not paths:
        return ""
    lines = "\n".join(f"  {p}" for p in paths)
    return ("\n[图片已存入工作区]\n" + lines +
            "\n（派给 codex 之类的子 agent 时**把路径给它**——它们只认文件、看不到对话里的图）")


def save_images(attachments, workspace, stamp: str = "") -> list:
    """把 image 类附件写进工作区的附件目录（受控 IO）。返回**工作区相对路径**列表。

    失败一律跳过：图存不下来不该让整条消息发不出去。
    """
    if not attachments or not workspace:
        return []
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    out = []
    dest = Path(workspace) / ATTACH_DIR
    for i, att in enumerate((a for a in attachments if isinstance(a, dict)), 1):
        mime = (att.get("mime") or "").lower()
        name = att.get("name") or ""
        if not (mime.startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg",
                                                                    ".gif", ".webp"))):
            continue
        try:
            raw = base64.b64decode(att.get("data") or "", validate=False)
            if not raw:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            fname = attachment_name(name, mime, stamp, i)
            (dest / fname).write_bytes(raw)
            out.append(f"{ATTACH_DIR}/{fname}")
        except Exception:  # noqa: BLE001 — 存不下来就跳过，别把消息卡住
            continue
    return out
