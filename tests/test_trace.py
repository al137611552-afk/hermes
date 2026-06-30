"""FR-13.D 运行时值追踪工具自测（纯逻辑 format_trace + 端到端真子进程追踪）。

运行：python tests/test_trace.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.trace import TraceRunTool, format_trace  # noqa: E402


# ---- format_trace（纯逻辑）----

def test_format_empty_events():
    out = format_trace({"events": [], "stdout": "", "error": None})
    assert "未记录到" in out


def test_format_steps_and_return():
    res = {"events": [
        {"func": "f", "line": 2, "src": "y = x + 1", "changed": {"x": "3"}},
        {"func": "f", "line": 3, "src": "return y", "changed": {"y": "4"}},
        {"func": "f", "ret": "4"},
    ], "stdout": "", "error": None}
    out = format_trace(res, target="f")
    assert "f:2" in out and "x=3" in out and "y=4" in out
    assert "返回 = 4" in out
    assert "聚焦：f" in out


def test_format_includes_stdout_and_error():
    res = {"events": [], "stdout": "hello", "error": "Traceback...\nValueError: boom"}
    out = format_trace(res)
    assert "hello" in out and "ValueError: boom" in out


def test_format_capped_note():
    res = {"events": [{"func": "f", "line": 1, "src": "x=1", "changed": {}}],
           "stdout": "", "error": None, "capped": True}
    assert "上限" in format_trace(res)


# ---- 端到端（真子进程 settrace 追踪）----

def _tool(tmp: Path) -> TraceRunTool:
    return TraceRunTool(tmp)


def test_trace_records_intermediate_values(tmp: Path):
    (tmp / "calc.py").write_text(
        "def yield_rate(principal, years):\n"
        "    total = principal\n"
        "    for _ in range(years):\n"
        "        total = total * 11 // 10\n"
        "    return total - principal\n")
    out = _tool(tmp).run({"code": "from calc import yield_rate\nprint(yield_rate(100, 2))",
                          "target": "yield_rate"})
    s = str(out)
    assert "yield_rate" in s
    assert "total=" in s          # 看到中间变量 total 的演化
    assert "返回 =" in s          # 看到返回值


def test_trace_captures_exception_before_crash(tmp: Path):
    (tmp / "boom.py").write_text(
        "def div(a, b):\n"
        "    mid = a + b\n"
        "    return mid / 0\n")  # 必崩
    out = str(_tool(tmp).run({"code": "from boom import div\ndiv(2, 3)", "target": "div"}))
    assert "mid=5" in out                  # 崩溃前的中间值仍拿到
    assert "ZeroDivisionError" in out      # 异常被回传


def test_trace_target_focus_excludes_others(tmp: Path):
    (tmp / "m.py").write_text(
        "def helper(x):\n    return x * 2\n"
        "def main(x):\n    return helper(x) + 1\n")
    out = str(_tool(tmp).run({"code": "from m import main\nmain(5)", "target": "main"}))
    assert "main" in out
    assert "helper:" not in out  # 聚焦 main，helper 的帧不记录


def test_trace_empty_code_raises(tmp: Path):
    try:
        _tool(tmp).run({"code": "  "})
        assert False, "应抛 ToolError"
    except ToolError:
        pass


def test_trace_bad_import_reports(tmp: Path):
    # 驱动代码 import 不存在的模块：异常被捕获并回传（含定位线索），不崩工具
    out = str(_tool(tmp).run({"code": "import does_not_exist_xyz"}))
    assert "ModuleNotFoundError" in out or "未产出" in out


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
