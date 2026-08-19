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
# 退出码是 shell 输出格式的一部分，但 **CodingEvaluator 也要读**（它会接管 shell 跑出来的
# 测试输出，接管了就有责任别把退出码弄丢）。故升为共享词汇、只此一份——
# 同一个格式抄两份正则，迟早一处改了另一处没改（本项目已因"两处写"吃过亏）。
EXIT_CODE_RE = re.compile(r"\[exit code\]\s*(-?\d+)")
_STDERR = re.compile(r"\[stderr\]\n(.*)\Z", re.S)

# 模型**自己打出来的**退出码：`cmd 2>&1; echo "exit=$?"`。
# 这类写法极常见（真跑里连撞三次），而它会让 shell 的退出码变成 **echo 的 0**——
# 于是"命令根本没跑起来/找不到命令"这一整类失败对评估内核完全隐形：
# 不进 issues、不分类、不进失败语料，块E/块G 永远学不到（块 V4 补齐时照出）。
# 与块 V1a 修的"CodingEvaluator 吞退出码"是同一家族：**退出码是硬事实，丢了就什么都判不了。**
#
# 判据刻意收得很窄，只认「命令里确实写了 `$?`」+「输出里解析得出这个数」两条同时成立：
# 宽一点（比如"输出里有 Error 就算失败"）会把 `cat error.log`、grep 到 Error 的正常输出
# 全判成失败——那正是块 V4a 刚清理掉的那类语料污染，不能反手又造一批。
# **已知不覆盖**：模型串联命令但不打印 `$?`（如 `a; b`）时失败仍隐形。要覆盖那种只能靠
# 更宽的文本启发式，风险明显更高，留作显式决策（同 ADR 0027 决策 4 的自我约束）。
_ECHOED_EXIT_RE = re.compile(r"exit(?:\s*code)?\s*[=:]\s*(-?\d+)", re.I)


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

        m = EXIT_CODE_RE.search(text)
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

        # 退出码 0，但模型自己 `echo "$?"` 打出来的是非零 → 真实失败被命令串联掩盖了
        cmd = str((tool_input or {}).get("command") or "") if isinstance(tool_input, dict) else ""
        if not issues and "$?" in cmd:
            echoed = [int(x) for x in _ECHOED_EXIT_RE.findall(text)]
            bad = [c for c in echoed if c != 0]
            if bad:
                metrics["echoed_exit_code"] = float(bad[0])
                signals.append(f"命令自报退出码 {bad[0]}")
                issues.append(f"命令自报退出码 {bad[0]}=失败（整条命令的退出码被 echo 掩盖）")

        se = _STDERR.search(text)
        if se and se.group(1).strip():
            signals.append("有 stderr 输出")

        return Evaluation(metrics=metrics, signals=signals, issues=issues,
                          confidence=confidence)
