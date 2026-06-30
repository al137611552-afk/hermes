"""FR-13.C 受影响测试探测 + 运行命令选择自测（纯逻辑 + 临时目录发现/运行）。

运行：python tests/test_affected_tests.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.verify import (  # noqa: E402
    affected_tests, detect_test_argv, discover_test_files, is_pytest_style,
    is_test_file, make_affected_test_runner, make_post_edit_checker, subject_of_test,
)


# ---- subject_of_test / is_test_file ----

def test_subject_prefix_suffix():
    assert subject_of_test("tests/test_workspace.py") == "workspace"
    assert subject_of_test("foo_test.py") == "foo"
    assert subject_of_test("web/pure.test.js") == "pure"
    assert subject_of_test("tests/test_p4_vision.py") == "p4_vision"


def test_subject_non_test_is_none():
    assert subject_of_test("src/agentcore/workspace.py") is None
    assert subject_of_test("web/app.js") is None
    assert subject_of_test("README.md") is None


def test_is_test_file():
    assert is_test_file("tests/test_x.py")
    assert is_test_file("a/b.test.js")
    assert not is_test_file("src/x.py")


# ---- affected_tests（核心纯逻辑）----

TESTS = ["tests/test_workspace.py", "tests/test_verify.py", "web/pure.test.js"]


def test_affected_source_maps_to_test():
    assert affected_tests("src/agentcore/workspace.py", TESTS) == ["tests/test_workspace.py"]


def test_affected_frontend_pure():
    assert affected_tests("web/pure.js", TESTS) == ["web/pure.test.js"]


def test_affected_editing_test_runs_itself():
    assert affected_tests("tests/test_verify.py", TESTS) == ["tests/test_verify.py"]


def test_affected_no_match_empty():
    # app.js 没有对应 app.test.js → 空（自然只测有测试覆盖的文件）
    assert affected_tests("web/app.js", TESTS) == []
    assert affected_tests("src/agentcore/loop.py", TESTS) == []


def test_affected_backslash_normalized():
    assert affected_tests("src\\agentcore\\workspace.py", TESTS) == ["tests/test_workspace.py"]


def test_affected_dedup_and_cap():
    many = [f"tests/test_dup.py", "tests/sub/test_dup.py"]  # 同主题不同路径，去重后保留两个
    got = affected_tests("dup.py", many)
    assert got == sorted(set(many))


# ---- detect_test_argv ----

def test_detect_argv_py_pytest():
    argv = detect_test_argv("tests/test_x.py", pytest_available=True, node_available=False)
    assert "pytest" in argv
    assert argv[-1] == "tests/test_x.py"


def test_detect_argv_py_standalone():
    argv = detect_test_argv("tests/test_x.py", pytest_available=False, node_available=False)
    assert "pytest" not in argv
    assert argv[-1] == "tests/test_x.py"


def test_detect_argv_node():
    argv = detect_test_argv("a.test.js", pytest_available=False, node_available=True)
    assert argv == ["node", "--test", "a.test.js"]
    assert detect_test_argv("a.test.js", pytest_available=False, node_available=False) is None


def test_detect_argv_other_none():
    assert detect_test_argv("a.txt", pytest_available=True, node_available=True) is None


# ---- discover_test_files（受控 IO）----

def test_discover_skips_noise(tmp: Path):
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_a.py").write_text("x")
    (tmp / "src.py").write_text("x")  # 非测试
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "test_cached.py").write_text("x")  # 噪声目录跳过
    (tmp / "web").mkdir()
    (tmp / "web" / "b.test.js").write_text("x")
    found = discover_test_files(tmp)
    assert "tests/test_a.py" in found
    assert "web/b.test.js" in found
    assert all("__pycache__" not in f for f in found)
    assert "src.py" not in found


# ---- make_affected_test_runner（端到端：真发现 + 真子进程）----

def test_runner_failing_test_reports(tmp: Path):
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_thing.py").write_text(
        "assert 1 == 2, 'boom'\n")  # 当独立脚本跑必失败
    (tmp / "thing.py").write_text("x = 1\n")
    run = make_affected_test_runner(tmp, runner="python")  # 强制独立脚本，与环境是否有 pytest 无关
    msg = run("thing.py")
    assert msg is not None and "未通过" in msg and "test_thing.py" in msg


def test_runner_passing_test_silent(tmp: Path):
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_ok.py").write_text("assert 1 == 1\n")
    (tmp / "ok.py").write_text("x = 1\n")
    run = make_affected_test_runner(tmp, runner="python")
    assert run("ok.py") is None  # 通过 → 不打扰


def test_is_pytest_style():
    assert is_pytest_style("def test_add():\n    assert 1")
    assert is_pytest_style("import pytest\n")
    assert is_pytest_style("@pytest.mark.parametrize\ndef test_x(): pass")
    assert not is_pytest_style("from m import x\nassert x == 1")  # 模块级断言不是 pytest 风格
    assert not is_pytest_style("")


def test_runner_no_pytest_warns_not_false_pass(tmp: Path):
    # 防呆：pytest 风格测试 + 无 pytest → 报"需装 pytest"，绝不静默通过（即便代码是坏的）
    (tmp / "calc.py").write_text("def add(a, b):\n    return a - b\n")  # bug
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_calc.py").write_text(
        "from calc import add\ndef test_a():\n    assert add(2, 2) == 4\n")
    out = make_affected_test_runner(tmp, runner="python")("calc.py")  # 强制无 pytest
    assert out is not None and "未装 pytest" in out  # 不是 None（假通过），而是明确提示


def test_runner_imports_workspace_root_module(tmp: Path):
    # 测试文件 `from calc import add`（无 sys.path 样板）应能 import 工作区根的 calc.py
    # —— 锁住 PYTHONPATH 注入工作区根的修复（真机 ModuleNotFoundError 回归）。
    (tmp / "tests").mkdir()
    (tmp / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp / "tests" / "test_calc.py").write_text(
        "from calc import add\nassert add(2, 2) == 4, 'add 错了'\n")
    run = make_affected_test_runner(tmp, runner="python")
    assert run("calc.py") is None  # 能 import + 断言通过 → 静默（修复前会 ModuleNotFoundError）
    # 改坏 → 报的是 AssertionError（已能 import），不是 ModuleNotFoundError
    (tmp / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    msg = run("calc.py")
    assert msg is not None and "AssertionError" in msg and "ModuleNotFoundError" not in msg


def test_runner_imports_src_layout_module(tmp: Path):
    # src 布局：`src/pkg/m.py`，测试 `from pkg.m import f` 应能 import（PYTHONPATH 含 src）。
    (tmp / "src" / "pkg").mkdir(parents=True)
    (tmp / "src" / "pkg" / "__init__.py").write_text("")
    (tmp / "src" / "pkg" / "thing.py").write_text("def f():\n    return 1\n")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_thing.py").write_text(
        "from pkg.thing import f\nassert f() == 1\n")
    run = make_affected_test_runner(tmp, runner="python")
    # 改 src/pkg/thing.py（stem=thing）→ 命中 test_thing.py，能 import 即静默
    assert run("src/pkg/thing.py") is None


def test_runner_no_affected_silent(tmp: Path):
    (tmp / "lonely.py").write_text("x = 1\n")
    run = make_affected_test_runner(tmp, runner="python")
    assert run("lonely.py") is None  # 没有对应测试 → 跳过


def test_runner_non_code_silent(tmp: Path):
    (tmp / "notes.md").write_text("# hi\n")
    run = make_affected_test_runner(tmp, runner="python")
    assert run("notes.md") is None


# ---- make_post_edit_checker（语法校验 + 受影响测试 组合）----

def test_checker_both_off_is_none():
    assert make_post_edit_checker(Path("."), auto_verify=False, auto_affected_test=False) is None


def test_checker_syntax_fail_shortcircuits(tmp: Path):
    # 语法坏 → 返回语法错、不去跑测试（即便有受影响的失败测试也不触发）
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_broken.py").write_text("assert 1 == 2\n")
    (tmp / "broken.py").write_text("def f(:\n  pass\n")  # 语法错
    check = make_post_edit_checker(tmp, auto_verify=True, auto_affected_test=True, runner="python")
    msg = check("broken.py")
    assert msg is not None and "语法错误" in msg and "未通过" not in msg


def test_checker_syntax_ok_runs_affected(tmp: Path):
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_good.py").write_text("assert 1 == 2, 'boom'\n")  # 受影响测试会失败
    (tmp / "good.py").write_text("x = 1\n")  # 语法 OK
    check = make_post_edit_checker(tmp, auto_verify=True, auto_affected_test=True, runner="python")
    msg = check("good.py")
    assert msg is not None and "未通过" in msg and "test_good.py" in msg


def test_checker_verify_only_skips_tests(tmp: Path):
    # 只开 auto_verify：语法过即返回 None，不跑测试
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_x.py").write_text("assert 1 == 2\n")
    (tmp / "x.py").write_text("y = 1\n")
    check = make_post_edit_checker(tmp, auto_verify=True, auto_affected_test=False, runner="python")
    assert check("x.py") is None


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
