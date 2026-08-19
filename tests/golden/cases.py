"""块F Golden Dataset —— 决策内核的"行为基线"语料（见 docs/ROADMAP.md 块F / ADR 0014）。

每条 = 一个决策点的 `输入 → 期望输出`，覆盖 A/B/C/D/E 的确定性映射：
- "need"     : crazy verdict → Need（块A verdict_to_need）
- "evaluate" : 工具输出 → Evaluation 事实（块B：issues 有无 + 关键 metric）
- "classify" : 失败文本 → 主错误分类（块C classify_text，None=不匹配）
- "retry"    : (分类, 已试次数) → 是否重试 + 退避（块D decide_retry）
- "deadend"  : 同一条路连撞 N 次 → 第几次起提示换思路（块E detect_repeated_failure）
- "research_judge": web_search 结果经模型裁判 → 三态（块H3a/H3c detect_offtarget_research：
                    不对题重搜 offtarget / 部分污染萃取 salvage / 对题静默 none，假裁判注入）
- "grounding" : 答案接地/时效闸（块H3c detect_ungrounded_answer：时效敏感+搜过+无引用无声明→催）
- "switch"    : 换源策略阶梯（块H switch_strategy_nudge：NO_PROGRESS 逐级 site→browser→ask_user）
- "novelty"   : 搜索结果 → 域名集（块H extract_domains：确定性去重/归一，Novelty 信号源）
- "fingerprint": 两次工具调用 → 同/异指纹（ADR 0027 V0：死路指纹的路径归一口径。
                 语料写**等价关系**不写哈希值——哈希不可读，且有意变更时全表重算无从审阅）
- "consensus_gate": 开工 gate（ADR 0019 gate_status：未决阻塞==0 且签字→open，否则 locked；门理由禁含百分比）
- "review_stop"   : 评审停止条件（ADR 0019 should_stop：max_rounds / no_new_blocking / wording_only / continue）

这是 G（Learning）改 Need→Decision 映射前必须先过的门：任一改动让某条偏离期望即回归。
新增能力时**追加**语料、不改既有期望（除非确是有意的行为变更，需同步说明）。
"""

# 块 V0 语料用的固定假工作区（不需真实存在；写死才确定性可复现）。
_WS = "/ws/proj"

