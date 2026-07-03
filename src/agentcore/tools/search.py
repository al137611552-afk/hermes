"""代码搜索工具：grep（按内容） / glob（按文件名）。"""
from __future__ import annotations

import re
import time

from .base import Tool, ToolError

MAX_HITS = 100
# 搜索时跳过的噪音目录
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
# ReDoS/大文件防护：用户/模型给的正则用回溯引擎（re），遇长行会灾难回溯（实测 `(a+)+$` 对全 a 行
# 呈指数增长：28 字符行就要 16s，且 re.search 不放 GIL → 卡死整个进程。真实触发几乎都来自**长行**
# （压缩后的 min.js、单行 JSON、生成代码），故把喂给正则的每行截到 MAX_GREP_LINE、跳过超大文件、
# 再加总时限兜底。这不能挡住蓄意构造的指数正则（那需换 ripgrep/re2，见 CHANGELOG），但堵住了现实里
# 绝大多数"手滑贪婪正则 + 长行"导致的挂死。
MAX_GREP_LINE = 2000        # 单行喂给正则的最大字符（超出截断，去掉回溯的"燃料"）
MAX_GREP_FILE_BYTES = 5_000_000   # 跳过大于此的文件（多为数据/压缩产物，非源码）
GREP_DEADLINE_S = 8.0       # 整个 grep 的墙钟时限：超时报错让模型缩小 pattern/path，别无声挂死


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
        deadline = time.time() + GREP_DEADLINE_S
        for f in _iter_files(root, self.workspace, gi):
            try:
                if f.stat().st_size > MAX_GREP_FILE_BYTES:   # 跳过超大文件（数据/压缩产物）
                    continue
                if time.time() > deadline:
                    hits.append(f"... (搜索超过 {GREP_DEADLINE_S:.0f}s 已停止；请缩小 path 或收紧 pattern)")
                    break
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line[:MAX_GREP_LINE]):      # 截断长行：去掉灾难回溯的燃料，防 ReDoS 挂死
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
