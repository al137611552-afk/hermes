"""代码符号索引（FR-9.2，纯逻辑、按需扫描、无新依赖）。

给超出 grep/glob 的两件事提供支撑：
- 项目/目录/文件的**符号大纲**（类、函数、方法 + 签名 + 行号）——让 Agent 不读全文件就掌握结构；
- 跨工作区**按名找定义**——比 grep 准（只给定义，不给所有提及）。

Python 用标准库 `ast` 精确抽取；其它语言（JS/TS/Go/Rust/Java/C…）用轻量正则兜底。
与 IO 分离：本模块只接收源码字符串/路径，遍历与上限在 walk_* 里，工具层只做包装，便于单测。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# 跳过的噪音目录（与 tools/search.py 一致）
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

_PY_EXT = {".py"}
_GENERIC_EXT = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".rb", ".php", ".kt", ".swift",
}

# 扫描上限（防大项目把上下文撑爆）
MAX_FILES = 600
MAX_SYMBOLS = 1200
MAX_FILE_BYTES = 1_000_000

# 其它语言的轻量定义模式（逐行匹配，捕获组 1 = 符号名）
_GENERIC_PATTERNS = [
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\*?\s*([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")),
    ("func", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),      # Go / 方法
    ("fn", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),    # Rust
    ("def", re.compile(r"^\s*def\s+([A-Za-z_]\w*)")),                          # Ruby
]


@dataclass
class Symbol:
    kind: str                 # class / function / method / interface / func / fn / def
    name: str
    line: int
    signature: str
    parent: str = ""          # 方法所属的类名（其它为空）


# ---- 抽取（纯函数） --------------------------------------------------------

def _py_signature(node) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 — 极端语法兜底
        args = "..."
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({args})"


def extract_python(source: str) -> list[Symbol]:
    """用 ast 抽取顶层函数/类及类内方法。语法错误返回空。"""
    out: list[Symbol] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(Symbol("function", node.name, node.lineno, _py_signature(node)))
        elif isinstance(node, ast.ClassDef):
            out.append(Symbol("class", node.name, node.lineno, f"class {node.name}"))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(Symbol("method", sub.name, sub.lineno,
                                      _py_signature(sub), parent=node.name))
    return out


def extract_generic(source: str) -> list[Symbol]:
    """正则逐行抽取其它语言的定义（尽力而为，可能漏/略多，但便宜无依赖）。"""
    out: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(source.splitlines(), 1):
        for kind, rx in _GENERIC_PATTERNS:
            m = rx.match(line)
            if m:
                name = m.group(1)
                if (name, i) in seen:
                    continue
                seen.add((name, i))
                out.append(Symbol(kind, name, i, line.strip()[:160]))
                break  # 一行只取第一个匹配
    return out


def extract_file(path: Path, source: str) -> list[Symbol]:
    ext = path.suffix.lower()
    if ext in _PY_EXT:
        return extract_python(source)
    if ext in _GENERIC_EXT:
        return extract_generic(source)
    return []


def is_indexable(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in _PY_EXT or ext in _GENERIC_EXT


# ---- 遍历（IO，带上限） ----------------------------------------------------

def _iter_source_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        if not is_indexable(p):
            continue
        yield p


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def walk_outline(root: Path, workspace: Path,
                 max_files: int = MAX_FILES, max_symbols: int = MAX_SYMBOLS):
    """遍历 root 下可索引文件，返回 [(相对路径, [Symbol])]（仅含有符号的文件）+ 是否被截断。"""
    result: list[tuple[str, list[Symbol]]] = []
    n_files = n_syms = 0
    truncated = False
    for p in _iter_source_files(root):
        if n_files >= max_files or n_syms >= max_symbols:
            truncated = True
            break
        src = _read(p)
        if src is None:
            continue
        syms = extract_file(p, src)
        if not syms:
            continue
        n_files += 1
        n_syms += len(syms)
        result.append((str(p.relative_to(workspace)), syms))
    return result, truncated


def walk_find(root: Path, workspace: Path, name: str, max_hits: int = 80):
    """跨 root 找名为 name 的定义（先精确，全无则按子串）。返回 [(相对路径, Symbol)]。"""
    name = name.strip()
    exact: list[tuple[str, Symbol]] = []
    loose: list[tuple[str, Symbol]] = []
    lname = name.lower()
    for p in _iter_source_files(root):
        src = _read(p)
        if src is None:
            continue
        rel = str(p.relative_to(workspace))
        for s in extract_file(p, src):
            if s.name == name:
                exact.append((rel, s))
            elif lname in s.name.lower():
                loose.append((rel, s))
    hits = exact or loose
    return hits[:max_hits], (len(hits) > max_hits), bool(exact)


# ---- 格式化（纯函数，给模型看的文本） --------------------------------------

def format_outline(files, truncated: bool) -> str:
    if not files:
        return "未发现可识别的代码符号（可能是空目录或不支持的语言）。"
    lines: list[str] = []
    for rel, syms in files:
        lines.append(rel)
        for s in syms:
            indent = "    " if s.kind == "method" else "  "
            lines.append(f"{indent}L{s.line} {s.signature}")
    if truncated:
        lines.append(f"... (已达上限，结果截断；可对子目录再次 code_outline 细看)")
    return "\n".join(lines)


def format_finds(hits, more: bool, exact: bool, name: str) -> str:
    if not hits:
        return f"未找到名为「{name}」的定义。"
    head = "" if exact else f"未找到精确匹配「{name}」，以下为名称包含它的定义：\n"
    lines = [head] if head else []
    for rel, s in hits:
        owner = f"{s.parent}." if s.parent else ""
        lines.append(f"{rel}:{s.line}: [{s.kind}] {owner}{s.signature}")
    if more:
        lines.append("... (命中过多已截断)")
    return "\n".join(lines)
