"""MCP 客户端（P6.4）：连接 stdio MCP server，把其工具接入 Agent 工具循环。"""
from __future__ import annotations

from .manager import McpManager
from .tool import McpTool, convert_result, qualified_name

__all__ = ["McpManager", "McpTool", "convert_result", "qualified_name"]
