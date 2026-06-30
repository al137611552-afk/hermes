"""ShellEvaluator：把命令执行输出解析成事实（退出码、stderr、超时/缺程序）。

吃得下的真实格式（tools/shell.py RunShellTool）：
    [exit code] 0
    [stdout]
    ...
    [stderr]
    ...
另有：`命令超时（>30s）…`、`找不到 powershell 可执行程序。`、`已在后台启动进程 #3 …`
"""
from __future__ import annotations

import re

from ..contract import Evaluation

_SHELL_TOOLS = frozenset({"run_shell", "run_powershell", "run_bash"})
_EXIT = re.compile(r"\[exit code\]\s*(-?\d+)")
_STDERR = re.compile(r"\[stderr\]\n(.*)\Z", re.S)


class ShellEvaluator:
    def applies(self, tool_name: str, output: str) -> bool:
        return tool_name in _SHELL_TOOLS

    def evaluate(self, tool_name: str, output: str, tool_input=None) -> Evaluation:
        text = output or ""
        metrics: dict[str, float] = {}
        signals: list[str] = []
        issues: list[str] = []
        confidence = 1.0

        # 后台启动：没有同步退出码，是一次"已派发"事实，不判好坏
        if "已在后台启动进程" in text:
            return Evaluation(signals=["后台启动进程（无同步退出码）"], confidence=1.0)
        # 超时 / 找不到程序：执行未正常完成
        if "命令超时" in text:
            return Evaluation(signals=["命令超时"], issues=["命令超时=未完成"],
                              confidence=1.0)
        if "找不到" in text and "可执行程序" in text:
            return Evaluation(signals=["shell 可执行程序缺失"],
                              issues=["环境缺 shell=无法执行"], confidence=1.0)

        m = _EXIT.search(text)
        if m:
            code = int(m.group(1))
            metrics["exit_code"] = float(code)
            if code == 0:
                signals.append("退出码 0")
            else:
                signals.append(f"退出码 {code}")
                issues.append("退出码非零=失败")   # 默认策略，可被 Policy 覆盖
        else:
            confidence = 0.5   # 没有标准退出码行，吃不准

        se = _STDERR.search(text)
        if se and se.group(1).strip():
            signals.append("有 stderr 输出")

        return Evaluation(metrics=metrics, signals=signals, issues=issues,
                          confidence=confidence)
