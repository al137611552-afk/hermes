"""Git 工具（FR-10.1）：只读 git_status / git_diff / git_log（非危险、不过 gate）+
git_commit / git_branch（危险、过权限 gate 逐次确认）。

工具**常注册**：工作区不是 git 仓库时返回可读错误（不崩），这样会话中途 git init 后
立刻可用。仓库礼仪走"引导不硬拦"：commit 结果显示分支名，默认分支直接提交附 ⚠。
底层封装在 gitsupport.py，本模块只做参数校验与错误转换。
"""
from __future__ import annotations

from .. import gitsupport as g
from .base import Tool, ToolError


def _call(fn, *args, **kwargs) -> str:
    """把 GitError 转成 ToolError（信息回灌模型，不中断循环）。"""
    try:
        return fn(*args, **kwargs)
    except g.GitError as e:
        raise ToolError(str(e)) from None


class GitStatusTool(Tool):
    name = "git_status"
    description = (
        "查看 git 仓库当前状态（只读）：当前分支与领先/落后、未提交改动（含未跟踪文件）、"
        "本地分支列表。工作区需是 git 仓库。"
    )
    input_schema = {"type": "object", "properties": {}}

    def run(self, params: dict) -> str:
        return _call(g.status_summary, self.workspace)


class GitDiffTool(Tool):
    name = "git_diff"
    description = (
        "查看未提交改动相对 HEAD（最近一次提交）的 diff（只读）。不带 path 看全部改动"
        "（未跟踪新文件只列名）；带 path 看单个文件（未跟踪文件也能看内容）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "可选：只看这个文件/目录的 diff"},
        },
    }

    def run(self, params: dict) -> str:
        return _call(g.diff_text, self.workspace, (params.get("path") or "").strip() or None)


class GitLogTool(Tool):
    name = "git_log"
    description = "查看最近的提交历史（只读，单行格式：短 sha/日期/作者/说明）。"
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "最多显示几条（默认 20）"},
        },
    }

    def run(self, params: dict) -> str:
        try:
            limit = int(params.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        return _call(g.log_text, self.workspace, limit)


class GitCommitTool(Tool):
    dangerous = True
    name = "git_commit"
    description = (
        "暂存并提交改动（需用户确认）。默认提交全部未提交改动；给 paths 则只提交这些路径。"
        "仓库礼仪：只在用户要求时提交；用户未明说时不要在默认分支（main/master）直接提交，"
        "先用 git_branch 开分支。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "提交说明（简洁、说清本次改动）"},
            "paths": {
                "type": "array", "items": {"type": "string"},
                "description": "可选：只提交这些文件/目录（默认全部改动）",
            },
        },
        "required": ["message"],
    }

    def run(self, params: dict) -> str:
        paths = params.get("paths") or None
        if paths is not None and not isinstance(paths, list):
            raise ToolError("paths 应为字符串数组")
        return _call(g.commit, self.workspace, params.get("message") or "", paths)


class GitBranchTool(Tool):
    dangerous = True
    name = "git_branch"
    description = (
        "建/切分支（需用户确认）：op=create 创建并切换到新分支；op=switch 切换到已有分支。"
        "分支列表看 git_status。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["create", "switch"], "description": "操作类型"},
            "name": {"type": "string", "description": "分支名"},
        },
        "required": ["op", "name"],
    }

    def run(self, params: dict) -> str:
        return _call(g.branch, self.workspace, params.get("op") or "", params.get("name") or "")
