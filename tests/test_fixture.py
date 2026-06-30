"""FR-13.E 失败固化 fixture 自测（纯逻辑 + 端到端写盘并跑一次）。

运行：python tests/test_fixture.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.fixture import (  # noqa: E402
    CaptureFixtureTool, build_fixture_content, slugify,
)


# ---- 纯逻辑 ----

def test_slugify():
    assert slugify("Yield Zero Years!") == "yield_zero_years"
    assert slugify("  a/b.c  ") == "a_b_c"
    assert slugify("") == "unnamed"
    assert slugify("___") == "unnamed"


def test_build_content_has_header_and_body():
    c = build_fixture_content("assert 1 == 2", "x→错", "2026-06-23")
    assert "固化复现 fixture" in c and "现象：x→错" in c and "2026-06-23" in c
    assert c.rstrip().endswith("assert 1 == 2")


# ---- 端到端 ----

def test_capture_reproduces_bug(tmp: Path):
    (tmp / "calc.py").write_text("def add(a, b):\n    return a - b\n")  # bug：应是 +
    out = str(CaptureFixtureTool(tmp).run({
        "name": "add bug",
        "body": "from calc import add\nassert add(2, 2) == 4, '应为 4'",
        "note": "add(2,2) 返回 0，应为 4",
    }))
    # 文件落地、命名规范
    f = tmp / "tests" / "test_capture_add_bug.py"
    assert f.is_file()
    assert "现象：add(2,2)" in f.read_text()
    # 跑一次确认当前复现（失败）
    assert "已固化复现 fixture：tests/test_capture_add_bug.py" in out
    assert "已确认当前复现" in out


def test_capture_warns_if_not_reproducing(tmp: Path):
    (tmp / "calc.py").write_text("def add(a, b):\n    return a + b\n")  # 已对
    out = str(CaptureFixtureTool(tmp).run({
        "name": "ok", "body": "from calc import add\nassert add(2, 2) == 4"}))
    assert "当前就通过了" in out  # 没复现出 bug，提示调整


def test_capture_empty_args_raise(tmp: Path):
    t = CaptureFixtureTool(tmp)
    for p in ({"name": "", "body": "x"}, {"name": "n", "body": " "}):
        try:
            t.run(p)
            assert False, "应抛 ToolError"
        except ToolError:
            pass


def test_capture_fixture_plugs_into_affected_tests(tmp: Path):
    # 固化的 fixture 命名为 tests/test_capture_<slug>.py，应被 FR-13.C 受影响测试发现
    from agentcore.verify import affected_tests
    (tmp / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    CaptureFixtureTool(tmp).run({"name": "addbug",
                                 "body": "from calc import add\nassert add(2,2)==4"})
    # 改 calc.py 时，受影响测试探测能不能把这个 capture fixture 也算进去？
    # 它的主题是 capture_addbug，不直接匹配 calc；但编辑该 fixture 自身会跑它。
    tests = ["tests/test_capture_addbug.py"]
    assert affected_tests("tests/test_capture_addbug.py", tests) == ["tests/test_capture_addbug.py"]


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
