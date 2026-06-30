"""情境自启②：工作区探测 + 智能默认自测（纯逻辑 + 临时目录探测）。

运行：python tests/test_profile.py
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.profile import (  # noqa: E402
    ProjectProfile, compute_smart_defaults, describe_smart_defaults,
    detect_project_profile,
)


@dataclass
class Agent:  # AgentConfig 替身（只取用到的字段）
    auto_affected_test: bool = False
    auto_review: bool = False


# ---- compute_smart_defaults（纯逻辑）--------------------------------------

def test_has_tests_enables_affected_test():
    p = ProjectProfile(has_tests=True, n_code_files=10, is_git=False)
    assert compute_smart_defaults(p, set(), Agent()) == {"auto_affected_test": True}


def test_no_tests_no_default():
    p = ProjectProfile(has_tests=False, n_code_files=10, is_git=False)
    assert compute_smart_defaults(p, set(), Agent()) == {}


def test_respects_user_panel_choice():
    # 用户在面板设过 auto_affected_test（不论开还是关）-> 智能默认让位，不动
    p = ProjectProfile(has_tests=True, n_code_files=10, is_git=False)
    assert compute_smart_defaults(p, {"auto_affected_test"}, Agent()) == {}


def test_skips_when_already_on():
    # 已经开着的不再重复开（避免重复事件）
    p = ProjectProfile(has_tests=True, n_code_files=10, is_git=False)
    assert compute_smart_defaults(p, set(), Agent(auto_affected_test=True)) == {}


def test_describe_message():
    assert "改完跑定向测试" in describe_smart_defaults({"auto_affected_test": True})
    assert "可在" in describe_smart_defaults({"auto_affected_test": True})  # 含可覆盖提示
    assert describe_smart_defaults({}) == ""


def test_is_large_property():
    assert ProjectProfile(False, 200, False).is_large
    assert not ProjectProfile(False, 50, False).is_large


# ---- detect_project_profile（受控 IO）-------------------------------------

def test_detect_with_tests(tmp: Path):
    (tmp / "src.py").write_text("x = 1\n")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_x.py").write_text("def test_a(): assert 1\n")
    prof = detect_project_profile(tmp)
    assert prof.has_tests is True and prof.n_code_files >= 2 and prof.is_git is False


def test_detect_without_tests(tmp: Path):
    (tmp / "app.py").write_text("print(1)\n")
    (tmp / "readme.md").write_text("# hi\n")   # 非代码不计
    prof = detect_project_profile(tmp)
    assert prof.has_tests is False and prof.n_code_files == 1


def test_detect_skips_noise(tmp: Path):
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "test_lib.py").write_text("x")  # 噪声目录里的测试不算
    (tmp / "main.py").write_text("x=1\n")
    prof = detect_project_profile(tmp)
    assert prof.has_tests is False  # node_modules 里的 test 被跳过


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
