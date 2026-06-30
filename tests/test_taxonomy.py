"""错误分类（Error Taxonomy）自测（docs/adr/0015、ROADMAP 块C）——纯逻辑。

验收：三类 Evaluator 的典型失败都能被分类（含 UNKNOWN 兜底）。
运行：python tests/test_taxonomy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.contract import Evaluation  # noqa: E402
from agentcore.agent.evaluators.coding import CodingEvaluator  # noqa: E402
from agentcore.agent.evaluators.search import SearchEvaluator  # noqa: E402
from agentcore.agent.evaluators.shell import ShellEvaluator  # noqa: E402
from agentcore.agent.taxonomy import ErrorClass, classify, classify_text  # noqa: E402


# ---- classify_text：各类规则命中 --------------------------------------------

def test_transient_io_patterns():
    for t in ["命令超时（>30s）", "Connection refused", "EADDRINUSE: address already in use",
              "端口被占用", "read timed out"]:
        assert ErrorClass.TRANSIENT_IO in classify_text(t), t


def test_auth_patterns():
    for t in ["HTTP 401 Unauthorized", "403 Forbidden", "permission denied",
              "invalid api-key", "凭证过期"]:
        assert ErrorClass.AUTH in classify_text(t), t


def test_not_found_patterns():
    for t in ["No such file or directory", "ModuleNotFoundError: no module named x",
              "未找到", "无命中。", "command not found", "404 Not Found"]:
        assert ErrorClass.NOT_FOUND in classify_text(t), t


def test_syntax_patterns():
    for t in ["SyntaxError: invalid syntax", "IndentationError", "unexpected token }",
              "编译报错"]:
        assert ErrorClass.SYNTAX in classify_text(t), t


def test_logic_patterns():
    for t in ["AssertionError", "测试未通过", "FAILED test_x", "expected 3 but got 4"]:
        assert ErrorClass.LOGIC in classify_text(t), t


def test_resource_patterns():
    for t in ["out of memory", "No space left on device", "429 Too Many Requests",
              "quota exceeded", "磁盘空间不足"]:
        assert ErrorClass.RESOURCE in classify_text(t), t


def test_ambiguous_patterns():
    for t in ["did you mean ...", "多个匹配", "ambiguous reference"]:
        assert ErrorClass.AMBIGUOUS in classify_text(t), t


def test_external_blocked_patterns():
    for t in ["请先登录后查看", "扫码登录", "503 Service Unavailable", "captcha 验证码"]:
        assert ErrorClass.EXTERNAL_BLOCKED in classify_text(t), t


def test_no_match_is_empty():
    assert classify_text("一切正常，退出码 0") == []


# ---- 优先级：根因在前、TRANSIENT 最前 ---------------------------------------

def test_priority_transient_first_when_both():
    cls = classify_text("AssertionError 且 connection refused")
    assert cls[0] is ErrorClass.TRANSIENT_IO    # 可重试的最该当主类
    assert ErrorClass.LOGIC in cls


def test_priority_not_found_before_logic():
    # import 缺失常是断言失败的真因 → NOT_FOUND 当主类
    cls = classify_text("ModuleNotFoundError: foo\nAssertionError")
    assert cls.index(ErrorClass.NOT_FOUND) < cls.index(ErrorClass.LOGIC)


# ---- classify(evaluation)：失败才分类，UNKNOWN 兜底，正常不污染 -------------

def test_classify_failure_uses_issues_and_output():
    ev = Evaluation(issues=["测试未全过=blocker"], signals=["测试失败 1 项"])
    assert ErrorClass.LOGIC in classify(ev, "AssertionError: 1 != 2")


def test_classify_unknown_fallback_for_unmatched_failure():
    # 有 issue（失败）但文本没任何规则命中 → UNKNOWN，绝不吞成"没事"
    ev = Evaluation(issues=["某种 blocker"], signals=["怪异信号"])
    assert classify(ev, "完全无法归类的乱码 zzqqxx") == [ErrorClass.UNKNOWN]


def test_classify_no_failure_returns_empty():
    ev = Evaluation(metrics={"exit_code": 0}, signals=["退出码 0"], issues=[])
    assert classify(ev, "[exit code] 0") == []


# ---- 三类 Evaluator 的典型失败 → 可分类（端到端验收）------------------------

def test_e2e_coding_failure_is_logic():
    ev = CodingEvaluator().evaluate("run_shell", "==== 1 failed, 2 passed ====\nAssertionError")
    assert ErrorClass.LOGIC in classify(ev, "AssertionError")


def test_e2e_coding_import_failure_is_not_found():
    ev = CodingEvaluator().evaluate("run_shell", "Traceback\nModuleNotFoundError: no module named foo")
    cls = classify(ev, "ModuleNotFoundError: no module named foo")
    assert cls[0] is ErrorClass.NOT_FOUND


def test_e2e_shell_timeout_is_transient():
    ev = ShellEvaluator().evaluate("run_shell", "命令超时（>30s）。")
    assert ErrorClass.TRANSIENT_IO in classify(ev)


def test_e2e_shell_missing_exe_is_not_found():
    ev = ShellEvaluator().evaluate("run_powershell", "找不到 powershell 可执行程序。")
    assert ErrorClass.NOT_FOUND in classify(ev)


def test_e2e_search_empty_is_not_a_failure():
    # 空检索结果：SearchEvaluator 不判 issue → classify 不当失败（NOT_FOUND 也不算"错")
    ev = SearchEvaluator().evaluate("grep_search", "无命中。")
    assert classify(ev) == []


# ---- 枚举完整性 -------------------------------------------------------------

def test_taxonomy_has_nine_classes_with_unknown():
    names = {c.name for c in ErrorClass}
    assert "UNKNOWN" in names and len(names) == 9


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
