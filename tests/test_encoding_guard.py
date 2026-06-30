"""编码守卫（防 bug③「Windows GBK 默认编码」同根复发）：AST 扫全部源码，
任何 subprocess(text=True) / Path.read_text / write_text / open(文本模式) **必须显式带 encoding**。
否则 Windows 中文环境按 GBK 解码 UTF-8 内容会 UnicodeDecodeError 崩/卡（命令输出、读 UTF-8 文件等）。

运行：python tests/test_encoding_guard.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _kwval(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _scan_file(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        attr = fn.attr if isinstance(fn, ast.Attribute) else None
        # subprocess.run / Popen 文本模式必须带 encoding
        if attr in ("run", "Popen"):
            tv, uv = _kwval(node, "text"), _kwval(node, "universal_newlines")
            text_mode = (isinstance(tv, ast.Constant) and tv.value is True) or \
                        (isinstance(uv, ast.Constant) and uv.value is True)
            if text_mode and not _has_kw(node, "encoding"):
                bad.append(f"{path.name}:{node.lineno} subprocess.{attr}(text=True) 缺 encoding")
        # Path.read_text / write_text 必须带 encoding
        if attr in ("read_text", "write_text") and not _has_kw(node, "encoding"):
            bad.append(f"{path.name}:{node.lineno} {attr}() 缺 encoding")
        # open(...) 文本模式必须带 encoding
        if isinstance(fn, ast.Name) and fn.id == "open":
            mode = node.args[1] if len(node.args) > 1 else _kwval(node, "mode")
            mstr = mode.value if isinstance(mode, ast.Constant) else ""
            if "b" not in (mstr or "") and not _has_kw(node, "encoding"):
                bad.append(f"{path.name}:{node.lineno} open() 文本模式缺 encoding")
    return bad


def test_all_text_io_specifies_encoding():
    offenders = []
    for f in SRC.rglob("*.py"):
        offenders += _scan_file(f)
    assert not offenders, "以下文本 I/O 漏了 encoding=（Windows GBK 会崩）：\n  " + "\n  ".join(offenders)


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    sys.exit(_run_all())
