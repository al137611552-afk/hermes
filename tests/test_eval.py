"""FR-11.0 评测套件离线自检：夹具可复现失败、金标准修复能过判分、判分纯函数正确。

不调模型、不碰网络——只验证 scripts/eval 的夹具与判分器本身。
运行：python tests/test_eval.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from harness import EvalResult  # noqa: E402
from tasks import (  # noqa: E402
    COMPREHEND_PASS_AT, TASKS, check_parallel_events, score_comprehension,
)

CALC_FIXED = '''"""简单数值工具。"""


def moving_average(values, window):
    if window <= 0:
        raise ValueError("window must be positive")
    out = []
    for i in range(len(values) - window + 1):
        out.append(sum(values[i:i + window]) / window)
    return out


def normalize(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]
'''


def _git(ws: Path, *args: str):
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True)


def test_bugfix_fixture_and_check(tmp: Path):
    t = TASKS["bugfix"]
    t.setup(tmp)
    ok, why = t.check(tmp, EvalResult())
    assert not ok and "未通过" in why                 # 夹具初始必挂
    (tmp / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
    ok, _ = t.check(tmp, EvalResult())
    assert ok                                         # 金标准修复能过
    (tmp / "test_calc.py").write_text("print('ALL TESTS PASSED')", encoding="utf-8")
    ok, why = t.check(tmp, EvalResult())
    assert not ok and "篡改" in why                   # 改测试文件＝作弊，必挂


def test_feature_git_fixture_and_check(tmp: Path):
    t = TASKS["feature_git"]
    t.setup(tmp)
    ok, why = t.check(tmp, EvalResult())
    assert not ok and "clear" in why                  # 初始没有 clear
    # 金标准操作：开分支 -> 实现 -> 补测试 -> 提交
    _git(tmp, "switch", "-q", "-c", "feature/clear")
    todo = (tmp / "todo.py").read_text(encoding="utf-8")
    todo += ("\n    def clear(self):\n        n = len(self._items)\n"
             "        self._items = []\n        return n\n")
    (tmp / "todo.py").write_text(todo, encoding="utf-8")
    test = (tmp / "test_todo.py").read_text(encoding="utf-8").replace(
        'print("ALL TESTS PASSED")',
        'assert t.clear() == 2 and t.pending() == []\nprint("ALL TESTS PASSED")')
    (tmp / "test_todo.py").write_text(test, encoding="utf-8")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-q", "-m", "feat: add clear")
    ok, why = t.check(tmp, EvalResult())
    assert ok, why
    # main 被动过（多了提交）应挂——空提交动 main 后切回分支再判
    _git(tmp, "switch", "-q", "main")
    _git(tmp, "commit", "-q", "--allow-empty", "-m", "dirty main")
    _git(tmp, "switch", "-q", "feature/clear")
    ok, why = t.check(tmp, EvalResult())
    assert not ok and "main" in why


def test_comprehend_scoring():
    good = "触发在 conversation.py 的 _budget，调 context.py 的 compress；keep_recent_turns 保底，旧 tool_result 先瘦身。"
    n, hits = score_comprehension(good)
    assert n >= COMPREHEND_PASS_AT and "compress" in hits
    n2, _ = score_comprehension("这个项目有上下文压缩功能，会压缩历史。")
    assert n2 < COMPREHEND_PASS_AT                    # 空话不得分
    t = TASKS["comprehend"]
    ok, _ = t.check(Path("."), EvalResult(answer=good))
    assert ok
    ok2, _ = t.check(Path("."), EvalResult(answer="不知道"))
    assert not ok2


def test_parallel_event_judge():
    s1 = ("subagent_start", {"id": 1})
    s2 = ("subagent_start", {"id": 2})
    d1 = ("subagent_done", {"id": 1, "ok": True})
    d2 = ("subagent_done", {"id": 2, "ok": True})
    ok, why = check_parallel_events([s1, s2, d2, d1])      # 真并行（#2 先完成）
    assert ok, why
    ok, why = check_parallel_events([s1, d1, s2, d2])      # 串行：第2个在第1个完成后才启动
    assert not ok and "未并行" in why
    ok, _ = check_parallel_events([s1, d1])                # 只有一个子任务
    assert not ok
    ok, _ = check_parallel_events([s1, s2, ("subagent_done", {"id": 2, "ok": False}), d1])
    assert not ok                                          # 有失败
    # 任务级 check：并行成立但无汇总输出 -> 挂
    t = TASKS["parallel"]
    ok, why = t.check(Path("."), EvalResult(events=[s1, s2, d2, d1], answer=""))
    assert not ok and "汇总" in why
    ok, _ = t.check(Path("."), EvalResult(events=[s1, s2, d2, d1], answer="对比结论…"))
    assert ok


def test_corpus_setup(tmp: Path):
    TASKS["comprehend"].setup(tmp)
    assert (tmp / "src" / "agentcore" / "context.py").is_file()
    assert not list((tmp / "src").rglob("__pycache__"))


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
