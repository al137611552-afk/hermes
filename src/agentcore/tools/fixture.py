"""失败输入固化 fixture（FR-13.E，P5 调试能力工程化第二波）。

debug 时一旦复现出错值/报错，用 capture_fixture 把「触发输入 + 期望/实际」固化成 `tests/` 下的
一个复现测试——bug 从「不可复现」变「可复现」，且**自动接入 FR-13.C 受影响测试闭环**：修好后它转绿、
守住回归。写完**立刻跑一次**确认「当前确实复现」（现在应当失败）。

约定：先固化再修。纯逻辑（slugify / build_fixture_content）与受控 IO（写盘 + 跑一次）分离。
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date

from ..verify import _PYTEST_NO_TESTS, _test_env, detect_test_argv
from .base import Tool, ToolError

FIXTURE_TIMEOUT = 30


def slugify(name: str) -> str:
    """名字 → 安全文件名片段（小写、字母数字下划线，其它折成下划线）。"""
    s = re.sub(r"[^0-9A-Za-z_]+", "_", (name or "").strip().lower()).strip("_")
    return s or "unnamed"


def build_fixture_content(body: str, note: str, today: str) -> str:
    """组织 fixture 文件内容（纯逻辑）：标准头注释（现象/日期）+ 复现/断言正文。"""
    head = ['"""固化复现 fixture（FR-13.E）。', ""]
    if note:
        head.append(f"现象：{note}")
    head += [f"捕获于 {today}。本测试用于复现该 bug —— 修复前应失败、修复后应通过（守住回归）。",
             '"""']
    return "\n".join(head) + "\n\n" + body.rstrip() + "\n"


class CaptureFixtureTool(Tool):
    dangerous = True  # 写文件 + 执行复现代码，过权限 gate
    name = "capture_fixture"
    description = (
        "把触发 bug 的输入固化成 tests/ 下的复现测试（FR-13.E）：debug 复现出错值/报错后调用，"
        "传 name（命名）+ body（一段会**失败**的复现/断言代码，import 目标并用具体输入断言期望值）"
        "+ 可选 note（现象）。写到 `tests/test_capture_<name>.py` 并**立刻跑一次确认当前确实复现**"
        "（现在应失败）。好处：bug 变可复现、修好后自动随受影响测试转绿、守住回归。**修 bug 前先固化。**"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "fixture 命名（用于文件名，如 yield_zero_years）"},
            "body": {"type": "string",
                     "description": "复现代码：import 目标 + 用触发输入断言期望值（现在应失败）。"
                                    "例：`from calc import yield_rate`\\n`assert yield_rate(1000,0,8)==0`"},
            "note": {"type": "string", "description": "可选：现象描述（什么输入→什么错值/报错）"},
        },
        "required": ["name", "body"],
    }

    def run(self, params: dict) -> str:
        name = (params.get("name") or "").strip()
        body = (params.get("body") or "").strip()
        if not name:
            raise ToolError("name 不能为空（用于 fixture 文件名）。")
        if not body:
            raise ToolError("body 不能为空：给一段会失败的复现/断言代码。")
        slug = slugify(name)
        rel = f"tests/test_capture_{slug}.py"
        path = (self.workspace / rel).resolve()
        if self.workspace.resolve() not in path.parents:
            raise ToolError("fixture 路径越出工作区。")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = build_fixture_content(body, (params.get("note") or "").strip(), date.today().isoformat())
        path.write_text(content, encoding="utf-8")

        # 立刻跑一次确认当前确实复现（期望失败）
        try:
            import importlib.util
            pytest_ok = importlib.util.find_spec("pytest") is not None
        except Exception:  # noqa: BLE001
            pytest_ok = False
        argv = detect_test_argv(rel, pytest_available=pytest_ok, node_available=False)
        verdict = ""
        try:
            proc = subprocess.run(argv, cwd=str(self.workspace), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=FIXTURE_TIMEOUT, env=_test_env(self.workspace))
            # 退出码 0 = 通过；pytest 退出码 5 = 没收集到用例（模块级 assert 全过）也算通过
            if proc.returncode == 0 or (pytest_ok and proc.returncode == _PYTEST_NO_TESTS):
                verdict = ("⚠ 但它**当前就通过了**——说明这段没复现出 bug（输入/断言不对，"
                           "或 bug 不在此路径）。请调整 body 让它真的失败，再固化。")
            else:
                tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-600:]
                verdict = "✓ 已确认当前复现（测试失败，符合预期）。修复后它会转绿。失败摘要：\n" + tail
        except subprocess.TimeoutExpired:
            verdict = f"（跑复现超时 >{FIXTURE_TIMEOUT}s，未能确认；检查 body 是否有长循环/等待。）"
        except OSError as e:
            verdict = f"（无法运行复现：{e}）"
        return f"已固化复现 fixture：{rel}\n{verdict}"
