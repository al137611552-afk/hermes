"""FR-11.2a 写入后零成本校验：detect_kind / verify_text 纯函数 + 工具集成（无网络）。

运行：python tests/test_verify.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools import build_registry  # noqa: E402
from agentcore.verify import detect_kind, make_verifier, verify_text  # noqa: E402


def test_detect_kind():
    assert detect_kind("a.py") == "py" and detect_kind("b.PYI") == "py"
    assert detect_kind("c.json") == "json"
    assert detect_kind("d.ts") == "node" and detect_kind("e.mjs") == "node"
    assert detect_kind("f.txt") == "" and detect_kind("g.md") == ""


def test_verify_text_python():
    assert verify_text("ok.py", "def f():\n    return 1\n") is None
    bad = verify_text("bad.py", "def f(:\n  pass")
    assert bad and "语法错误" in bad and "bad.py" in bad and "第 1 行" in bad


def test_verify_text_bom_not_false_flagged():
    # 带 UTF-8 BOM 的源码是合法的（Windows 工具常加 BOM），不该误报；但真语法错仍要报
    assert verify_text("ok.py", "﻿def f():\n    return 1\n") is None
    assert verify_text("ok.json", '﻿{"a": 1}') is None
    assert "语法错误" in verify_text("bad.py", "﻿def f(:\n  pass")


def test_verify_text_json():
    assert verify_text("ok.json", '{"a": 1}') is None
    bad = verify_text("bad.json", '{"a": 1,}')
    assert bad and "JSON 格式错误" in bad
    # 非校验类型一律放行
    assert verify_text("x.txt", "随便什么") is None


def test_make_verifier_reads_disk(tmp: Path):
    (tmp / "g.py").write_text("x = (1, 2", encoding="utf-8")    # 缺右括号
    v = make_verifier(tmp)
    assert "语法错误" in v("g.py")
    (tmp / "h.py").write_text("x = 1\n", encoding="utf-8")
    assert v("h.py") is None
    assert v("不存在.py") is None                              # 校验器自身故障不抛
    assert v("notes.txt") is None                              # 非校验类型


def test_write_tools_append_verify_result(tmp: Path):
    """write/edit/multi_edit 落盘后把校验结果并入返回（FR-11.2a）。"""
    reg = build_registry(tmp, verifier=make_verifier(tmp))
    # 写出语法错误的 py -> 返回里带警告（但文件照样写了）
    out = reg.get("write_file").run({"path": "x.py", "content": "def bad(:\n  pass"})
    assert "已写入" in out and "语法错误" in out
    assert (tmp / "x.py").exists()
    # 写出正确的 py -> 无警告
    out2 = reg.get("write_file").run({"path": "y.py", "content": "y = 1\n"})
    assert "已写入" in out2 and "语法错误" not in out2
    # edit 把好文件改坏 -> 警告
    out3 = reg.get("edit_file").run({"path": "y.py", "old_string": "y = 1", "new_string": "y = ("})
    assert "语法错误" in out3
    # 非代码文件不校验
    out4 = reg.get("write_file").run({"path": "note.txt", "content": "随便"})
    assert "语法错误" not in out4


def test_no_verifier_unchanged(tmp: Path):
    """不注入 verifier（auto_verify=false）时行为同旧版。"""
    reg = build_registry(tmp)
    out = reg.get("write_file").run({"path": "x.py", "content": "def bad(:\n  pass"})
    assert out == "已写入 x.py（16 字符）"


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
