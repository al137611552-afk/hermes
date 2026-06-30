"""代码搜索工具：grep（按内容） / glob（按文件名）。"""
from __future__ import annotations

import re

from .base import Tool, ToolError

MAX_HITS = 100
# 搜索时跳过的噪音目录
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def _iter_files(root, workspace=None, gi=None):
    for p in root.rglob("*"):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        if gi is not None and workspace is not None:
            rel = str(p.relative_to(workspace)).replace("\\", "/")
            if gi(rel, p.name):
                continue
        yield p


class GrepSearchTool(Tool):
    name = "grep_search"
    parallel_safe = True  # 只读，同轮多个调用并发执行
    description = "用正则在工作区文件内容中搜索，返回命中行（文件:行号:内容）。"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "搜索起始目录，默认工作区根"},
        },
        "required": ["pattern"],
    }

    def run(self, params: dict) -> str:
        try:
            rx = re.compile(params["pattern"])
        except re.error as e:
            raise ToolError(f"正则非法：{e}")
        root = self.resolve(params.get("path") or ".")
        if not root.is_dir():
            raise ToolError(f"目录不存在：{params.get('path', '.')}")

        from ..ignore import make_gitignore_matcher
        gi = make_gitignore_matcher(self.workspace)
        hits = []
        for f in _iter_files(root, self.workspace, gi):
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        rel = f.relative_to(self.workspace)
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= MAX_HITS:
                            hits.append(f"... (已截断，仅显示前 {MAX_HITS} 条)")
                            return "\n".join(hits)
            except OSError:
                continue
        return "\n".join(hits) if hits else "无命中。"


class GlobSearchTool(Tool):
    name = "glob_search"
    parallel_safe = True  # 只读，同轮多个调用并发执行
    description = "用通配符在工作区按文件名查找文件，如 **/*.py。"
    input_schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string", "description": "glob 通配，如 **/*.py"}},
        "required": ["pattern"],
    }

    def run(self, params: dict) -> str:
        from ..ignore import make_gitignore_matcher
        gi = make_gitignore_matcher(self.workspace)
        pattern = params.get("pattern") or "*"
        matches = []
        for p in self.workspace.glob(pattern):
            if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
                continue
            rel = str(p.relative_to(self.workspace)).replace("\\", "/")
            if gi(rel, p.name):
                continue
            matches.append(rel)
            if len(matches) >= MAX_HITS:
                break
        matches.sort()
        return "\n".join(matches) if matches else "无匹配文件。"
