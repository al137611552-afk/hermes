"""长期记忆工具（P6.3）：让 Agent 在对话中主动记 / 查 / 删持久记忆。

这些工具只操作应用内部的记忆库（不碰文件系统 / 命令），属非危险操作，不过权限
gate；调用过程作为工具块在前端可见。注入与自动抽取见 longmem.py / bridge。
"""
from __future__ import annotations

from ..store.memory import KINDS, MemoryStore
from .base import Tool, ToolError


class _MemoryTool(Tool):
    """记忆工具基类：无需工作区，持有 MemoryStore。"""

    def __init__(self, store: MemoryStore) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self.store = store


class RememberTool(_MemoryTool):
    name = "remember"
    description = (
        "记下一条**跨项目通用**、换个项目也仍有用的事实（用户称呼/长期偏好/工作习惯/反复强调的"
        "要求/技能能力倾向）。**项目专属的内容（某项目的目标/架构/技术栈/决定/约定等）不要记这里，"
        "应写进该项目根目录的 hermes.md**——记进全局记忆会跨项目互相干扰。只在确实值得长期、"
        "跨项目保留时使用；一次性内容不要记。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的跨项目事实，一句话、具体、自包含"},
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "类别：user(关于用户) / preference(偏好) / skill(能力/技能倾向) / fact(其它跨项目事实)",
            },
        },
        "required": ["content"],
    }

    def run(self, params: dict) -> str:
        content = (params.get("content") or "").strip()
        if not content:
            raise ToolError("content 不能为空")
        mid = self.store.add(content, params.get("kind", "fact"), source="tool")
        if mid is None:
            return "已存在等价的记忆，未重复记录。"
        return f"已记住（#{mid}）：{content}"


class RecallTool(_MemoryTool):
    name = "recall"
    description = "检索你的长期记忆。给 query 做关键词匹配；不给则列出全部最近记忆。"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词；留空则列出全部"},
        },
    }

    def run(self, params: dict) -> str:
        items = self.store.search(params.get("query", ""), limit=50)
        if not items:
            return "（没有匹配的长期记忆）"
        return "\n".join(f"#{m['id']} [{m['kind']}] {m['content']}" for m in items)


class ForgetTool(_MemoryTool):
    name = "forget"
    description = (
        "删除不再正确或不需要的长期记忆。可按 id（recall 给出的）删一条，"
        "或按 query 关键词删除所有匹配的记忆（如用户说'忘记我的名字'就用 query='名字'/'叫'）。"
        "被忘记的内容之后不会再被自动重新记住。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "要删除的单条记忆 id（来自 recall）"},
            "query": {"type": "string", "description": "关键词：删除所有内容包含它的记忆"},
        },
    }

    def run(self, params: dict) -> str:
        query = (params.get("query") or "").strip()
        if query:
            deleted = self.store.forget_by_query(query)
            if not deleted:
                return f"没有匹配「{query}」的记忆。"
            return f"已忘记 {len(deleted)} 条（之后不会再被自动记住）：" + "；".join(deleted)
        if params.get("id") is not None:
            try:
                mid = int(params["id"])
            except (TypeError, ValueError):
                raise ToolError("id 需为整数")
            return (f"已删除记忆 #{mid}（之后不会再被自动记住）"
                    if self.store.delete(mid) else f"未找到记忆 #{mid}")
        raise ToolError("需要提供 id 或 query")
