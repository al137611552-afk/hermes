"""CodingEvaluator：把测试/构建输出解析成事实（通过数/失败数、报错信号）。

吃得下的真实格式（hermes 里实际出现的）：
- pytest 摘要：`===== 1 failed, 2 passed in 0.3s =====`、`3 passed`、`2 errors`
- hermes 独立 runner 脚尾：`3/9 passed`
- 改后定向校验（verify.py）：`🧪 受影响测试未通过 …` / `🧪 受影响测试 …`
- 裸失败信号：Traceback / AssertionError / FAILED / 未通过

**退出码兜底**（2026-08-19 补，V1 跑通链路时发现的缺口）：本 Evaluator 优先级高于 Shell，
只要输出里有 "pytest" 等特征词就接管——**接管了却解析不出计数时，原先会把 shell 的退出码
一起吞掉，判成"无 issues"**。于是 `pytest nonexistent.py`（exit 4，测试根本没跑起来）
这一整类"测试命令本身写错了"的失败，对评估内核完全隐形。现在退出码作为硬事实一并读入：
没有计数但非零退出 → 判"测试未跑成"；有计数且全过但非零退出 → 判"退出码非零"。
"""
from __future__ import annotations

import re

from ..contract import Evaluation
from .base import OBSERVATION_TOOLS
from .shell import EXIT_CODE_RE

# pytest 摘要里的计数（大小写不敏感，单复数都吃）。
# **必须限定同一行**（`[ \t]` 而非 `\s`）：`\s` 跨行，会把
# `pytest-9.1.0\nERROR: file not found` 读成"0 errors"——凭空造出 total=0 的幻影计数，
# 于是"测试根本没跑起来"被判成"用例全过"（2026-08-19 修 V1 缺口时揪出的既有 bug）。
# 结尾 `\b` 防 `3 passedxyz` 之类误匹配。
_PASSED = re.compile(r"(\d+)[ \t]+passed\b", re.I)
_FAILED = re.compile(r"(\d+)[ \t]+failed\b", re.I)
_ERRORS = re.compile(r"(\d+)[ \t]+errors?\b", re.I)
# hermes 独立 runner：`N/M passed`
_RUNNER = re.compile(r"(\d+)[ \t]*/[ \t]*(\d+)[ \t]+passed\b", re.I)
# 触发"这是测试/构建输出"的特征词
_TEST_MARKERS = ("passed", "failed", "🧪", "未通过", "Traceback", "AssertionError",
                 "FAILED", "pytest", "需装 pytest")
# 裸失败信号（无计数时的兜底判失败）
_FAIL_WORDS = ("Traceback", "AssertionError", "FAILED", "未通过", "🧪 受影响测试未通过")


class CodingEvaluator:
    def applies(self, tool_name: str, output: str) -> bool:
        """按**输出特征词**认领——因为测试结果会搭在各种工具的输出里
        （shell 跑测试、edit_file 后自动跑的受影响测试都算）。

        但**观察类工具除外**：读文件/检索读到的失败字样是"世界里有这段文本"，
        不是"我这次动作失败了"（ADR 0027 决策 11）。
        """
        if tool_name in OBSERVATION_TOOLS:
            return False
        return any(m in output for m in _TEST_MARKERS)

    def evaluate(self, tool_name: str, output: str, tool_input=None) -> Evaluation:
        metrics: dict[str, float] = {}
        signals: list[str] = []
        confidence = 0.6   # 默认：只命中启发式词、没拿到计数

        # 1) 优先 hermes runner 的 N/M（最精确）
        m = _RUNNER.search(output)
        if m:
            passed, total = int(m.group(1)), int(m.group(2))
            metrics.update(passed=passed, total=total, failed=total - passed)
            confidence = 1.0
        else:
            # 2) pytest 风格各计数
            p = _PASSED.search(output)
            f = _FAILED.search(output)
            e = _ERRORS.search(output)
            if p or f or e:
                passed = int(p.group(1)) if p else 0
                failed = int(f.group(1)) if f else 0
                errors = int(e.group(1)) if e else 0
                metrics.update(passed=passed, failed=failed, errors=errors,
                               total=passed + failed + errors)
                confidence = 1.0

        # 2.5) 退出码：shell 包装层给的硬事实，无论有没有计数都记下来
        m_exit = EXIT_CODE_RE.search(output)
        exit_code = int(m_exit.group(1)) if m_exit else None
        if exit_code is not None:
            metrics["exit_code"] = float(exit_code)

        # 3) 由计数 / 裸信号 / 退出码判通过与否
        failed_n = metrics.get("failed", 0) + metrics.get("errors", 0)
        # total==0 = 一个用例都没数到（如 pytest 收集失败时的 "0 passed"）——
        # 那不叫"有计数"，该落到下面的退出码兜底去判"没跑成"。
        has_counts = metrics.get("total", 0) > 0
        bare_fail = any(w in output for w in _FAIL_WORDS)
        nonzero_exit = exit_code is not None and exit_code != 0

        if has_counts and failed_n == 0 and not bare_fail:
            if nonzero_exit:
                # 用例都过了命令却非零退出：收集/插件/收尾阶段出的错，同样是真问题
                signals.append(f"用例全过但退出码 {exit_code}（收集/插件/收尾阶段出错）")
            else:
                signals.append("测试全过")
        elif failed_n > 0:
            signals.append(f"测试失败 {int(failed_n)} 项")
        elif bare_fail:
            signals.append("出现失败信号（无计数）")
        elif nonzero_exit:
            # 一个用例计数都解析不出、也没有裸失败信号，但命令非零退出
            # → 测试**根本没跑起来**（命令写错 / 文件不存在 / 收集失败）
            signals.append(f"测试未跑成（退出码 {exit_code}，无任何用例计数）")
            confidence = 1.0   # 退出码是硬事实，不再是 0.6 的启发式猜测
        if "需装 pytest" in output:
            signals.append("需安装 pytest 才能真跑")
            confidence = min(confidence, 0.7)
        if "Traceback" in output:
            signals.append("有 Traceback（运行期报错）")

        # issues = 默认策略：测试未全过 / 有失败信号 = blocker（可被上层 Policy 覆盖）
        issues: list[str] = []
        if failed_n > 0 or (bare_fail and not (has_counts and failed_n == 0)):
            issues.append("测试未全过=blocker")
        elif nonzero_exit:
            # 分开两种措辞：块C 要据此归类，"没跑成"多半是 NOT_FOUND/SYNTAX，
            # "跑了但收尾炸了"更接近 LOGIC/RESOURCE——喂给分类器的干草堆不该混为一谈。
            issues.append("退出码非零=失败" if has_counts else "测试未跑成=blocker")

        return Evaluation(metrics=metrics, signals=signals, issues=issues,
                          confidence=confidence)
