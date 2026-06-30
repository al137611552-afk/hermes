"""可编程生命周期 hooks（对标 Claude Code PreToolUse/PostToolUse、Windsurf Cascade Hooks）。

让用户在 config 里配「工具调用前/后跑一条命令」，把现在硬编码的 auto_verify/auto_test/auto_review
泛化成**用户可扩展的守卫/动作**——例如：写文件前扫密钥拦截、edit 后跑项目 linter、挡住对某些
路径的修改等。不内嵌 LSP、不做协议，就是「触发事件 → 跑命令 → 看退出码/输出」。

约定（沿用 Claude Code 退出码语义，老用户零学习成本）：
- **PreToolUse**（工具执行前）：退出码 **2=拦截**（工具不执行，stdout 作为拒绝理由回灌模型）、
  **1=放行但警告**（stdout 警告并入工具结果回灌）、**0=放行**。
- **PostToolUse**（工具成功执行后）：stdout 追加到工具结果回灌模型（如 linter 诊断）。

hook 命令通过 **stdin 收到 JSON**：{event, tool, params, workspace[, result]}，cwd=工作区。
纯逻辑（match_hooks / parse_pre_result）与受控 IO（HookRunner 子进程）分离，便于单测。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PRE = "PreToolUse"
POST = "PostToolUse"

# 退出码语义（PreToolUse）
EXIT_DENY = 2
EXIT_WARN = 1


def match_hooks(hooks: list, event: str, tool_name: str) -> list:
    """挑出匹配某事件 + 工具名的 hook（纯逻辑，按配置顺序）。

    matcher 为正则，对工具名 `re.search`；空/缺省视为匹配全部。正则非法时**不匹配**
    （宁可漏跑也不因坏配置卡住工具）。
    """
    out = []
    for h in hooks or []:
        if getattr(h, "event", None) != event:
            continue
        pat = (getattr(h, "matcher", "") or "").strip() or ".*"
        try:
            if re.search(pat, tool_name):
                out.append(h)
        except re.error:
            continue
    return out


def parse_pre_result(returncode: int, stdout: str, stderr: str) -> "tuple[str, str]":
    """把 PreToolUse hook 的退出码 + 输出解析成 (decision, message)（纯逻辑）。

    decision ∈ {"deny","warn","allow"}。message 优先取 stdout，空则取 stderr。
    未知退出码按 allow 处理（坏 hook 不阻塞正常工作）。
    """
    msg = (stdout or "").strip() or (stderr or "").strip()
    if returncode == EXIT_DENY:
        return "deny", msg or "操作被 PreToolUse hook 拦截。"
    if returncode == EXIT_WARN:
        return "warn", msg
    return "allow", ""


class HookRunner:
    """按配置在工具调用前/后跑用户命令。无匹配 hook 时零开销。线程安全（仅子进程，无共享态）。"""

    def __init__(self, workspace: Path, hooks: list) -> None:
        self.workspace = Path(workspace).resolve()
        self.hooks = list(hooks or [])

    def _run(self, hook, payload: dict) -> "subprocess.CompletedProcess | None":
        cmd = (getattr(hook, "command", "") or "").strip()
        if not cmd:
            return None
        try:
            return subprocess.run(
                ["bash", "-lc", cmd] if _posix() else ["cmd", "/c", cmd],
                input=json.dumps(payload, ensure_ascii=False),
                cwd=str(self.workspace), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=max(1, int(getattr(hook, "timeout", 15) or 15)),
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None  # hook 跑不起来/超时：不阻塞工具（pre 视为 allow、post 视为无输出）

    def pre(self, tool_name: str, params: dict) -> "tuple[bool, str | None]":
        """PreToolUse：返回 (是否放行, 警告/拒绝信息)。任一 hook 拦截即整体拦截。"""
        matched = match_hooks(self.hooks, PRE, tool_name)
        if not matched:
            return True, None
        payload = {"event": PRE, "tool": tool_name, "params": params,
                   "workspace": str(self.workspace)}
        warns: list[str] = []
        for h in matched:
            proc = self._run(h, payload)
            if proc is None:
                continue
            decision, msg = parse_pre_result(proc.returncode, proc.stdout, proc.stderr)
            label = (getattr(h, "name", "") or "hook").strip()
            if decision == "deny":
                return False, f"⛔ 被 hook「{label}」拦截：{msg}"
            if decision == "warn" and msg:
                warns.append(f"⚠ hook「{label}」：{msg}")
        return True, ("\n".join(warns) if warns else None)

    def post(self, tool_name: str, params: dict, result: str) -> "str | None":
        """PostToolUse：返回要追加到工具结果的文本（各 hook stdout 拼接），无则 None。"""
        matched = match_hooks(self.hooks, POST, tool_name)
        if not matched:
            return None
        payload = {"event": POST, "tool": tool_name, "params": params,
                   "workspace": str(self.workspace), "result": result[:4000]}
        outs: list[str] = []
        for h in matched:
            proc = self._run(h, payload)
            if proc is None:
                continue
            text = (proc.stdout or "").strip()
            if text:
                label = (getattr(h, "name", "") or "hook").strip()
                outs.append(f"🪝 hook「{label}」：\n{text}")
        return "\n".join(outs) if outs else None


def _posix() -> bool:
    import os
    return os.name != "nt"


def make_hook_runner(workspace: Path, hooks: list) -> "HookRunner | None":
    """有配置 hook 才建 runner，否则 None（零开销、行为同旧版）。"""
    return HookRunner(workspace, hooks) if hooks else None
