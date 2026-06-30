"""FR-9.2 代码库检索：codeindex 纯逻辑 + code_outline/find_symbol 工具（无网络）。

运行：python tests/test_codeindex.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.codeindex import (  # noqa: E402
    extract_generic,
    extract_python,
    walk_find,
    walk_outline,
)
from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.codesearch import CodeOutlineTool, FindSymbolTool  # noqa: E402

_PY = '''\
import os

def top_func(a, b=1):
    return a + b

class Foo:
    def method_one(self):
        pass
    async def method_two(self, x):
        return x

async def top_async(y):
    return y
'''

_JS = '''\
export class Widget {}
function doThing(a) {}
export async function loadAll() {}
const handler = (e) => { return e; };
let make = function () {};
'''


# ---- 纯逻辑：Python（ast） --------------------------------------------------

def test_extract_python():
    syms = extract_python(_PY)
    by = {(s.kind, s.name): s for s in syms}
    assert ("function", "top_func") in by
    assert ("function", "top_async") in by
    assert ("class", "Foo") in by
    assert ("method", "method_one") in by and by[("method", "method_one")].parent == "Foo"
    assert ("method", "method_two") in by
    # 签名 + 行号
    assert "def top_func(a, b=1)" in by[("function", "top_func")].signature
    assert "async def top_async" in by[("function", "top_async")].signature
    assert by[("class", "Foo")].line == 6


def test_extract_python_syntax_error_safe():
    assert extract_python("def (((") == []


# ---- 纯逻辑：其它语言（正则） ----------------------------------------------

def test_extract_generic_js():
    names = {s.name for s in extract_generic(_JS)}
    assert {"Widget", "doThing", "loadAll", "handler", "make"} <= names


# ---- 遍历 ------------------------------------------------------------------

def _make_tree(tmp: Path):
    (tmp / "pkg").mkdir()
    (tmp / "pkg" / "a.py").write_text(_PY, encoding="utf-8")
    (tmp / "web").mkdir()
    (tmp / "web" / "app.js").write_text(_JS, encoding="utf-8")
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "junk.py").write_text("def should_skip(): pass", encoding="utf-8")
    (tmp / "notes.txt").write_text("not code", encoding="utf-8")


def test_walk_outline_skips_noise_and_nonsource(tmp: Path):
    _make_tree(tmp)
    files, truncated = walk_outline(tmp, tmp)
    rels = {rel for rel, _ in files}
    assert any(r.endswith("a.py") for r in rels)
    assert any(r.endswith("app.js") for r in rels)
    assert not any("junk" in r for r in rels)       # __pycache__ 跳过
    assert not any("notes.txt" in r for r in rels)   # 非源码跳过
    assert truncated is False


def test_walk_find_exact_then_loose(tmp: Path):
    _make_tree(tmp)
    hits, more, exact = walk_find(tmp, tmp, "top_func")
    assert exact is True and len(hits) == 1 and hits[0][1].name == "top_func"
    # 子串回退
    hits2, _, exact2 = walk_find(tmp, tmp, "method")
    assert exact2 is False and {h[1].name for h in hits2} == {"method_one", "method_two"}
    # 找不到
    hits3, _, _ = walk_find(tmp, tmp, "不存在的符号xyz")
    assert hits3 == []


# ---- 工具 ------------------------------------------------------------------

def test_code_outline_tool(tmp: Path):
    _make_tree(tmp)
    out = CodeOutlineTool(tmp).run({"path": "pkg"})
    assert "a.py" in out and "def top_func(a, b=1)" in out and "class Foo" in out
    # 单文件
    out2 = CodeOutlineTool(tmp).run({"path": "web/app.js"})
    assert "Widget" in out2 and "loadAll" in out2


def test_find_symbol_tool(tmp: Path):
    _make_tree(tmp)
    out = FindSymbolTool(tmp).run({"name": "Foo"})
    assert "a.py" in out and "[class]" in out
    miss = FindSymbolTool(tmp).run({"name": "zzz_nope"})
    assert "未找到" in miss
    try:
        FindSymbolTool(tmp).run({"name": "  "}); assert False, "空 name 应报错"
    except ToolError:
        pass


def test_registry_registers_codesearch(tmp: Path):
    names = build_registry(tmp).names()
    assert "code_outline" in names and "find_symbol" in names


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
