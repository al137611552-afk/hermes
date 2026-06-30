"""评测任务集（FR-11.0）：每个任务 = 夹具 setup + prompt + 程序化判分 check。

判分全自动且**可离线自检**（tests/test_eval.py 用金标准修复/合成事件验证判分器本身，
不调模型）。起步 4 任务来自 2026-06-12 的真实实测（DEVLOG「P10 收官实测」）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]

# ---- 夹具内容（常量化，判分时可比对"测试文件未被篡改"）------------------------

CALC_BUGGY = '''"""简单数值工具。"""


def moving_average(values, window):
    """滑动平均：返回每个完整窗口的平均值列表。"""
    if window <= 0:
        raise ValueError("window must be positive")
    out = []
    for i in range(len(values) - window):
        out.append(sum(values[i:i + window]) / window)
    return out


def normalize(values):
    """把数值线性缩放到 [0, 1]。"""
    lo, hi = min(values), max(values)
    return [(v - lo) / (hi - lo) for v in values]
'''

CALC_TEST = '''"""运行：python test_calc.py"""
from calc import moving_average, normalize

assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5], moving_average([1, 2, 3, 4], 2)
assert moving_average([5], 1) == [5.0]
assert normalize([3, 3, 3]) == [0.0, 0.0, 0.0]
assert normalize([0, 5, 10]) == [0.0, 0.5, 1.0]
print("ALL TESTS PASSED")
'''

TODO_PY = '''"""极简待办清单。"""


class TodoList:
    def __init__(self):
        self._items = []

    def add(self, text):
        text = (text or "").strip()
        if not text:
            raise ValueError("empty todo")
        self._items.append({"text": text, "done": False})

    def complete(self, index):
        self._items[index]["done"] = True

    def pending(self):
        return [it["text"] for it in self._items if not it["done"]]
'''

TODO_TEST = '''"""运行：python test_todo.py"""
from todo import TodoList

t = TodoList()
t.add("a"); t.add("b"); t.complete(0)
assert t.pending() == ["b"]
print("ALL TESTS PASSED")
'''


def _run(ws: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ws, capture_output=True, text=True, timeout=60)


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(ws, "git", *args)


def _pytests(ws: Path, script: str) -> bool:
    p = _run(ws, sys.executable, script)
    return p.returncode == 0 and "ALL TESTS PASSED" in (p.stdout or "")


# ---- 任务定义 -----------------------------------------------------------------

@dataclass
class Task:
    name: str
    title: str
    prompt: str
    setup: Callable[[Path], None]
    check: Callable[[Path, object], "tuple[bool, str]"]  # (workspace, EvalResult) -> (过?, 说明)


def _setup_bugfix(ws: Path) -> None:
    (ws / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    (ws / "test_calc.py").write_text(CALC_TEST, encoding="utf-8")


def _check_bugfix(ws: Path, result) -> "tuple[bool, str]":
    if (ws / "test_calc.py").read_text(encoding="utf-8") != CALC_TEST:
        return False, "测试文件被篡改（要求只改 calc.py）"
    if not _pytests(ws, "test_calc.py"):
        return False, "测试仍未通过"
    return True, "测试全绿且未改测试文件"


def _setup_feature_git(ws: Path) -> None:
    (ws / "todo.py").write_text(TODO_PY, encoding="utf-8")
    (ws / "test_todo.py").write_text(TODO_TEST, encoding="utf-8")
    (ws / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "eval@local"),
                 ("config", "user.name", "eval"), ("add", "-A"),
                 ("commit", "-q", "-m", "init: todo list")):
        _git(ws, *args)


def _check_feature_git(ws: Path, result) -> "tuple[bool, str]":
    if "def clear" not in (ws / "todo.py").read_text(encoding="utf-8"):
        return False, "todo.py 没有 clear() 实现"
    if not _pytests(ws, "test_todo.py"):
        return False, "测试未通过"
    if _git(ws, "rev-parse", "--verify", "-q", "feature/clear").returncode != 0:
        return False, "没有 feature/clear 分支"
    if (_git(ws, "rev-list", "--count", "main").stdout or "").strip() != "1":
        return False, "main 分支被动过（应只有初始提交）"
    if (_git(ws, "rev-list", "--count", "feature/clear").stdout or "").strip() == "1":
        return False, "feature/clear 上没有新提交"
    if (_git(ws, "status", "--porcelain").stdout or "").strip():
        return False, "工作区不干净（有未提交改动）"
    return True, "分支/提交/测试/树干净 全部达标"


def _setup_corpus(ws: Path) -> None:
    """以 hermes 自身内核源码为只读语料（约 40 文件）。"""
    dst = ws / "src" / "agentcore"
    shutil.copytree(ROOT / "src" / "agentcore", dst,
                    ignore=shutil.ignore_patterns("__pycache__"))


# 理解题判分：关键标识符命中率（答案必须落到具体实现上，背不出来）
COMPREHEND_KEYWORDS = ("compress", "_budget", "context.py", "keep_recent_turns", "tool_result")
COMPREHEND_PASS_AT = 3


def score_comprehension(answer: str) -> "tuple[int, list[str]]":
    hits = [k for k in COMPREHEND_KEYWORDS if k in (answer or "")]
    return len(hits), hits


def _check_comprehend(ws: Path, result) -> "tuple[bool, str]":
    n, hits = score_comprehension(getattr(result, "answer", ""))
    if n >= COMPREHEND_PASS_AT:
        return True, f"关键标识符命中 {n}/{len(COMPREHEND_KEYWORDS)}：{hits}"
    return False, f"命中不足（{n}/{len(COMPREHEND_KEYWORDS)} < {COMPREHEND_PASS_AT}）：{hits}"


def check_parallel_events(events: list) -> "tuple[bool, str]":
    """并行判定（纯函数）：≥2 个子任务、全部成功，且第 2 个 start 早于第 1 个 done。"""
    starts = [i for i, (e, _) in enumerate(events) if e == "subagent_start"]
    dones = [(i, d) for i, (e, d) in enumerate(events) if e == "subagent_done"]
    if len(starts) < 2:
        return False, f"子任务数不足（{len(starts)} < 2）"
    if len(dones) < len(starts) or not all(d.get("ok") for _, d in dones):
        return False, "有子任务未完成或失败"
    if starts[1] > dones[0][0]:
        return False, "未并行：第 2 个子任务在第 1 个完成后才启动"
    return True, f"{len(starts)} 个子任务并行且全部成功"


def _check_parallel(ws: Path, result) -> "tuple[bool, str]":
    ok, why = check_parallel_events(getattr(result, "events", []))
    if ok and not (getattr(result, "answer", "") or "").strip():
        return False, "并行成立但没有汇总输出"
    return ok, why


def _setup_noop(ws: Path) -> None:
    pass


def _check_delegate_implicit(ws: Path, result) -> "tuple[bool, str]":
    """隐式调研（不显式提示"用子任务并行"）应自发并行委派——防"精简 prompt 致委派退化"再现。"""
    n = getattr(result, "subagents", 0)
    return (n >= 2, f"自发委派 {n} 个子任务（目标 ≥2；prompt 未显式要求委派）")


def _check_quick_query(ws: Path, result) -> "tuple[bool, str]":
    """简单事实咨询应快：不委派、不堆步数（需联网；网络失败判负，非 hermes 缺陷）。"""
    if getattr(result, "error", ""):
        return False, f"运行出错（可能联网失败）：{str(result.error)[:50]}"
    n, tc = getattr(result, "subagents", 0), getattr(result, "tool_calls", 0)
    return (n == 0 and tc <= 6, f"委派 {n}、工具 {tc} 次（目标：不委派 + ≤6 步）")


TASKS: dict[str, Task] = {
    "bugfix": Task(
        "bugfix", "修复隐藏 bug + 测试全绿",
        "这个项目的测试挂了。请运行测试脚本 test_calc.py（用合适的 python 命令）看失败原因，"
        "修复 calc.py 里的问题（不要改测试文件），然后重新跑测试确认全部通过。",
        _setup_bugfix, _check_bugfix,
    ),
    "feature_git": Task(
        "feature_git", "开分支加功能 + 补测试 + 提交",
        "给 TodoList 加一个 clear() 方法（清空所有待办并返回清掉的条数），在 test_todo.py 里"
        "补对应断言。请开一个 feature/clear 分支做，测试通过后提交（Conventional Commits）。",
        _setup_feature_git, _check_feature_git,
    ),
    "comprehend": Task(
        "comprehend", "代码库理解（给出文件:行号）",
        "这个项目里'上下文压缩'机制是怎么实现的？我要：①从哪里触发；②具体裁剪策略（分几层）；"
        "③涉及哪些文件和函数（给出 文件:行号）。不要修改任何文件。",
        _setup_corpus, _check_comprehend,
    ),
    "parallel": Task(
        "parallel", "并行委派调研 + 汇总（显式要求）",
        "用两个 researcher 子任务并行调研：A=src/agentcore/tools 的工具体系（注册与限权机制）；"
        "B=src/agentcore/providers 的模型适配层（统一接口与两个实现的差异）。两个互不依赖，"
        "请同一轮一起委派，最后给我一份两者如何协作的对比汇总。",
        _setup_corpus, _check_parallel,
    ),
    # 隐式委派：不提"用子任务"，只"逐一分析很多单元"——防委派退化（精简 prompt 曾在此翻车）
    "delegate_implicit": Task(
        "delegate_implicit", "隐式调研 → 应自发并行委派",
        "逐一分析 src/agentcore/tools 目录下的每一个工具文件，对每个工具列出：名称、是否危险操作、"
        "主要参数、一句话用途，最后汇总成一张表格给我。",
        _setup_corpus, _check_delegate_implicit,
    ),
    # 简单咨询：应快速答、不委派、不堆步数（需联网）
    "quick_query": Task(
        "quick_query", "简单事实咨询 → 应快、不委派",
        "Python 目前最新的稳定版本号是多少？简单告诉我就行。",
        _setup_noop, _check_quick_query,
    ),
}
