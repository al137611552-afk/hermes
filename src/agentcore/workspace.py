"""工作区文件树与只读预览（右侧面板用）。纯函数，便于单测、不碰网络。

只读：构建工作区目录树、把路径安全地解析到工作区内、按类型读取文件内容
（文本/代码、图片、HTML、二进制）。路径越界一律拒绝。
"""
from __future__ import annotations

import base64
import os
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


def open_plan(platform: str, path: str) -> list:
    """按平台给出「用系统默认程序打开」依次尝试的手段（纯逻辑，便于单测）。

    返回一串步骤，每步是 `("startfile", 原生路径)`、`("run", [argv…])` 或 `("browser", file URI)`，
    由 `open_in_default_app` 依次执行到某步成功为止。

    **为什么首选原生路径而不是 file:// URI**（这正是"打开已有项目后点『在浏览器打开』没反应"的根因）：
    `Path.as_uri()` 会把非 ASCII 与空格百分号编码——`C:\\Users\\张三\\我的项目\\index.html`
    变成 `file:///C:/Users/%E5%BC%A0%E4%B8%89/...`，Windows 的 ShellExecute 对这种百分号编码的
    file URI 解码并不可靠，于是静默失败。hermes 自带的默认工作区在安装目录下（纯 ASCII 无空格），
    URI 不带编码所以一直好用；**用户"打开已有项目"选的路径才常含中文/空格**，故只在那条路上暴雷。
    传原生路径就完全绕开了编码这一环。
    """
    p = str(path)
    if platform.startswith("win"):
        steps: list = [("startfile", p)]
    elif platform == "darwin":
        steps = [("run", ["open", p])]
    else:
        steps = [("run", ["xdg-open", p])]
    uri = _file_uri(p)
    if uri:                       # 兜底手段；URI 表达不出来（相对路径等）就只留首选手段
        steps.append(("browser", uri))
    return steps


def _file_uri(path: str) -> "str | None":
    """尽力给出 file:// URI；表达不出来就返回 None（`as_uri()` 对相对路径会抛 ValueError，
    这里绝不让它把整条打开路径炸掉——它只是兜底手段）。"""
    try:
        return Path(path).absolute().as_uri()
    except Exception:  # noqa: BLE001
        return None


def open_in_default_app(path: Path, *, platform: "str | None" = None,
                        startfile=None, run=None, browser=None) -> "tuple[bool, str]":
    """用系统默认程序打开一个文件，返回 (是否成功, 失败原因)。受控 IO，可注入依赖单测。

    每一步都**看返回值/异常**再决定成不成——`webbrowser.open()` 打不开时返回 False 而不是抛异常，
    老实现忽略了它、一律回 ok:True，于是前端拿到"成功"却什么也没发生（用户视角＝点了没反应、
    也没有任何报错）。全部手段都失败时把原因带回去，让前端能提示用户。
    """
    import subprocess
    import sys as _sys
    plat = platform if platform is not None else _sys.platform
    if startfile is None:
        startfile = getattr(os, "startfile", None)
    if run is None:
        def run(argv):
            return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL) is not None
    if browser is None:
        import webbrowser
        browser = webbrowser.open

    errors: list[str] = []
    for kind, arg in open_plan(plat, str(path)):
        try:
            if kind == "startfile":
                if startfile is None:
                    continue
                startfile(arg)          # 无返回值：不抛异常即视为已交给系统
                return True, ""
            if kind == "run":
                if run(arg):
                    return True, ""
                errors.append(f"{arg[0]} 未能打开")
            elif kind == "browser":
                if browser(arg):
                    return True, ""
                errors.append("系统未注册可用的默认浏览器")
        except FileNotFoundError:
            errors.append(f"找不到 {arg[0] if isinstance(arg, list) else arg}")
        except Exception as e:  # noqa: BLE001 — 换下一种手段，别把整条路堵死
            errors.append(f"{type(e).__name__}: {e}")
    return False, "；".join(errors) or "没有可用的打开方式"


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
