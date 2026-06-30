"""MCP 工具适配（P6.4）：把 MCP server 通告的工具包成内核统一的 Tool。

`McpTool.run()` 把调用代理给 manager（经后台 asyncio loop 同步执行），再把
`CallToolResult` 转回 str / ToolOutput（图片走并列块，见 ADR-0010）。结果转换是
纯函数（鸭子类型，不依赖 mcp SDK 导入），便于在无 server 环境单测。
"""
from __future__ import annotations

from typing import Callable

from ..tools.base import Tool, ToolError, ToolOutput

# 工具名分隔：server 名 + "__" + 原始工具名（整体满足 Anthropic 工具名 [a-zA-Z0-9_-]{1,64}）
SEP = "__"


def qualified_name(server: str, tool: str) -> str:
    return f"{server}{SEP}{tool}"


def convert_result(result) -> tuple[str, list[dict], bool]:
    """把 MCP CallToolResult 转成 (文本, 额外内容块, ok)。

    - TextContent  -> 文本（多段拼接）
    - ImageContent -> image 块（base64），作为并列块（部分端点不解析 tool_result 内嵌图）
    - EmbeddedResource / 其它 -> 退化为文本占位
    - isError -> ok=False
    """
    texts: list[str] = []
    blocks: list[dict] = []
    for item in (getattr(result, "content", None) or []):
        itype = getattr(item, "type", None)
        if itype == "text":
            texts.append(getattr(item, "text", "") or "")
        elif itype == "image":
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": getattr(item, "mimeType", "image/png"),
                    "data": getattr(item, "data", ""),
                },
            })
            texts.append("[图片]")
        elif itype == "resource":
            res = getattr(item, "resource", None)
            txt = getattr(res, "text", None)
            texts.append(txt if txt else "[资源]")
        else:
            texts.append(f"[{itype or '未知内容'}]")
    ok = not bool(getattr(result, "isError", False))
    text = "\n".join(t for t in texts if t) or ("(无输出)" if ok else "工具返回错误")
    return text, blocks, ok


class McpTool(Tool):
    """单个 MCP 工具的适配器。来自外部 server，默认 dangerous（逐次过权限 gate）。"""

    def __init__(
        self,
        server: str,
        tool_name: str,
        description: str,
        input_schema: dict,
        caller: Callable[[str, str, dict], object],
        *,
        trusted: bool = False,
    ) -> None:
        # 不调用 super().__init__：MCP 工具无工作区、不用路径解析
        self.server = server
        self.tool_name = tool_name  # server 上的原始名（调用时用）
        self.name = qualified_name(server, tool_name)
        self.description = f"[MCP:{server}] {(description or '').strip()}".strip()
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.dangerous = not trusted  # trust 的 server 免 gate
        self._caller = caller

    def run(self, params: dict):
        try:
            result = self._caller(self.server, self.tool_name, params or {})
        except Exception as e:  # 连接断开 / 超时 / 子进程已退出等
            raise ToolError(
                f"MCP 调用失败（{self.server}.{self.tool_name}）：{type(e).__name__}: {e}"
            )
        text, blocks, ok = convert_result(result)
        if not ok:
            raise ToolError(text)  # 回灌模型，AgentLoop 不中断
        return ToolOutput(text=text, blocks=blocks) if blocks else text
