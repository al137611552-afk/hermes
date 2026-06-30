"""CodingEvaluator：把测试/构建输出解析成事实（通过数/失败数、报错信号）。

吃得下的真实格式（hermes 里实际出现的）：
- pytest 摘要：`===== 1 failed, 2 passed in 0.3s =====`、`3 passed`、`2 errors`
- hermes 独立 runner 脚尾：`3/9 passed`
- 改后定向校验（verify.py）：`🧪 受影响测试未通过 …` / `🧪 受影响测试 …`
- 裸失败信号：Traceback / AssertionError / FAILED / 未通过
"""
from __future__ import annotations

import re

from ..contract import Evaluation

# pytest 摘要里的计数（大小写不敏感，单复数都吃）
_PASSED = re.compile(r"(\d+)\s+passed", re.I)
_FAILED = re.compile(r"(\d+)\s+failed", re.I)
_ERRORS = re.compile(r"(\d+)\s+errors?", re.I)
# hermes 独立 runner：`N/M passed`
_RUNNER = re.compile(r"(\d+)\s*/\s*(\d+)\s+passed", re.I)
# 触发"这是测试/构建输出"的特征词
_TEST_MARKERS = ("passed", "failed", "🧪", "未通过", "Traceback", "AssertionError",
                 "FAILED", "pytest", "需装 pytest")
# 裸失败信号（无计数时的兜底判失败）
_FAIL_WORDS = ("Traceback", "AssertionError", "FAILED", "未通过", "🧪 受影响测试未通过")


class CodingEvaluator:
    def applies(self, tool_name: str, output: str) -> bool:
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

        # 3) 由计数 / 裸信号判通过与否
        failed_n = metrics.get("failed", 0) + metrics.get("errors", 0)
        has_counts = "total" in metrics
        bare_fail = any(w in output for w in _FAIL_WORDS)

        if has_counts and failed_n == 0 and not bare_fail:
            signals.append("测试全过")
        elif failed_n > 0:
            signals.append(f"测试失败 {int(failed_n)} 项")
        elif bare_fail:
            signals.append("出现失败信号（无计数）")
        if "需装 pytest" in output:
            signals.append("需安装 pytest 才能真跑")
            confidence = min(confidence, 0.7)
        if "Traceback" in output:
            signals.append("有 Traceback（运行期报错）")

        # issues = 默认策略：测试未全过 / 有失败信号 = blocker（可被上层 Policy 覆盖）
        issues: list[str] = []
        if failed_n > 0 or (bare_fail and not (has_counts and failed_n == 0)):
            issues.append("测试未全过=blocker")

        return Evaluation(metrics=metrics, signals=signals, issues=issues,
                          confidence=confidence)
