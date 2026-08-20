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


# ---- CodingEvaluator：退出码兜底（2026-08-19 补，V1 揪出的缺口）----------------

def test_coding_nonzero_exit_without_counts_is_not_run():
    """`pytest 不存在的文件` → exit 4、无任何用例计数：测试**根本没跑起来**，必须是 blocker。

    原先 CodingEvaluator 接管了（输出含 "pytest"）却解析不出计数，把退出码一起吞掉判成"无 issues"，
    于是"测试命令本身写错了"这一整类失败对评估内核完全隐形。
    """
    out = ("[exit code] 4\n[stdout]\n=== test session starts ===\n"
           "platform linux -- Python 3.12.3, pytest-9.1.0\n"
           "ERROR: file or directory not found: nonexistent_xyz.py\n")
    e = CodingEvaluator().evaluate("run_bash", out)
    assert e.issues == ["测试未跑成=blocker"], e.issues
    assert e.metrics["exit_code"] == 4.0
    assert e.confidence == 1.0, "退出码是硬事实，不该还是 0.6 的启发式猜测"


def test_coding_collected_zero_items_is_not_run():
    """pytest 收集到 0 个用例（exit 5）：`0 passed` 不算"有计数"，同样是没跑成。"""
    out = "[exit code] 5\n[stdout]\ncollected 0 items\n\n0 passed in 0.01s\n"
    e = CodingEvaluator().evaluate("run_bash", out)
    assert e.issues == ["测试未跑成=blocker"], (e.issues, e.metrics)


def test_coding_all_passed_but_nonzero_exit_is_blocker():
    """用例全过、命令却非零退出（收集/插件/收尾阶段出错）——也是真问题，不能报"全过"。"""
    e = CodingEvaluator().evaluate("run_bash", "[exit code] 1\n[stdout]\n==== 3 passed in 0.1s ====")
    assert e.issues == ["退出码非零=失败"], e.issues
    assert e.metrics["passed"] == 3 and e.metrics["exit_code"] == 1.0


def test_coding_count_regex_does_not_match_across_lines():
    """**幻影计数回归门**：`pytest-9.1.0\nERROR:` 曾被 `(\\d+)\\s+errors?` 跨行读成"0 errors"，
    凭空造出 total=0，于是"没跑成"被判成"用例全过"。计数正则必须限定同一行。"""
    out = "版本 pytest-9.1.0\nERROR: file not found"
    e = CodingEvaluator().evaluate("run_bash", out)
    assert "total" not in e.metrics, e.metrics
    assert "errors" not in e.metrics, e.metrics


def test_coding_records_exit_code_even_with_counts():
    """退出码无论有没有计数都记为事实——它是 shell 包装层给的硬信息。"""
    e = CodingEvaluator().evaluate("run_bash", "[exit code] 0\n[stdout]\n==== 2 passed ====")
    assert e.metrics["exit_code"] == 0.0 and e.metrics["passed"] == 2
    assert e.issues == []


def test_coding_without_exit_code_line_unchanged():
    """没有 `[exit code]` 行的输入（verify.py 定向校验）行为不变——只加兜底、不改既有判定。"""
    e = CodingEvaluator().evaluate("edit_file", "🧪 受影响测试（FR-13.C）：全部通过")
    assert e.issues == [] and "exit_code" not in e.metrics


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



def test_coding_evaluator_ignores_observation_tools():
    """**读文件/检索读到失败字样 ≠ 这次动作失败了**（ADR 0027 决策 11，块 V4a）。

    CodingEvaluator 按输出特征词认领（测试结果会搭在各种工具输出里），但观察类工具例外——
    否则读一个含 assert 的测试文件就被判成 blocker「测试未全过」，进而污染失败语料、
    让 deadend_hint 在纯只读任务里误报（块 V4 收割语料时实测照出）。
    """
    from agentcore.agent.evaluators import evaluate
    from agentcore.agent.evaluators.base import OBSERVATION_TOOLS

    body = "1\tassert add(1, 2) == 4, \"1+2 应当等于 4\"\n2\tAssertionError"
    for name in ("read_file", "grep_search", "list_dir", "code_outline", "git_diff"):
        assert name in OBSERVATION_TOOLS, name
        assert CodingEvaluator().applies(name, body) is False, name
    # 检索类仍走 SearchEvaluator（命中数事实照常产出），只是不再被 Coding 抢走
    ev = evaluate("grep_search", body)
    assert ev is None or not ev.issues, ev


def test_coding_evaluator_still_claims_execution_output():
    """别修过头：测试结果搭在 shell / edit_file（受影响测试）输出里，仍必须被接管。"""
    assert CodingEvaluator().applies("run_bash", "1 failed, 2 passed in 0.3s") is True
    assert CodingEvaluator().applies("edit_file", "🧪 受影响测试（FR-13.C）：1 failed") is True


def test_reading_a_failing_test_file_is_not_a_failure():
    """端到端口径：整条链路（evaluate → classify）对读文件必须一声不响。"""
    from agentcore.agent.loop import AgentLoop

    body = "1\tdef test_x():\n2\t    assert 1 == 2\n3\tAssertionError: boom"
    _ev, classes = AgentLoop._assess("read_file", body, True, {"path": "test_x.py"})
    assert classes == [], classes
    _ev, classes = AgentLoop._assess("run_bash", body, True, None)
    assert classes, "执行类的同样文本仍必须判成失败，别修过头"


def test_shell_echoed_exit_code_is_not_swallowed():
    """`cmd 2>&1; echo "exit=$?"` —— 整条命令的退出码变成 echo 的 0，真实失败被掩盖。

    这类写法极常见（真跑里连撞三次：找不到命令、语法错、私有包缺失），而它让
    "命令根本没跑起来"这一整类失败对评估内核完全隐形：不进 issues、不分类、不进失败语料。
    与块 V1a 修的"CodingEvaluator 吞退出码"同一家族——**退出码是硬事实，丢了就什么都判不了**。
    """
    from agentcore.agent.evaluators import evaluate
    from agentcore.agent.taxonomy import ErrorClass, classify

    out = ("[exit code] 0\n[stdout]\n"
           "bash: line 1: acme-build: command not found\nexit=127\n")
    ev = evaluate("run_bash", out, {"command": 'acme-build --release 2>&1; echo "exit=$?"'})
    assert ev.issues and "127" in ev.issues[0], ev.issues
    assert ErrorClass.NOT_FOUND in classify(ev, out)


def test_shell_echoed_exit_code_zero_is_still_success():
    from agentcore.agent.evaluators import evaluate

    out = "[exit code] 0\n[stdout]\nhello\nexit=0\n"
    assert not evaluate("run_bash", out, {"command": 'echo hello; echo "exit=$?"'}).issues


def test_shell_does_not_invent_failures_from_log_text():
    """判据刻意收得很窄（要求命令里确实写了 `$?`）：宽一点就会把 `cat error.log`、
    grep 到 Error 的正常输出全判成失败——那正是块 V4a 刚清理掉的语料污染，不能反手又造一批。"""
    from agentcore.agent.evaluators import evaluate

    out = "[exit code] 0\n[stdout]\n2026-01-01 Error: something happened\nexit=1 是日志正文\n"
    assert not evaluate("run_bash", out, {"command": "cat error.log"}).issues
    # 连 `$?` 都没写的串联命令：**已知不覆盖**，这里钉住这条边界，免得日后误以为已经处理了
    assert not evaluate("run_bash", "[exit code] 0\n[stdout]\nboom: command not found\n",
                        {"command": "boom; true"}).issues

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
