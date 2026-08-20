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


# 续话标识在不同 server 里叫法不一（Codex 实测：`structuredContent.threadId`，
# 事件里还并存 `thread_id` / `session_id`）。按**键名**认，不按 server 名认——
# 写死 "codex" 就等于给下一个 agent 型 server 再抄一遍。
THREAD_KEYS = ("threadId", "thread_id", "conversationId", "sessionId")


def extract_thread_id(result) -> str:
    """从工具返回里取续话 id（纯函数）。取不到返回空串，绝不抛。

    只认 `structuredContent`——**不去正文里正则捞**：正文是给人看的自然语言，
    形状随模型输出变，靠它接续迟早接错会话，比接不上更糟。
    """
    sc = getattr(result, "structuredContent", None)
    if not isinstance(sc, dict):
        return ""
    for k in THREAD_KEYS:
        v = sc.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def thread_param(input_schema: dict) -> str:
    """这个工具**接不接**续话 id；接的话用哪个键（纯函数）。不接返回空串。

    按 schema 判定而不是按工具名（`*-reply` 那种后缀是约定、不是契约）。
    """
    props = (input_schema or {}).get("properties") or {}
    for k in THREAD_KEYS:
        if k in props:
            return k
    return ""


class ThreadMemory:
    """记住每个 server 最近一次的续话 id（线程安全）。

    **为什么要它**：续话 id 只能靠模型自己从上一次返回里掏出来再带上，一旦漏了就是
    **静默新开一个会话**——上下文全丢、还不报错，表现成"它怎么又从头问一遍"
    （2026-08-20 真机反馈的痛点）。
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._ids: dict = {}

    def get(self, server: str) -> str:
        with self._lock:
            return self._ids.get(server, "")

    def set(self, server: str, value: str) -> None:
        if not value:
            return
        with self._lock:
            self._ids[server] = value


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
        always_confirm: bool = False,
        threads=None,
    ) -> None:
        # 不调用 super().__init__：MCP 工具无工作区、不用路径解析
        self.server = server
        self.tool_name = tool_name  # server 上的原始名（调用时用）
        self.name = qualified_name(server, tool_name)
        self.description = f"[MCP:{server}] {(description or '').strip()}".strip()
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.dangerous = not trusted  # trust 的 server 免 gate
        # 高影响力（agent 型 server）：即便本会话点过「全部允许」也每次都问。
        # trust=True 时本来就不过 gate，二者互斥——由配置侧保证别同时开。
        self.always_confirm = bool(always_confirm) and not trusted
        # 续话记忆（ThreadMemory）：None＝不接续，行为同以前
        self._threads = threads
        self._thread_key = thread_param(self.input_schema)
        self._caller = caller

    # 要实时流（loop 会给 stream 回调，前端把增量追加到运行中的工具块）。
    # **agent 型 server 没有它就是黑箱**：codex 一次调用要跑几分钟，中途什么都看不到，
    # 出错也只能等到最后。Codex 的 MCP server 本来就在发 notifications/progress，
    # 之前只是没人接（2026-08-20 真机反馈）。不发进度的 server 不受影响——没通知就没增量。
    wants_stream = True

    def run(self, params: dict, stream=None):
        params, resumed = self._resume(dict(params or {}))
        try:
            result = self._caller(self.server, self.tool_name, params, stream)
        except Exception as e:  # 连接断开 / 超时 / 子进程已退出等
            raise ToolError(
                f"MCP 调用失败（{self.server}.{self.tool_name}）：{type(e).__name__}: {e}"
            )
        text, blocks, ok = convert_result(result)
        tid = extract_thread_id(result)
        if self._threads is not None:
            self._threads.set(self.server, tid)
        if not ok:
            raise ToolError(text)  # 回灌模型，AgentLoop 不中断
        # **把续话 id 摆到明面上**：自动接续是兜底，模型自己带上才是常态；
        # 不回显的话，模型既学不会也没法在换工具时带走。
        note = f"[已自动接续 {self._thread_key}={resumed}]\n" if resumed else ""
        tail = f"\n\n[{self._thread_key or 'thread'}] {tid}（追问同一件事时带上它）" if tid else ""
        text = f"{note}{text}{tail}"
        return ToolOutput(text=text, blocks=blocks) if blocks else text

    def _resume(self, params: dict) -> tuple:
        """缺了续话 id 就用上次的补上（返回 (params, 补的值)）。

        **只在模型没给的时候补**：模型显式给了就以它为准——它可能有意开新会话或换线程。
        """
        key = self._thread_key
        if not key or self._threads is None or params.get(key):
            return params, ""
        # 同义键任一已给出，就当模型自己带了（别覆盖它）
        if any(params.get(k) for k in THREAD_KEYS):
            return params, ""
        last = self._threads.get(self.server)
        if last:
            params[key] = last
        return params, last
