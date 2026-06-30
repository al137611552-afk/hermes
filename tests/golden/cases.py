"""块F Golden Dataset —— 决策内核的"行为基线"语料（见 docs/ROADMAP.md 块F / ADR 0014）。

每条 = 一个决策点的 `输入 → 期望输出`，覆盖 A/B/C/D/E 的确定性映射：
- "need"     : crazy verdict → Need（块A verdict_to_need）
- "evaluate" : 工具输出 → Evaluation 事实（块B：issues 有无 + 关键 metric）
- "classify" : 失败文本 → 主错误分类（块C classify_text，None=不匹配）
- "retry"    : (分类, 已试次数) → 是否重试 + 退避（块D decide_retry）
- "deadend"  : 同一条路连撞 N 次 → 第几次起提示换思路（块E detect_repeated_failure）

这是 G（Learning）改 Need→Decision 映射前必须先过的门：任一改动让某条偏离期望即回归。
新增能力时**追加**语料、不改既有期望（除非确是有意的行为变更，需同步说明）。
"""

CASES = [
    # ---- 块A：crazy verdict → Need -----------------------------------------
    {"id": "need-done", "kind": "need", "verdict": "done", "expect": "goal_satisfied"},
    {"id": "need-phase", "kind": "need", "verdict": "phase_done", "expect": "goal_satisfied"},
    {"id": "need-continue", "kind": "need", "verdict": "continue", "expect": "continue"},
    {"id": "need-user", "kind": "need", "verdict": "need_user", "expect": "need_user_input"},
    {"id": "need-unknown-falls-to-continue", "kind": "need", "verdict": "???", "expect": "continue"},

    # ---- 块B：工具输出 → Evaluation 事实 -----------------------------------
    {"id": "eval-pytest-all-pass", "kind": "evaluate", "tool": "run_powershell",
     "output": "==== 3 passed in 0.1s ====",
     "expect": {"has_issues": False}},
    {"id": "eval-pytest-some-fail", "kind": "evaluate", "tool": "run_powershell",
     "output": "==== 1 failed, 2 passed ====\nAssertionError",
     "expect": {"has_issues": True}},
    {"id": "eval-shell-exit-nonzero", "kind": "evaluate", "tool": "run_powershell",
     "output": "[exit code] 1\n[stderr]\nboom",
     "expect": {"has_issues": True, "metric": ["exit_code", 1.0]}},
    {"id": "eval-shell-exit-zero", "kind": "evaluate", "tool": "run_powershell",
     "output": "[exit code] 0\n[stdout]\nok",
     "expect": {"has_issues": False, "metric": ["exit_code", 0.0]}},
    {"id": "eval-search-empty-not-failure", "kind": "evaluate", "tool": "grep_search",
     "output": "无命中。",
     "expect": {"has_issues": False}},

    # ---- 块C：失败文本 → 主错误分类 ----------------------------------------
    {"id": "cls-transient-refused", "kind": "classify",
     "text": "curl: (7) Connection refused", "expect": "transient_io"},
    {"id": "cls-transient-timeout", "kind": "classify",
     "text": "命令超时（>30s）", "expect": "transient_io"},
    {"id": "cls-notfound", "kind": "classify",
     "text": "cat: /no/such: No such file or directory", "expect": "not_found"},
    {"id": "cls-auth", "kind": "classify",
     "text": "401 Unauthorized: invalid token", "expect": "auth"},
    {"id": "cls-logic-assert", "kind": "classify",
     "text": "AssertionError: 1 != 2", "expect": "logic"},
    {"id": "cls-no-match-returns-none", "kind": "classify",
     "text": "全部正常，一切顺利", "expect": None},

    # ---- 块D：(分类, 已试次数) → 重试决策 ----------------------------------
    {"id": "retry-transient-first", "kind": "retry",
     "classes": ["transient_io"], "attempts": 1, "max": 2, "base": 0.5,
     "expect": {"retry": True, "delay": 0.5}},
    {"id": "retry-transient-second", "kind": "retry",
     "classes": ["transient_io"], "attempts": 2, "max": 3, "base": 0.5,
     "expect": {"retry": True, "delay": 1.0}},
    {"id": "retry-transient-over-max", "kind": "retry",
     "classes": ["transient_io"], "attempts": 3, "max": 2, "base": 0.5,
     "expect": {"retry": False}},
    {"id": "retry-logic-never", "kind": "retry",
     "classes": ["logic"], "attempts": 1, "max": 2, "base": 0.5,
     "expect": {"retry": False}},
    {"id": "retry-notfound-never", "kind": "retry",
     "classes": ["not_found"], "attempts": 1, "max": 2, "base": 0.5,
     "expect": {"retry": False}},

    # ---- 块E：同一条路连撞 → 第几次起提示换思路 ----------------------------
    {"id": "deadend-nudges-on-second", "kind": "deadend", "tool": "run_powershell",
     "params": {"command": "pytest broken"},
     "output": "==== 1 failed, 2 passed ====\nAssertionError",
     "threshold": 2, "repeat": 3,
     "expect": {"first_nudge_at": 2}},      # 第1次不提示、第2次提示、之后每指纹不重复
    {"id": "deadend-transient-never", "kind": "deadend", "tool": "run_powershell",
     "params": {"command": "curl x"},
     "output": "[exit code] 1\n[stderr]\ncurl: (7) Connection refused",
     "threshold": 2, "repeat": 3,
     "expect": {"first_nudge_at": None}},   # 瞬时失败永不算死路
]
