"""代码库检索工具（FR-9.2）：项目符号大纲 + 按名找定义。

补 grep/glob 给不了的"结构化检索"：让 Agent 不读全文件就掌握项目结构、精确定位定义。
只读、限工作区内、非危险（不过权限 gate）。底层用纯逻辑 codeindex（Python ast + 其它语言正则）。
"""
from __future__ import annotations

from ..codeindex import format_finds, format_outline, walk_find, walk_outline
from .base import Tool, ToolError


class CodeOutlineTool(Tool):
    name = "code_outline"
    parallel_safe = True  # 只读，同轮多个调用并发执行
    description = (
        "列出某文件或目录里的代码符号大纲（类、函数、方法 + 签名 + 行号），用来**快速掌握项目结构**、"
        "不必通读文件。默认从工作区根开始；大项目建议传子目录 path 细看。支持 Python 与常见语言。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录（相对工作区），默认工作区根 ."},
        },
    }

    def run(self, params: dict) -> str:
        target = self.resolve(params.get("path") or ".")
        if not target.exists():
            raise ToolError(f"路径不存在：{params.get('path', '.')}")
        if target.is_file():
            from ..codeindex import extract_file, is_indexable
            if not is_indexable(target):
                raise ToolError("该文件类型不支持符号大纲")
            try:
                src = target.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                raise ToolError(f"读取失败：{e}")
            rel = str(target.relative_to(self.workspace))
            return format_outline([(rel, extract_file(target, src))], False)
        files, truncated = walk_outline(target, self.workspace)
        return format_outline(files, truncated)


class FindSymbolTool(Tool):
    name = "find_symbol"
    parallel_safe = True  # 只读，同轮多个调用并发执行
    description = (
        "按名字在工作区里**查找定义**（类/函数/方法等），返回 文件:行 + 类型 + 签名。"
        "比 grep 准——只给定义、不给所有提及。传准确的符号名最好；找不到精确匹配会回退到名称包含它的结果。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要找的符号名（类/函数/方法名）"},
            "path": {"type": "string", "description": "可选：缩小搜索的子目录，默认工作区根"},
        },
        "required": ["name"],
    }

    def run(self, params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            raise ToolError("name 不能为空")
        root = self.resolve(params.get("path") or ".")
        if not root.is_dir():
            raise ToolError(f"目录不存在：{params.get('path', '.')}")
        hits, more, exact = walk_find(root, self.workspace, name)
        return format_finds(hits, more, exact, name)
