"""事实层 Evaluator 自测（docs/adr/0014 块B）——纯逻辑，喂真实格式的工具输出。

运行：python tests/test_evaluators.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.evaluators import evaluate, score  # noqa: E402
from agentcore.agent.evaluators.coding import CodingEvaluator  # noqa: E402
from agentcore.agent.evaluators.search import SearchEvaluator  # noqa: E402
from agentcore.agent.evaluators.shell import ShellEvaluator  # noqa: E402


# ---- CodingEvaluator：测试/构建输出 ------------------------------------------

def test_coding_pytest_all_passed():
    e = CodingEvaluator().evaluate("run_shell", "===== 3 passed in 0.42s =====")
    assert e.metrics == {"passed": 3, "failed": 0, "errors": 0, "total": 3}
    assert "测试全过" in e.signals and e.issues == []
    assert e.confidence == 1.0


def test_coding_pytest_some_failed_is_blocker():
    e = CodingEvaluator().evaluate("run_shell", "==== 1 failed, 2 passed in 0.3s ====")
    assert e.metrics["failed"] == 1 and e.metrics["total"] == 3
    assert "测试未全过=blocker" in e.issues


def test_coding_pytest_errors_counted():
    e = CodingEvaluator().evaluate("run_shell", "2 errors in 1.0s")
    assert e.metrics["errors"] == 2 and "测试未全过=blocker" in e.issues


def test_coding_hermes_runner_format():
    e = CodingEvaluator().evaluate("run_shell", "  ok  test_x\n\n3/9 passed")
    assert e.metrics == {"passed": 3, "total": 9, "failed": 6}
    assert "测试未全过=blocker" in e.issues and e.confidence == 1.0


def test_coding_bare_traceback_no_counts():
    e = CodingEvaluator().evaluate("run_shell", "Traceback (most recent call last):\n  AssertionError")
    assert "测试未全过=blocker" in e.issues
    assert e.confidence < 1.0   # 只有裸信号、没计数 → 置信度低
    assert any("Traceback" in s for s in e.signals)


def test_coding_needs_pytest_lowers_confidence():
    e = CodingEvaluator().evaluate("run_shell", "需装 pytest 才能真跑（pytest 风格测试）")
    assert any("pytest" in s for s in e.signals) and e.confidence <= 0.7


def test_coding_verify_marker_pass():
    e = CodingEvaluator().evaluate("edit_file", "🧪 受影响测试（FR-13.C）：全部通过")
    # 有 🧪 但无失败计数也无裸失败词 → 不判 blocker
    assert e.issues == []


# ---- SearchEvaluator：检索 ----------------------------------------------------

def test_search_grep_empty():
    e = SearchEvaluator().evaluate("grep_search", "无命中。")
    assert e.metrics["hits"] == 0 and "返回 0 条" in e.signals
    assert e.issues == []           # 空结果是事实、不是 blocker


def test_search_grep_hits_counted():
    e = SearchEvaluator().evaluate("grep_search", "a.py:1: foo\nb.py:9: foo\nc.py:3: foo")
    assert e.metrics["hits"] == 3 and "命中 3 条" in e.signals


def test_search_glob_empty():
    e = SearchEvaluator().evaluate("glob_search", "无匹配文件。")
    assert e.metrics["hits"] == 0


def test_search_code_not_found():
    e = SearchEvaluator().evaluate("search_code", "未找到与『鉴权』相关的定义")
    assert e.metrics["hits"] == 0 and "返回 0 条" in e.signals


# ---- ShellEvaluator：命令执行 -------------------------------------------------

def test_shell_exit_zero():
    e = ShellEvaluator().evaluate("run_powershell", "[exit code] 0\n[stdout]\nok")
    assert e.metrics["exit_code"] == 0 and e.issues == []
    assert "退出码 0" in e.signals


def test_shell_exit_nonzero_is_blocker():
    e = ShellEvaluator().evaluate("run_shell",
                                  "[exit code] 1\n[stdout]\n\n[stderr]\nbash: x: not found")
    assert e.metrics["exit_code"] == 1
    assert "退出码非零=失败" in e.issues and "有 stderr 输出" in e.signals


def test_shell_timeout():
    e = ShellEvaluator().evaluate("run_shell", "命令超时（>30s）。若是长运行进程…")
    assert "命令超时=未完成" in e.issues


def test_shell_background_no_verdict():
    e = ShellEvaluator().evaluate("run_shell", "已在后台启动进程 #3（pid 1234）：npm run dev")
    assert e.issues == [] and any("后台" in s for s in e.signals)


def test_shell_missing_executable():
    e = ShellEvaluator().evaluate("run_powershell", "找不到 powershell 可执行程序。")
    assert "环境缺 shell=无法执行" in e.issues


# ---- 调度器 evaluate()：路由 + Coding 优先于 Shell ---------------------------

def test_dispatch_routes_search():
    e = evaluate("grep_search", "无命中。")
    assert e is not None and e.metrics["hits"] == 0


def test_dispatch_coding_beats_shell_for_test_output():
    # shell 跑 pytest：内容是测试输出 → 应归 Coding（出 passed/total），而非 Shell（只出 exit_code）
    out = "[exit code] 1\n[stdout]\n==== 1 failed, 2 passed ====\n[stderr]\n"
    e = evaluate("run_shell", out)
    assert "total" in e.metrics and "测试未全过=blocker" in e.issues


def test_dispatch_plain_shell_uses_shell_evaluator():
    e = evaluate("run_shell", "[exit code] 0\n[stdout]\nhello")
    assert e.metrics.get("exit_code") == 0 and "passed" not in e.metrics


def test_dispatch_unknown_tool_returns_none():
    assert evaluate("read_file", "随便什么文件内容") is None


# ---- score()：只投影、单向、绝不回喂 -----------------------------------------

def test_score_blocker_is_low():
    bad = ShellEvaluator().evaluate("run_shell", "[exit code] 1\n[stderr]\nboom")
    good = ShellEvaluator().evaluate("run_shell", "[exit code] 0")
    assert score(bad) < score(good)
    assert 0.0 <= score(bad) <= 1.0 and 0.0 <= score(good) <= 1.0


def test_score_weighted_by_confidence():
    e = CodingEvaluator().evaluate("run_shell", "Traceback")   # confidence<1, 有 blocker
    assert score(e) <= 0.2


# ---- loop 接线：_emit_result 把 eval 附进 tool_result（纯观测，不改控制流）----

def test_emit_result_attaches_eval():
    from agentcore.agent.loop import AgentLoop
    captured = {}
    emit = lambda event, data: captured.update({event: data})
    call = type("C", (), {"id": "t1", "name": "run_shell", "input": {}})()
    AgentLoop._emit_result(emit, call, ("[exit code] 1\n[stderr]\nboom", False, []))
    ev = captured["tool_result"]
    assert "eval" in ev
    assert ev["eval"]["issues"] == ["退出码非零=失败"]
    assert 0.0 <= ev["eval"]["score"] <= 1.0


def test_emit_result_no_eval_for_unknown_tool():
    from agentcore.agent.loop import AgentLoop
    captured = {}
    emit = lambda event, data: captured.update({event: data})
    call = type("C", (), {"id": "t2", "name": "read_file", "input": {}})()
    AgentLoop._emit_result(emit, call, ("文件内容", True, []))
    assert "eval" not in captured["tool_result"]   # 无适配 Evaluator → 不附


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
