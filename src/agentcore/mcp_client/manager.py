"""MCP 客户端管理（P6.4）：连接 stdio MCP server、列工具、代理调用。

内核是同步的，MCP SDK 是 asyncio。这里起一个常驻后台线程跑事件循环，所有 MCP
操作用 `run_coroutine_threadsafe(...).result()` 从同步侧阻塞获取。

关键：stdio_client / ClientSession 的 async context 必须在**同一个 task 内**进入与退出
（否则 anyio 报 "cancel scope in different task"）。故每个 server 用一个常驻 `_serve`
协程：进入 context → initialize → list_tools → 把工具经 concurrent.future 交回主线程 →
`await stop_event`保活；close() 时置位 stop_event，`_serve` 在自己的 task 里干净退出。
调用 `call_tool` 作为另一个协程跑（只用 session 的内部流，不碰 cancel scope，安全）。

mcp SDK 仅在方法内惰性导入：未装 SDK 且 mcp.enabled=false 时整模块可正常 import。
"""
from __future__ import annotations

import concurrent.futures
import sys
import threading
import time

from ..config import MCPConfig, McpServerConfig
from .events import CODEX_EVENT, event_request_id, render_event
from .tool import McpTool, ThreadMemory


_EMPTY_SCHEMA = {"type": "object", "properties": {}}


def tool_input_schema(t) -> dict:
    """从 server 通告的 Tool 上取 input schema，**兼容 mcp SDK 新旧字段名**（纯函数，鸭子类型）。

    mcp SDK **2.0 起把 `inputSchema` 改名成 `input_schema`**（2026-08-07 真跑撞到：装了 2.0.0 后
    `AttributeError: 'Tool' object has no attribute 'inputSchema'` 让**所有 MCP server 一律连不上**，
    浏览器穿透 / 文件系统 / Codex 模板全废）。pyproject 只写 `mcp>=1.2`，新装机器拿到的就是新版，
    所以两个名字都要认；都没有时给空 schema（无参工具照样能用），不为字段名让整台 server 挂掉。
    """
    for attr in ("input_schema", "inputSchema"):
        v = getattr(t, attr, None)
        if v:
            return v
    return dict(_EMPTY_SCHEMA)


def _decode_best(data: bytes) -> str:
    """server stderr 字节流尽力解码：node 输出多为 UTF-8，Windows 系统/cmd 错误多为本地编码
    （中文 = GBK/cp936）。先试 UTF-8，失败退回本地编码，再兜底 replace——避免 GBK 字节当 UTF-8 读成乱码。"""
    if not data:
        return ""
    import locale
    # **顺序要点：GBK 必须排在本地代码页前面**。单字节代码页（英文 Windows 的 cp1252 等）
    # 对绝大多数字节序列都能"成功"解码，只是解成乱码——排在前面就等于吞掉一切，GBK 分支永远到不了
    # （2026-08-13 CI 实测：英文 runner 上 GBK 的"系统找不到指定的路径。"被 cp1252 解成一串乱码）。
    # GBK 是会校验的多字节编码（非法尾字节直接抛），先试它更安全。
    # 代价：西文 locale 下少量带重音的本地错误文案可能被 GBK 蒙对成中文乱码；但 MCP server（多为 node）
    # 输出压倒性是 UTF-8、已被第一个分支拦下，本地代码页只服务系统级错误，这个残余风险小于原来的必错。
    for enc in ("utf-8", "gbk", locale.getpreferredencoding(False)):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _flatten_exc(e) -> str:
    """拆开 ExceptionGroup（anyio/asyncio TaskGroup 会把真正的子异常包起来、信息变成
    'unhandled errors in a TaskGroup'），递归取叶子异常，拼成给人看的简短原因。"""
    leaves: list[str] = []

    def walk(x):
        subs = getattr(x, "exceptions", None)
        if subs:                       # ExceptionGroup / BaseExceptionGroup
            for s in subs:
                walk(s)
        else:
            msg = f"{type(x).__name__}: {x}".strip()
            if msg not in leaves:
                leaves.append(msg)

    walk(e)
    return "；".join(leaves) if leaves else f"{type(e).__name__}: {e}"


