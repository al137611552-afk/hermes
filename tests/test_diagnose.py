"""FR-13.B 报错定位自测（纯解析逻辑 + 临时目录读盘取上下文）。

运行：python tests/test_diagnose.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.diagnose import (  # noqa: E402
    Frame, enrich_traceback, parse_exception_line, parse_traceback,
    pick_workspace_frame, with_location,
)

TB = '''Traceback (most recent call last):
  File "/proj/app.py", line 10, in <module>
    main()
  File "/proj/lib/calc.py", line 5, in compute
    return a / b
ZeroDivisionError: division by zero'''


# ---- parse_traceback / parse_exception_line（纯逻辑）----

def test_parse_frames():
    frames = parse_traceback(TB)
    assert frames == [Frame("/proj/app.py", 10, "<module>"),
                      Frame("/proj/lib/calc.py", 5, "compute")]


def test_parse_no_traceback():
    assert parse_traceback("just some output\nno error here") == []


def test_parse_exception_line():
    assert parse_exception_line(TB) == "ZeroDivisionError: division by zero"
    assert parse_exception_line("no exception") is None


# ---- pick_workspace_frame（纯逻辑判定，需真实文件）----

def test_pick_deepest_workspace_frame(tmp: Path):
    (tmp / "pkg").mkdir()
    (tmp / "pkg" / "calc.py").write_text("def compute():\n    return 1/0\n")
    (tmp / "app.py").write_text("compute()\n")
    tb = (f'Traceback (most recent call last):\n'
          f'  File "{tmp / "app.py"}", line 1, in <module>\n'
          f'  File "{tmp / "pkg" / "calc.py"}", line 2, in compute\n'
          f'ZeroDivisionError: division by zero')
    picked = pick_workspace_frame(parse_traceback(tb), tmp)
    assert picked is not None
    fr, path = picked
    assert fr.func == "compute" and path == (tmp / "pkg" / "calc.py").resolve()  # 取最深的工作区帧


def test_pick_skips_frames_outside_workspace(tmp: Path):
    # 只有 stdlib 帧（不在工作区、文件也不在）→ None
    tb = ('Traceback (most recent call last):\n'
          '  File "/usr/lib/python3.11/json/__init__.py", line 1, in loads\n'
          'ValueError: bad')
    assert pick_workspace_frame(parse_traceback(tb), tmp) is None


# ---- enrich_traceback（读盘取上下文）----

def test_enrich_shows_source_context(tmp: Path):
    (tmp / "calc.py").write_text(
        "def compute(a, b):\n"
        "    x = a + b\n"
        "    return a / b\n"   # 第 3 行崩
        "# tail\n")
    tb = (f'Traceback (most recent call last):\n'
          f'  File "{tmp / "calc.py"}", line 3, in compute\n'
          f'    return a / b\n'
          f'ZeroDivisionError: division by zero')
    out = enrich_traceback(tb, tmp)
    assert out is not None
    assert "calc.py:3" in out
    assert "→    3 | " in out and "return a / b" in out      # 崩的那行带箭头
    assert "x = a + b" in out                                 # 上文也在
    assert "ZeroDivisionError: division by zero" in out       # 异常行


def test_enrich_relative_path_in_tb(tmp: Path):
    # traceback 里是相对路径（cwd=工作区跑出来的）也能定位
    (tmp / "m.py").write_text("def f():\n    raise ValueError('x')\n")
    tb = ('Traceback (most recent call last):\n'
          '  File "m.py", line 2, in f\n'
          'ValueError: x')
    out = enrich_traceback(tb, tmp)
    assert out is not None and "m.py:2" in out and "raise ValueError" in out


def test_enrich_none_when_no_workspace_frame(tmp: Path):
    tb = ('Traceback (most recent call last):\n'
          '  File "/elsewhere/x.py", line 1, in <module>\n'
          'RuntimeError: nope')
    assert enrich_traceback(tb, tmp) is None


def test_enrich_none_when_no_traceback(tmp: Path):
    assert enrich_traceback("all good, tests passed", tmp) is None


def test_with_location_appends_or_passthrough(tmp: Path):
    (tmp / "z.py").write_text("def g():\n    return 1/0\n")
    tb = (f'oops\nTraceback (most recent call last):\n'
          f'  File "{tmp / "z.py"}", line 2, in g\n'
          f'ZeroDivisionError: division by zero')
    enriched = with_location(tb, tmp)
    assert tb in enriched and "📍 报错定位" in enriched      # 原文保留 + 追加定位
    assert with_location("plain output", tmp) == "plain output"  # 无 tb 原样返回


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
