"""工作区文件树与只读预览（右侧面板用）。纯函数，便于单测、不碰网络。

只读：构建工作区目录树、把路径安全地解析到工作区内、按类型读取文件内容
（文本/代码、图片、HTML、二进制）。路径越界一律拒绝。
"""
from __future__ import annotations

import base64
from pathlib import Path

# 不展开的目录（噪音/体积大/无预览意义）
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "data",
    "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    ".egg-info",
}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}
_TEXT_EXT = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".html", ".htm", ".css", ".csv", ".log", ".sh", ".ps1", ".bat", ".toml", ".ini",
    ".cfg", ".xml", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php",
    ".sql", ".vue", ".svelte", ".env", ".gitignore", ".dockerfile",
}
_HTML_EXT = {".html", ".htm"}
MAX_FILE_BYTES = 500_000     # 单文件预览上限（超过则截断显示）
MAX_TREE_ENTRIES = 2000      # 树节点上限，防超大工作区爆掉
CONV_MAX_CHARS = 20_000      # 项目规范文件注入 system 的字符上限


def resolve_within(root: Path, relpath: str) -> Path:
    """把相对路径解析到工作区内；越界则抛 ValueError。"""
    root = root.resolve()
    p = (root / (relpath or "")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"拒绝访问工作区外的路径：{relpath}")
    return p


def build_tree(root: Path, *, max_depth: int = 6) -> dict:
    """构建工作区目录树（目录在前、文件在后；跳过 _SKIP_DIRS、隐藏目录、.gitignore 命中项）。"""
    from .ignore import make_gitignore_matcher
    root = root.resolve()
    gi = make_gitignore_matcher(root)   # 额外尊重项目 .gitignore，大项目文件树不被生成物淹没
    count = [0]

    def walk(d: Path, depth: int) -> list[dict]:
        out: list[dict] = []
        try:
            items = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return out
        for it in items:
            if count[0] >= MAX_TREE_ENTRIES:
                break
            if it.is_dir() and (it.name in _SKIP_DIRS or it.name.startswith(".")):
                continue
            rel = str(it.relative_to(root)).replace("\\", "/")
            if gi(rel, it.name):            # 项目 .gitignore 命中（生成物/缓存/日志等）
                continue
            count[0] += 1
            if it.is_dir():
                out.append({
                    "name": it.name, "path": rel, "type": "dir",
                    "children": walk(it, depth + 1) if depth + 1 < max_depth else [],
                })
            else:
                try:
                    size = it.stat().st_size
                except OSError:
                    size = 0
                out.append({"name": it.name, "path": rel, "type": "file", "size": size})
        return out

    return {"name": root.name or str(root), "path": "", "type": "dir", "children": walk(root, 0)}


def read_conventions(root: Path, name: str) -> str:
    """读取工作区根目录的项目规范文件（如 hermes.md）内容，供注入 system。

    文件不存在 / name 为空 / 越界 / 读失败 都返回 ""；超长截断到 CONV_MAX_CHARS。
    """
    if not name:
        return ""
    try:
        p = resolve_within(root, name)
    except ValueError:
        return ""
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:CONV_MAX_CHARS].strip()
    except OSError:
        return ""


def _looks_text(p: Path) -> bool:
    """无扩展名时的兜底：读前 4KB，无 NUL 字节则当文本。"""
    try:
        chunk = p.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def read_file(root: Path, relpath: str) -> dict:
    """按类型读取工作区内某文件。kind ∈ text|html|image|binary|error。"""
    p = resolve_within(root, relpath)
    if not p.is_file():
        return {"kind": "error", "error": "文件不存在或不是文件"}
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        size = 0

    if ext in _IMAGE_EXT:
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            ".ico": "image/x-icon",
        }.get(ext, "image/png")
        b64 = base64.b64encode(p.read_bytes()).decode()
        return {"kind": "image", "name": p.name, "ext": ext, "size": size,
                "dataUrl": f"data:{mime};base64,{b64}"}

    if ext == ".svg":  # SVG 既是文本也是图：当图直观预览
        b64 = base64.b64encode(p.read_bytes()).decode()
        return {"kind": "image", "name": p.name, "ext": ext, "size": size,
                "dataUrl": f"data:image/svg+xml;base64,{b64}"}

    if ext in _TEXT_EXT or ext in _HTML_EXT or _looks_text(p):
        raw = p.read_bytes()[:MAX_FILE_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        return {
            "kind": "html" if ext in _HTML_EXT else "text",
            "name": p.name, "ext": ext, "size": size,
            "text": text, "truncated": size > MAX_FILE_BYTES,
        }

    return {"kind": "binary", "name": p.name, "ext": ext, "size": size}
