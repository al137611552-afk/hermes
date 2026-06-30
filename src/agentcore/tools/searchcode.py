"""按相关性检索代码工具（search_code）：大库里按概念/意图找最相关的代码块。

补 grep（精确串）/ find_symbol（按名）够不到的「按自然语言意图找代码」——大型陌生库里模型
读不动全部文件时，一次查询把最相关的几段拉进上下文。只读、免权限 gate。
"""
from __future__ import annotations

from ..retrieval import search_code
from .base import Tool, ToolError


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "按**相关性**检索工作区代码：给一段自然语言描述（要找的功能/概念/意图），"
        "返回全库最相关的若干代码块（文件:行 + 片段），按相关度排序。"
        "适合大型/陌生代码库里**按意图定位**（如「处理用户登录鉴权的地方」「计算折扣的逻辑」）——"
        "比 grep（要精确串）、find_symbol（要确切符号名）更适合「不知道叫什么、只知道想干嘛」时。"
        "底层是关系排序检索（非精确匹配）；要精确串用 grep_search，要看全文用 read_file。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "自然语言描述要找的代码功能/概念（如「分级折扣计算」「JWT 鉴权中间件」）"},
            "limit": {"type": "integer", "description": "返回多少个最相关块（默认 8，1~20）"},
        },
        "required": ["query"],
    }

    def run(self, params: dict) -> str:
        query = (params.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空：用一句话描述你要找的代码功能/概念。")
        limit = params.get("limit")
        try:
            limit = max(1, min(20, int(limit))) if limit is not None else 8
        except (TypeError, ValueError):
            limit = 8
        return search_code(self.workspace, query, limit)
