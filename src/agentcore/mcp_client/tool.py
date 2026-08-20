"""MCP 工具适配（P6.4）：把 MCP server 通告的工具包成内核统一的 Tool。

`McpTool.run()` 把调用代理给 manager（经后台 asyncio loop 同步执行），再把
`CallToolResult` 转回 str / ToolOutput（图片走并列块，见 ADR-0010）。结果转换是
纯函数（鸭子类型，不依赖 mcp SDK 导入），便于在无 server 环境单测。
"""
from __future__ import annotations

from typing import Callable

from .diag import inside_hermes_dir as _in_hermes
from .gitwatch import diff_status, render_changes, status_lines
from ..tools.base import Tool, ToolError, ToolOutput

# 工具名分隔：server 名 + "__" + 原始工具名（整体满足 Anthropic 工具名 [a-zA-Z0-9_-]{1,64}）
SEP = "__"


def qualified_name(server: str, tool: str) -> str:
    return f"{server}{SEP}{tool}"


def sdk_field(obj, *names, default=None):
    """按多个候选名取字段（SDK **1.x 驼峰 / 2.x 蛇形**并存，两种都要认）。

    CLAUDE.md 里记过同类坑（`Tool.inputSchema` → `input_schema`），但当时只改了那一个。
    2026-08-20 端到端真跑才发现还漏着两处：`isError`（于是 **MCP 工具报错从来没被识别成错误**，
    错误文本被当正常结果回灌）和 `structuredContent`（于是续话 id 一直取不到）。
    """
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


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
                    "media_type": sdk_field(item, "mimeType", "mime_type", default="image/png"),
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
    ok = not bool(sdk_field(result, "isError", "is_error", default=False))
    text = "\n".join(t for t in texts if t) or ("(无输出)" if ok else "工具返回错误")
    return text, blocks, ok


# 续话标识在不同 server 里叫法不一（Codex 实测：`structuredContent.threadId`，
# 事件里还并存 `thread_id` / `session_id`）。按**键名**认，不按 server 名认——
# 写死 "codex" 就等于给下一个 agent 型 server 再抄一遍。
THREAD_KEYS = ("threadId", "thread_id", "conversationId", "sessionId")
# prepare() 用它把"这次是自动接续的"传给 run()。**放在 params 里而不是实例上**：
# 同一个 McpTool 被所有会话共用，放实例上会在并发调用之间串台。
RESUMED_KEY = "_hermes_resumed"


def extract_thread_id(result) -> str:
    """从工具返回里取续话 id（纯函数）。取不到返回空串，绝不抛。

    只认 `structuredContent`——**不去正文里正则捞**：正文是给人看的自然语言，
    形状随模型输出变，靠它接续迟早接错会话，比接不上更糟。
    """
    sc = sdk_field(result, "structuredContent", "structured_content")
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
        # **两个都开时以「更严」的为准**：`always_confirm` 压过 `trust`。
        # 2026-08-20 真机：用户配里 trust=true + always_confirm=true，按原先"trust 优先"
        # 的写法，"每次都问"被**静默作废**、一次确认都没弹。权限上宁可多问一次，
        # 也不能让两个开关打架的结果是"谁都没拦住"。面板已改成两者互斥，这里是兜底。
        self.always_confirm = bool(always_confirm)
        self.dangerous = self.always_confirm or not trusted
        # 续话记忆（ThreadMemory）：None＝不接续，行为同以前
        self._threads = threads
        self._thread_key = thread_param(self.input_schema)
        # 当前会话的工作区（由 build_registry 绑）。None＝不补 cwd，行为同以前。
        self.workspace = None
        # **「schema 收 cwd」＝agent 型**：它在某个目录里自己干活（codex 就是）。
        # 用它同时决定"补不补 cwd"和"要不要用 git 记录改动"。
        # 别用 thread key 当判据——`codex__codex` 的 schema 里根本没有 threadId（只有 reply 有）。
        self._takes_cwd = "cwd" in ((self.input_schema or {}).get("properties") or {})
        self._caller = caller

    # 要实时流（loop 会给 stream 回调，前端把增量追加到运行中的工具块）。
    # **agent 型 server 没有它就是黑箱**：codex 一次调用要跑几分钟，中途什么都看不到，
    # 出错也只能等到最后。Codex 的 MCP server 本来就在发 notifications/progress，
    # 之前只是没人接（2026-08-20 真机反馈）。不发进度的 server 不受影响——没通知就没增量。
    wants_stream = True

    def bind_workspace(self, workspace):
        """绑定**当前会话**的工作区，返回一个轻副本（server 侧进程与续话记忆仍共享）。

        agent 型 server 的 `cwd` 是**按调用**给的参数，不是 server 配置——所以不必为换工作区
        重启子进程。绑成副本而不是改自己：同一个 McpTool 实例被所有会话共用，
        直接改字段会在并发会话之间串台。
        """
        import copy
        if not workspace:
            return self
        t = copy.copy(self)
        t.workspace = str(workspace)
        return t

    def prepare(self, params: dict) -> dict:
        """调用前补齐参数。**由 loop 在权限确认之前调用**——确认条上要显示的是
        真正会执行的参数（cwd 决定它在哪儿干活，看不到就等于没确认）。
        """
        params = dict(params or {})
        params.pop(RESUMED_KEY, None)     # 幂等：loop 与 run 会各调一次
        key = self._thread_key
        if key and self._threads is not None and not any(params.get(k) for k in THREAD_KEYS):
            last = self._threads.get(self.server)
            if last:
                params[key] = last
                params[RESUMED_KEY] = last      # run() 取走，用来在结果里标一句
        # cwd：schema 收这个参数、模型又没给 → 用当前会话的工作区。
        # 不补的话它就在 hermes 自己的安装目录里干活（真机踩过），而且**不报错**。
        if self._takes_cwd and not params.get("cwd") and self.workspace:
            params["cwd"] = self.workspace
        return params

    def run(self, params: dict, stream=None):
        params = self.prepare(params)
        resumed = params.pop(RESUMED_KEY, "")
        # agent 型调用（schema 收 cwd 的那种）：**调用前后各取一次 git 状态**。
        # 事后取一次会把用户自己没提交的改动算到 agent 头上——那种"自信的错数"比没有更糟。
        watch_cwd = str(params.get("cwd") or "") if self._takes_cwd else ""
        before = status_lines(watch_cwd) if watch_cwd else None
        # **在 hermes 自己的目录里干活**几乎不会是本意（真机踩到：模板把"当前工作区"填成了
        # hermes 的临时工作区 `data/workspaces/_scratch`）。它不报错、结果也看着正常，
        # 只是全干在了错地方——所以调用时就喊出来，别等用户自己发现。
        misplaced = ""
        if watch_cwd and _in_hermes(watch_cwd):
            misplaced = (f"⚠ 工作目录是 hermes 自己的目录（{watch_cwd}）——不是你的项目。"
                         "在 hermes 里打开项目工作区，或在调用参数里显式给 cwd。\n")
            if stream is not None:
                try:
                    stream("warn", misplaced)
                except Exception:  # noqa: BLE001
                    pass
        try:
            result = self._caller(self.server, self.tool_name, params, stream)
        except ToolError:
            raise           # 已经是可读文案（如"调用已被用户停止"），别再套一层"MCP 调用失败"
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
        # **改了什么由 git 说，不由 agent 自述说**（同评测那条「判分优先程序化」）
        changed = render_changes(diff_status(before, status_lines(watch_cwd))) if watch_cwd else ""
        text = f"{misplaced}{note}{text}{tail}{changed}"
        return ToolOutput(text=text, blocks=blocks) if blocks else text

