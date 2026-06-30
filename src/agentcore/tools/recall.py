"""recall_history 工具：搜过往会话的原始对话记录（细节的无损来源，递进记忆的最后一层）。

记忆是逐层下钻：① 注入的框架原则(principle)/事实(fact) → ② 不够再用本工具搜原始对话。
只读检索、不改任何状态，不过权限 gate。
"""
from __future__ import annotations

from typing import Callable

from .base import Tool, ToolError


class RecallHistoryTool(Tool):
    name = "recall_history"
    description = (
        "搜索你与用户**过往会话的原始对话记录**（按关键词）。这是记忆的最后一层、最精确的细节来源——"
        "**递进使用**：先用已注入的长期记忆（框架原则 / 事实）；当它们不足以回答、你需要找回"
        "「当时具体怎么说 / 怎么做 / 哪段代码」的精确细节时，才用本工具搜原文。"
        "query 取关键术语（别用整句），命中任一词即返回。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词，取关键术语（如功能名/报错/概念），别用整句"},
        },
        "required": ["query"],
    }
    parallel_safe = True  # 只读检索，可与其它只读工具并行

    def __init__(self, search_fn: "Callable[[str, int], list[dict]]") -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._search = search_fn

    def run(self, params: dict) -> str:
        q = (params.get("query") or "").strip()
        if not q:
            raise ToolError("query 不能为空")
        rows = self._search(q, 8) or []
        if not rows:
            return "（过往对话里没搜到相关记录）"
        lines = []
        for r in rows:
            who = "用户" if r.get("role") == "user" else "AI"
            title = r.get("title") or "无标题会话"
            lines.append(f"[会话「{title}」] {who}：{r.get('text', '')}")
        return "从过往对话原始记录里找到以下片段：\n" + "\n".join(lines)