class McpManager:
    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self._loop = None
        self._thread: threading.Thread | None = None
        self._stop_event = None              # asyncio.Event（在 loop 上创建）
        self._sessions: dict = {}            # server -> ClientSession
        self._tools: list[McpTool] = []
        self._errors: dict[str, str] = {}    # server -> 连接失败原因（拆开 ExceptionGroup，供 GUI 显示）
        self._errbufs: dict = {}             # server -> StringIO，捕获子进程 stderr（真正崩因，如"目录不存在"）
        # agent 型 server 的续话记忆：模型漏带 thread id 时自动补，避免**静默新开会话**
        self._threads = ThreadMemory()
        # 自定义事件 → 实时流的归属表。**按 requestId 绑定**：子 Agent 可能并发调同一个
        # server，靠"当前那次"猜必然串台。首条事件到达时，若只有一次调用还没绑定，
        # 就认定是它（并发同时起跑的极端情况下宁可不显示，也不要张冠李戴）。
        self._ev_lock = threading.Lock()
        self._ev_pending: dict = {}          # server -> [stream 回调]
        self._ev_bound: dict = {}            # (server, requestId) -> stream 回调

    @property
    def errors(self) -> dict:
        """各 server 最近一次连接失败的原因（连上的不在此）。"""
        return dict(self._errors)

    # ---- 生命周期 -------------------------------------------------------
    def start(self) -> list[McpTool]:
        """启动后台 loop、连接所有启用的 server，返回收集到的工具列表。

        单个 server 失败只记录并跳过，不抛、不影响其它 server 与整个 app。
        """
        if not self.config.enabled:
            return []
        servers = {n: s for n, s in self.config.servers.items() if s.enabled}
        if not servers:
            return []
        try:
            import mcp  # noqa: F401  # 未装 SDK 时一次性友好跳过，而非让每个 server 卡到超时
        except ImportError:
            print("  [MCP] 已配置 server，但未安装 mcp SDK（pip install mcp），已全部跳过。",
                  file=sys.stderr, flush=True)
            return []
        self._start_loop()
        self._errors = {}
        self._cleanup_errbufs()   # 清掉上次连接残留的 stderr 临时文件
        for name, sc in servers.items():
            try:
                tools = self._launch_server(name, sc)
                self._tools.extend(tools)
                self._errors.pop(name, None)
                print(f"  [MCP] 已连接 {name}：{len(tools)} 个工具", file=sys.stderr, flush=True)
            except TimeoutError:  # 连接超时——首次 npx 下载 server 包常超时（不是配置错）
                msg = (f"连接超时（>{self.config.connect_timeout:.0f}s）。首次连接时 npx/uvx 要先下载 server 包、"
                       "可能较慢；等十几秒让它下完，再点该 server 的「启用」开关重试一次通常就连上了。")
                self._errors[name] = msg
                print(f"  [MCP] 连接 {name} 失败，已跳过：{msg}", file=sys.stderr, flush=True)
            except Exception as e:  # noqa: BLE001
                msg = _flatten_exc(e)   # 拆开 ExceptionGroup 露出真异常（TaskGroup 会把它藏住）
                tail = ""
                buf = self._errbufs.get(name)
                if buf is not None:
                    try:
                        buf.flush()                       # 按字节读 + 智能解码（GBK 字节别当 UTF-8 读成乱码）
                        with open(buf.name, "rb") as fh:
                            tail = _decode_best(fh.read()).strip()
                    except Exception:  # noqa: BLE001
                        tail = ""
                if tail:                # server 自己打印的崩因（如"目录不存在"）比 "Connection closed" 有用得多
                    head = " ".join(tail.splitlines())[:280]   # 取头部人话错误（首行通常就是原因）
                    msg = f"{msg}｜server 说：{head}"
                self._errors[name] = msg
                print(f"  [MCP] 连接 {name} 失败，已跳过：{msg}", file=sys.stderr, flush=True)
        return self._tools

    def _start_loop(self) -> None:
        import asyncio
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        # asyncio.Event 必须在 loop 上创建，才能与该 loop 绑定
        self._stop_event = self._submit(self._mk_event()).result(timeout=5)

    async def _mk_event(self):
        import asyncio
        return asyncio.Event()

    def _submit(self, coro):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _launch_server(self, name: str, sc: McpServerConfig) -> list[McpTool]:
        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._submit(self._serve(name, sc, ready))
        return ready.result(timeout=self.config.connect_timeout)  # 阻塞到连上 / 失败

    async def _serve(self, name: str, sc: McpServerConfig, ready: concurrent.futures.Future) -> None:
        import inspect as _inspect
        import tempfile as _tf
        # 用真临时文件捕获 server 子进程 stderr（stdio_client 在 OS 层重定向、需真 fileno，StringIO 不行）。
        errfile = None
        try:
            errfile = _tf.NamedTemporaryFile(mode="w+", encoding="utf-8", errors="replace",
                                             delete=False, suffix=".mcperr")
            self._errbufs[name] = errfile
        except Exception:  # noqa: BLE001
            errfile = None
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=sc.command, args=list(sc.args),
                env=(sc.env or None), cwd=sc.cwd,
            )
            # errlog 捕获 server stderr（真正崩因在这，如"目录不存在"）；老版本 SDK 没此参数则跳过
            _kw = ({"errlog": errfile} if errfile is not None
                   and "errlog" in _inspect.signature(stdio_client).parameters else {})
            bindings = self._event_bindings(name)
            async with stdio_client(params, **_kw) as (read, write):
                async with ClientSession(read, write, **bindings) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._sessions[name] = session
                    tools = [
                        McpTool(
                            server=name, tool_name=t.name,
                            description=t.description or "",
                            input_schema=tool_input_schema(t),
                            caller=self.call, trusted=sc.trust,   # call(server, tool, params, stream)
                            always_confirm=getattr(sc, "always_confirm", False),
                            threads=self._threads,
                        )
                        for t in listed.tools
                    ]
                    if not ready.done():
                        ready.set_result(tools)
                    await self._stop_event.wait()  # 保活直到 close()
        except Exception as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e)
        finally:
            self._sessions.pop(name, None)

    # ---- 调用（同步入口，供 McpTool.run 用） -----------------------------
    def call(self, server: str, tool_name: str, params: dict, stream=None):
        """调用一个工具。`stream(kind, delta)` 非空时，把 server 的**过程通知**实时推出去。

        MCP 的进度是**按调用**走的（客户端给 progressToken、server 回 notifications/progress），
        所以能准确归属到这一次调用，不会串台。老 SDK 没有 `progress_callback` 参数就自动跳过，
        行为退回原样（拿不到过程，但不影响调用本身）。
        """
        if self._loop is None or server not in self._sessions:
            raise RuntimeError(f"MCP server '{server}' 未连接")
        session = self._sessions[server]
        kw = {}
        if stream is not None and self._supports_progress():
            async def _on_progress(progress, total=None, message=None):
                text = (message or "").strip()
                if not text:                      # 没文案就报个进度数字，总比什么都没有强
                    text = f"…{progress:g}" + (f"/{total:g}" if total else "")
                try:
                    stream("progress", text + "\n")
                except Exception:  # noqa: BLE001 — 推流失败绝不能影响工具调用本身
                    pass
            kw["progress_callback"] = _on_progress
        if stream is not None:
            with self._ev_lock:
                self._ev_pending.setdefault(server, []).append(stream)
        try:
            fut = self._submit(session.call_tool(tool_name, params, **kw))
            return fut.result(timeout=self.call_timeout_for(server))
        finally:
            if stream is not None:
                with self._ev_lock:
                    pend = self._ev_pending.get(server) or []
                    if stream in pend:
                        pend.remove(stream)
                    for k in [k for k, v in self._ev_bound.items() if v is stream]:
                        self._ev_bound.pop(k, None)

    def _event_bindings(self, server: str) -> dict:
        """给 `codex/event` 这类**自定义通知**挂一个绑定（SDK 的官方口子）。

        不挂的话它们会在 pydantic 那关直接失败（用户真机见到的 `Field required`），
        消息连进都进不来——**Codex 一条标准 MCP 通知都不发**，不接就等于没有过程。
        老 SDK 没有 `notification_bindings` 参数则自动跳过，行为退回原样。
        """
        try:
            import inspect

            from mcp import ClientSession
            from mcp.client.extension import NotificationBinding
            from pydantic import BaseModel, ConfigDict
            if "notification_bindings" not in inspect.signature(ClientSession.__init__).parameters:
                return {}
        except Exception:  # noqa: BLE001
            return {}

        class _AnyParams(BaseModel):
            """宽松载荷：**不照着某个版本的事件字段建模**——上游一改字段就会全线失效，
            而这条通道的价值恰恰是"尽量把过程透出来"。校验交给 render_event 做容错。"""
            model_config = ConfigDict(extra="allow")

        async def _handler(params) -> None:
            raw = params.model_dump(by_alias=True) if hasattr(params, "model_dump") else params
            text = render_event(raw.get("msg") if isinstance(raw, dict) else None)
            if text:
                self._dispatch_event(server, event_request_id(raw), text)

        return {"notification_bindings": [
            NotificationBinding(method=CODEX_EVENT, params_type=_AnyParams, handler=_handler)]}

    def _dispatch_event(self, server: str, rid, text: str) -> None:
        """把一条事件文本投给**发起它的那次调用**的实时流。"""
        with self._ev_lock:
            cb = self._ev_bound.get((server, rid))
            if cb is None:
                pend = self._ev_pending.get(server) or []
                if rid is None or len(pend) != 1:
                    return          # 认不出归属：宁可不显示，也不要挂到别人的调用上
                cb = pend[0]
                self._ev_bound[(server, rid)] = cb
        try:
            cb("codex", text)
        except Exception:  # noqa: BLE001 — 推流失败绝不能影响工具调用本身
            pass

    @staticmethod
    def _supports_progress() -> bool:
        """装的 mcp SDK 认不认 `progress_callback`（pyproject 只写 mcp>=1.2，新旧机器都可能遇到）。"""
        try:
            import inspect

            from mcp import ClientSession
            return "progress_callback" in inspect.signature(ClientSession.call_tool).parameters
        except Exception:  # noqa: BLE001
            return False

    def call_timeout_for(self, server: str) -> float:
        """该 server 的单次调用超时：server 级覆盖优先，否则跟随全局（纯逻辑）。

        agent 型 server（如 `codex mcp-server`）一次调用＝跑完一整个会话，分钟级；
        而全局默认是按"一次工具调用"定的 60s。**超时该按 server 定**，
        不该为了一个慢 server 把所有 server 一起放松。
        """
        sc = self.config.servers.get(server)
        override = getattr(sc, "call_timeout", None) if sc else None
        return float(override) if override else float(self.config.call_timeout)

    # ---- 关闭 -----------------------------------------------------------
    def _cleanup_errbufs(self) -> None:
        """关掉并删除捕获 server stderr 的临时文件。"""
        import os as _os
        for buf in list(self._errbufs.values()):
            try:
                buf.close()
                _os.unlink(buf.name)
            except Exception:  # noqa: BLE001
                pass
        self._errbufs.clear()

    def close(self) -> None:
        if self._loop is None:
            self._cleanup_errbufs()
            return
        # 置位 stop_event：各 _serve 在自己的 task 里退出 async-with（cancel scope 安全）
        if self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
            time.sleep(0.3)  # 给子进程一点收尾时间
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._loop = None
        self._sessions.clear()
        self._cleanup_errbufs()
