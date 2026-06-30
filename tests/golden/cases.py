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

    # ---- 块G：历史失败 → 候选策略生成的边界（系统性才升级，可解释带证据）----
    {"id": "learn-systemic-proposes", "kind": "learn",
     "rows": [{"fp": "p1", "class": "not_found", "detail": "no a"},
              {"fp": "p2", "class": "not_found", "detail": "no b"},
              {"fp": "p3", "class": "not_found", "detail": "no c"}],
     "expect": {"classes": ["not_found"]}},   # 跨3路3次 → 升级为候选
    {"id": "learn-single-path-no-propose", "kind": "learn",
     "rows": [{"fp": "p1", "class": "not_found"},
              {"fp": "p1", "class": "not_found"},
              {"fp": "p1", "class": "not_found"}],
     "expect": {"classes": []}},              # 单条路偶发 → 不升级（块D/E 已管）
    {"id": "learn-transient-never-proposes", "kind": "learn",
     "rows": [{"fp": "p1", "class": "transient_io"},
              {"fp": "p2", "class": "transient_io"},
              {"fp": "p3", "class": "transient_io"}],
     "expect": {"classes": []}},              # 瞬时 IO 永不成策略

    # ---- 块H1：搜索/调研结果质量——预算约束满足（小红书 618 睡衣 500 元验收）----
    {"id": "research-budget-miss", "kind": "evaluate", "tool": "web_search",
     "params": {"query": "在小红书搜索618推荐的女士睡衣，500元以内"},
     "output": ("[搜索结果·bing] 小红书 618 女士睡衣\n"
                "1. 真丝睡衣套装\n   http://a\n   ¥899 618大促\n"
                "2. 设计师款睡裙\n   http://b\n   1280元\n"
                "3. 进口长袖睡衣\n   http://c\n   ￥699"),
     "expect": {"has_issues": True, "metric": ["within_budget", 0.0]}},
    {"id": "research-budget-ok", "kind": "evaluate", "tool": "web_search",
     "params": {"query": "女士睡衣 500元以内"},
     "output": ("[搜索结果·bing] 女士睡衣\n"
                "1. 纯棉睡衣\n   http://a\n   ¥199\n"
                "2. 真丝款\n   http://b\n   899元\n"
                "3. 冰丝睡裙\n   http://c\n   ¥359"),
     "expect": {"has_issues": False, "metric": ["within_budget", 2.0]}},
]
