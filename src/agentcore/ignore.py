"""轻量 .gitignore 支持：在硬编码 _SKIP_DIRS 之外，额外尊重项目根的 .gitignore，
让文件树 / 检索在大项目里不被项目自定义的生成物、缓存、日志等淹没。

不求完整 gitignore 语义（不处理否定 `!`、嵌套 .gitignore、`**` 的全部细节），
覆盖最常见的 `name`、`*.ext`、`dir/`、`a/b` 这几类模式，够把噪音滤掉。
纯逻辑、便于单测。
"""
from __future__ import annotations

import fnmatch
from pathlib import Path


def read_gitignore_patterns(root) -> list[str]:
    """读工作区根 .gitignore，返回去掉注释/空行/否定行后的模式列表。"""
    out: list[str] = []
    gi = Path(root) / ".gitignore"
    if not gi.is_file():
        return out
    try:
        # utf-8-sig：自动去掉 Windows 记事本存的 BOM，否则第一行模式会带 ﻿ 匹配失效
        for line in gi.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("!"):
                out.append(s.rstrip("/"))
    except Exception:  # noqa: BLE001 — 读不了就当没有
        pass
    return out


def make_gitignore_matcher(root):
    """返回 `matches(rel, name) -> bool`：该相对路径 / 末段名是否被项目 .gitignore 命中。
    rel 用 posix 风格相对工作区路径；name 是末段文件/目录名。无 .gitignore 时恒 False。"""
    pats = read_gitignore_patterns(root)
    if not pats:
        return lambda rel, name: False

    def matches(rel: str, name: str) -> bool:
        parts = rel.split("/")
        for p in pats:
            if "/" in p:                       # 带路径：按相对路径匹配（含其子项）
                pp = p.lstrip("/")
                if fnmatch.fnmatch(rel, pp) or fnmatch.fnmatch(rel, pp + "/*") \
                        or rel == pp or rel.startswith(pp + "/"):
                    return True
            elif any(fnmatch.fnmatch(seg, p) for seg in parts):  # 纯名字/通配：任意路径段匹配
                return True                    # （这样平铺遍历下，被忽略目录的子项也命中）
        return False

    return matches