CASES = [
    # ---- 块V0：死路指纹的路径归一（ADR 0027 决策 2）------------------------
    # 同一个文件的绝对写法与相对写法必须同指纹——模型两种都会用。
    {"id": "fp-abs-equals-rel", "kind": "fingerprint",
     "a": {"tool": "read_file", "params": {"path": _WS + "/calc.py"}, "workspace": _WS},
     "b": {"tool": "read_file", "params": {"path": "calc.py"}, "workspace": _WS},
     "expect": "same"},
    # 同一相对路径在两个不同工作区 → 同指纹（这是跨评测跑聚合的前提）。
    {"id": "fp-cross-workspace", "kind": "fingerprint",
     "a": {"tool": "read_file", "params": {"path": "/tmp/run1/calc.py"}, "workspace": "/tmp/run1"},
     "b": {"tool": "read_file", "params": {"path": "/tmp/run2/calc.py"}, "workspace": "/tmp/run2"},
     "expect": "same"},
    # command 里嵌着的绝对路径同样要折（路径在命令行中间，切不出来）。
    {"id": "fp-command-embedded-path", "kind": "fingerprint",
     "a": {"tool": "run_bash", "params": {"command": "pytest /tmp/run1/test_calc.py -q"},
           "workspace": "/tmp/run1"},
     "b": {"tool": "run_bash", "params": {"command": "pytest /tmp/run2/test_calc.py -q"},
           "workspace": "/tmp/run2"},
     "expect": "same"},
    # 反斜杠/正斜杠是同一个文件的两种写法。
    {"id": "fp-separator-normalized", "kind": "fingerprint",
     "a": {"tool": "read_file", "params": {"path": "src" + chr(92) + "a.py"}},
     "b": {"tool": "read_file", "params": {"path": "src/a.py"}},
     "expect": "same"},
    # **防归一过头**：工作区内不同文件仍须分得开，否则所有路合成一条、死路记忆失效。
    {"id": "fp-different-files-still-differ", "kind": "fingerprint",
     "a": {"tool": "read_file", "params": {"path": _WS + "/calc.py"}, "workspace": _WS},
     "b": {"tool": "read_file", "params": {"path": _WS + "/other.py"}, "workspace": _WS},
     "expect": "diff"},
    # 不传 workspace 时不折——存量调用方与旧库口径不变。
    {"id": "fp-no-workspace-not-folded", "kind": "fingerprint",
     "a": {"tool": "read_file", "params": {"path": "/tmp/run1/calc.py"}},
     "b": {"tool": "read_file", "params": {"path": "/tmp/run2/calc.py"}},
     "expect": "diff"},

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
    # 退出码兜底（2026-08-19 新增，V1 揪出的缺口）：**新增语料，既有期望一条未改**。
    # 测试没跑起来 / 收集到 0 个用例 / 全过但收尾非零——三种都必须判失败。
    {"id": "eval-pytest-not-run-exit4", "kind": "evaluate", "tool": "run_bash",
     "output": ("[exit code] 4\n[stdout]\n=== test session starts ===\n"
                "platform linux -- Python 3.12.3, pytest-9.1.0\n"
                "ERROR: file or directory not found: nonexistent_xyz.py\n"),
     "expect": {"has_issues": True, "metric": ["exit_code", 4.0]}},
    {"id": "eval-pytest-collected-zero", "kind": "evaluate", "tool": "run_bash",
     "output": "[exit code] 5\n[stdout]\ncollected 0 items\n\n0 passed in 0.01s\n",
     "expect": {"has_issues": True}},
    {"id": "eval-pytest-passed-but-nonzero-exit", "kind": "evaluate", "tool": "run_bash",
     "output": "[exit code] 1\n[stdout]\n==== 3 passed in 0.1s ====",
     "expect": {"has_issues": True, "metric": ["passed", 3]}},
    # 幻影计数回归门：跨行匹配曾把 `9.1.0\nERROR` 读成"0 errors"。total 必须**不存在**。
    {"id": "eval-no-phantom-count-across-lines", "kind": "evaluate", "tool": "run_bash",
     "output": "版本 pytest-9.1.0\nERROR: file not found",
     "expect": {"has_issues": False, "metric": ["total", None]}},
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

    # ---- 块 V4a：观察类工具的输出里有失败字样 ≠ 一次失败（ADR 0027 决策 11）----
    # 补盲区，非改行为：既有 evaluate 语料用的都是执行类工具（run_bash/run_powershell），
    # 从没覆盖"读到一个失败的测试文件"这条路，于是它一直被判成 blocker 并写进失败语料。
    {"id": "eval-read-file-with-assertions-not-a-failure", "kind": "evaluate", "tool": "read_file",
     "params": {"path": "run_tests.py"},
     "output": ("1\tfrom calc import add\n"
                "2\tassert add(1, 2) == 4, \"1+2 应当等于 4\"\n"
                "3\tAssertionError\n"),
     "expect": {"has_issues": False}},
    {"id": "eval-grep-hit-with-traceback-not-a-failure", "kind": "evaluate", "tool": "grep_search",
     "params": {"pattern": "AssertionError"},
     "output": ("tests/test_x.py:12:    AssertionError: boom\n"
                "tests/test_y.py:44:Traceback (most recent call last):\n"),
     "expect": {"has_issues": False}},

    # ---- 块H1：搜索/调研结果质量——预算约束满足（小红书 618 睡衣 500 元验收）----
    {"id": "research-budget-miss", "kind": "evaluate", "tool": "web_search",
     "params": {"query": "在小红书搜索618推荐的女士睡衣，500元以内"},
     "output": ("[搜索结果·bing] 小红书 618 女士睡衣\n"
                "1. 真丝睡衣套装\n   http://a\n   ¥899 618大促\n"
                "2. 设计师款睡裙\n   http://b\n   1280元\n"
                "3. 进口长袖睡衣\n   http://c\n   ￥699"),
     "expect": {"has_issues": True, "metric": ["within_budget", 0.0]}},
    # 表头**两行**（v3.54 块3「搜完顺带读正文」之后的真实形态）：`hits` 必须仍是结果条数。
    # 补盲区，非改行为——原有两条语料建于块3 之前、都是单行表头，故一直没覆盖到（V2 批 2 照出）。
    {"id": "research-budget-miss-multiline-header", "kind": "evaluate", "tool": "web_search",
     "params": {"query": "机械键盘 500元以内"},
     "output": ("[搜索结果·bing+duckduckgo] 机械键盘 500元以内\n"
                "[已读正文] 前 2 条已抓取正文并按查询摘录（下面 ↳ 的部分）\n"
                "1. A 键盘\n   http://a\n   ¥899\n   ↳ 页面标价 ¥899\n"
                "2. B 键盘\n   http://b\n   ¥1299"),
     "expect": {"has_issues": True, "metric": ["hits", 2.0]}},
    {"id": "research-budget-ok", "kind": "evaluate", "tool": "web_search",
     "params": {"query": "女士睡衣 500元以内"},
     "output": ("[搜索结果·bing] 女士睡衣\n"
                "1. 纯棉睡衣\n   http://a\n   ¥199\n"
                "2. 真丝款\n   http://b\n   899元\n"
                "3. 冰丝睡裙\n   http://c\n   ¥359"),
     "expect": {"has_issues": False, "metric": ["within_budget", 2.0]}},

    # ---- 块H3a/H3c：模型裁判三态决策（假裁判注入固定 verdict，锁分类）----------
    {"id": "judge-offtarget-research", "kind": "research_judge",
     "query": "618夏季女士睡衣", "goal": "搜618夏季女士睡衣",
     "output": "1. 真丝厚睡衣套装\n   http://a\n   ¥299 秋冬加厚",
     "verdict": '{"on_target": false, "off": ["真丝厚款：秋冬不符夏季"]}',
     "expect": "offtarget"},                  # 一条都不相关 → 换词/换源重搜
    {"id": "judge-salvage-partial", "kind": "research_judge",
     "query": "618夏季女士睡衣", "goal": "搜618夏季女士睡衣",
     "output": "1. 真丝厚款\n2. 冰丝短袖 ¥199",
     "verdict": '{"on_target": false, "use": ["冰丝短袖睡衣 ¥199"], "off": ["真丝厚款：秋冬不符"]}',
     "expect": "salvage"},                    # 部分污染 → 萃取有效项、不整批丢
    {"id": "judge-ontarget-silent", "kind": "research_judge",
     "query": "夏季睡衣", "goal": "夏季睡衣",
     "output": "1. 冰丝短袖睡衣 ¥199",
     "verdict": '{"on_target": true}',
     "expect": "none"},                       # 对题 → 静默不打扰

    # ---- 块H3c：接地/时效闸（保守触发：时效敏感 + 做过搜索 + 无引用无声明）------
    {"id": "ground-fresh-ungrounded-fires", "kind": "grounding",
     "goal": "查2026最新显卡价格", "answer": "大概三千到五千元。", "did_research": True,
     "expect": True},                         # 凭记忆答时效问题 → 催据来源/声明
    {"id": "ground-cited-passes", "kind": "grounding",
     "goal": "查2026最新显卡价格", "answer": "据 http://jd.com/x，某卡 ¥4999。",
     "did_research": True, "expect": False},  # 已引用来源 → 接地放行
    {"id": "ground-disclaimed-passes", "kind": "grounding",
     "goal": "查2026最新显卡价格", "answer": "以下基于训练知识、可能已过时，建议以实时为准：约三千。",
     "did_research": True, "expect": False},  # 已声明过时 → 诚实放行
    {"id": "ground-nonfresh-no-kill", "kind": "grounding",
     "goal": "光合作用原理", "answer": "植物把光能转化为化学能。", "did_research": True,
     "expect": False},                        # 非时效问题 → 凭常识答不误杀
    {"id": "ground-no-research-no-guard", "kind": "grounding",
     "goal": "查2026最新显卡价格", "answer": "大概三千到五千元。", "did_research": False,
     "expect": False},                        # 没搜过 → 不触发（闸只管"搜了却凭记忆"）

    # ---- 块H 换源策略阶梯：NO_PROGRESS 逐级升 site→browser→ask_user，越界停 ----
    {"id": "switch-step0-site", "kind": "switch", "step": 0, "expect": "site_filter"},
    {"id": "switch-step1-browser", "kind": "switch", "step": 1, "expect": "browser"},
    {"id": "switch-step2-ask-user", "kind": "switch", "step": 2, "expect": "ask_user"},
    {"id": "switch-step3-exhausted", "kind": "switch", "step": 3, "expect": "none"},

    # ---- 块H Novelty：域名抽取去重/归一（确定性事实，无模型无分数）-------------
    {"id": "novelty-dedup-strip-www", "kind": "novelty",
     "text": "见 http://www.JD.com/x 和 https://JD.com/y 还有 http://b.tmall.com/z",
     "expect": ["b.tmall.com", "jd.com"]},
    {"id": "novelty-no-links-empty", "kind": "novelty",
     "text": "纯文本无任何链接，只有文字描述。", "expect": []},

    # ---- ADR 0019 开工 gate：可数事实"未决阻塞==0"且签字，绝不百分比 ------------
    {"id": "gate-needuser-locked", "kind": "consensus_gate", "signed": True,
     "decisions": [{"id": "d1", "status": "NeedUser"}], "expect": "locked"},   # 待用户拍板 → 锁
    {"id": "gate-open-blocking-locked", "kind": "consensus_gate", "signed": True,
     "decisions": [{"id": "d1", "status": "Accepted", "blocking": ["还没定"]}],
     "expect": "locked"},                                                      # 有未决阻塞 → 锁
    {"id": "gate-unsigned-locked", "kind": "consensus_gate", "signed": False,
     "decisions": [{"id": "d1", "status": "Accepted"}], "expect": "locked"},   # 零阻塞但没签字 → 锁
    {"id": "gate-clean-signed-open", "kind": "consensus_gate", "signed": True,
     "decisions": [{"id": "d1", "status": "Accepted"}, {"id": "d2", "status": "Deferred"},
                   {"id": "d3", "status": "Rejected"}], "expect": "open"},     # 零阻塞+签字 → 开

    # ---- ADR 0019 评审停止条件：可证伪、可数 ------------------------------------
    {"id": "stop-max-rounds", "kind": "review_stop", "max_rounds": 3,
     "rounds": [[{"id": "d1", "blocking": ["q"]}]] * 3, "expect": "max_rounds"},
    {"id": "stop-no-new-blocking", "kind": "review_stop", "max_rounds": 9,
     "rounds": [[{"id": "d1", "blocking": ["q1"]}], [{"id": "d1", "blocking": ["q1"]}]],
     "expect": "no_new_blocking"},                                             # 第二轮零新增 → 停
    {"id": "stop-wording-only", "kind": "review_stop", "max_rounds": 9,
     "rounds": [[{"id": "d1", "choice": "X", "blocking": ["q1"]}],
                [{"id": "d1", "choice": "X", "blocking": ["q1", "q2"]}],
                [{"id": "d1", "choice": "X", "blocking": ["q1", "q2", "q3"]}]],
     "expect": "wording_only"},                                               # 签名不变、blocking 仍增 → 只改措辞
    {"id": "stop-continue-new-blocking", "kind": "review_stop", "max_rounds": 9,
     "rounds": [[{"id": "d1", "blocking": ["q1"]}], [{"id": "d1", "blocking": ["q1", "q2"]}]],
     "expect": "continue"},                                                    # 仍有新增阻塞 → 继续评

    # ---- ADR 0019 收敛后升级：Open→NeedUser（不留死状态），四态不动 --------------
    {"id": "escalate-open-to-needuser", "kind": "escalate",
     "decisions": [{"id": "a", "status": "Open"},
                   {"id": "b", "status": "Open", "blocking": ["未决"]},
                   {"id": "c", "status": "Accepted"}, {"id": "d", "status": "Rejected"},
                   {"id": "e", "status": "Deferred"}, {"id": "f", "status": "NeedUser"}],
     "expect": ["NeedUser", "NeedUser", "Accepted", "Rejected", "Deferred", "NeedUser"]},
]
