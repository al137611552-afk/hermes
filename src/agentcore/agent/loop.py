"""Agent 主循环：plan → act → observe。

每一轮调用 provider 跑一次模型；若模型要求调用工具，则（危险工具过权限
gate 后）执行，把 tool_result 回灌，再进入下一轮；直到模型不再调用工具或
达到 max_steps 上限。

事件通过注入的 emit(event, data) 回调推给上层（bridge -> 前端）：
- chunk        文本增量（data: str）
- tool_use     模型发起工具调用（data: {id, name, input}）
- tool_result  工具执行结果（data: {id, name, ok, output}）
- error        出错（data: str）
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..providers import BaseProvider, Message
from ..tools import ToolError, ToolOutput, ToolRegistry
from .contract import NUDGE_BROWSE, NUDGE_LOGIN, NUDGE_STUCK, Need
from .gate import PermissionGate

# 工具被用户拒绝时回灌给模型的提示
_DENIED = "用户拒绝了本次操作。请不要重试该操作，可改用其它方式或询问用户。"

# 情境自启（反复改不好 → 提示用 trace_run）：编辑类工具 + 失败信号标记
_EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit"})
_FAIL_MARKERS = ("Traceback", "AssertionError", "FAILED", "未通过", "失败",
                 "Error:", "error:", "Exception", "🧪")


def looks_failing(text: str) -> bool:
    """工具输出是否含失败信号（纯逻辑、启发式）。"""
    return any(m in (text or "") for m in _FAIL_MARKERS)


# 浏览类只读工具（逐个看文件/检索）——用于"大库里浏览太多还没用 search_code"的检测
_BROWSE_TOOLS = frozenset({"read_file", "list_dir", "grep_search", "glob_search", "code_outline"})
_BROWSE_NUDGE_AT = 6   # 大库里累计浏览这么多次还没用 search_code，就提示一次

# ── Need → 注入文案（块 A：单一"差距→注入"选择点，见 docs/adr/0014）──────────
#
# loop.py 的三个情境探测器负责从工具调用里**探测事实**并归到一个 Need；具体
# 「提示什么」统一由这里按 Need 选择。这样判断（探测）与做法（注入文案）分开，
# 后续要改某个 Need 的应对、或让 Policy 接管，只动这一处。文案与重构前逐字一致，
# 故行为等价。
def _nudge_injection(need: Need, **ctx) -> str:
    """按 Need 选注入文案（纯函数）。ctx 提供 PROGRESS_STALLED 所需的 path/count。"""
    if need is NUDGE_LOGIN:
        return ("[系统] 刚打开的页面是**登录墙**（需要登录才能看内容）。**必须**用 ask_user 工具暂停、"
                "提示用户在弹出的浏览器里登录后回复『继续』，等回复了再 browser_navigate 重开目标页继续。"
                "**严禁** browser_navigate 到 google / baidu / bing 等搜索引擎绕开登录——那不是用户要的、会被判为绕路。")
    if need is NUDGE_BROWSE:
        return (
            "[系统观察] 你已经逐个浏览了不少文件来找代码——这个项目较大，"
            "用 **search_code** 给一句意图描述（如「处理 X 的地方」「鉴权逻辑」）能一次拉到最相关的几段、"
            "比逐个 list/read/grep 省很多步。先 search_code 定位、再 read_file 看细节。"
        )
    if need is NUDGE_STUCK:
        return (
            f"[系统观察] 你已经第 {ctx['count']} 次修改 `{ctx['path']}`、而它仍在失败——"
            "反复改同一处通常是**没定位准、在盲改**。先停下别再猜：用 **trace_run** 跑一段调用相关函数的"
            "驱动代码，直接看每一步的中间值，定位到底是哪一步 / 哪个值算错了，再针对性修。"
        )
    raise ValueError(f"无对应注入文案的 Need: {need}")


# 登录墙检测：浏览器结果里这些**强信号**才判为"被登录墙挡住"（避免误伤"页头有个登录按钮但正文可读"的页）
_LOGIN_WALL_RE = __import__("re").compile(
    r"请先?登[录入]|登[录入]后(?:查看|继续|可见)|需要登[录入]|扫码登[录入]|未登[录入]|"
    r"(?:please |you (?:need|must) to? )?(?:sign|log)\s?in(?: to (?:continue|view|see))?|"
    r"login required|/login|/signin|/passport|accounts?\.\w+/(?:login|signin)", __import__("re").I)


def detect_login_wall(calls, out_by_id: dict, state: dict) -> "str | None":
    """浏览器穿透下，某次 browser_* 结果像登录墙时，返回一条强制指令（纯逻辑，每轮最多提一次）：
    必须用 ask_user 让用户登录、禁止换 google/baidu 等搜索引擎绕开。"""
    if state.get("nudged"):
        return None
    for c in calls:
        name = getattr(c, "name", "")
        if name.split("__", 1)[-1].startswith("browser_"):
            if _LOGIN_WALL_RE.search(str(out_by_id.get(getattr(c, "id", None), ""))):
                state["nudged"] = True
                return _nudge_injection(NUDGE_LOGIN)
    return None


def detect_browse_nudge(calls, state: dict, enabled: bool, search_available: bool) -> "str | None":
    """检测「大项目里逐个浏览很多文件、却没用 search_code」，提示按意图检索（纯逻辑，原地更新 state）。

    state: {"browse": int, "used_search": bool, "nudged": bool}。每轮对话只提示一次。
    """
    if not enabled or not search_available or state.get("nudged"):
        # 仍要记录 used_search，避免关掉再开时误判；但不提示
        for c in calls:
            if getattr(c, "name", "") == "search_code":
                state["used_search"] = True
        return None
    for c in calls:
        name = getattr(c, "name", "")
        if name == "search_code":
            state["used_search"] = True
        elif name in _BROWSE_TOOLS:
            state["browse"] = state.get("browse", 0) + 1
    if not state.get("used_search") and state.get("browse", 0) >= _BROWSE_NUDGE_AT:
        state["nudged"] = True
        return _nudge_injection(NUDGE_BROWSE)
    return None


def detect_stuck_edit(calls, out_by_id: dict, edit_counts: dict, nudged: set,
                      threshold: int, trace_available: bool) -> "str | None":
    """检测「同一文件反复改且仍在失败」，返回一次性提示文本（纯逻辑，原地更新 edit_counts/nudged）。

    触发条件：某编辑类工具命中同一 path 累计 ≥ threshold 次，且**本步有失败信号**，且该 path 还没提示过，
    且环境里有 trace_run 可用。每个 path 只提示一次，避免反复打扰。
    """
    if threshold <= 0 or not trace_available:
        return None
    step_failing = any(looks_failing(str(v)) for v in out_by_id.values())
    for c in calls:
        if getattr(c, "name", "") not in _EDIT_TOOLS:
            continue
        path = (getattr(c, "input", None) or {}).get("path", "")
        if not path:
            continue
        edit_counts[path] = edit_counts.get(path, 0) + 1
        if edit_counts[path] >= threshold and step_failing and path not in nudged:
            nudged.add(path)
            return _nudge_injection(NUDGE_STUCK, path=path, count=edit_counts[path])
    return None


class AgentLoop:
    def __init__(
        self,
        provider: BaseProvider,
        registry: ToolRegistry,
        gate: PermissionGate,
        *,
        max_steps: int = 25,
        hook_runner=None,
        stuck_threshold: int = 0,
        browse_nudge: bool = False,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.gate = gate
        self.max_steps = max_steps
        self.hook_runner = hook_runner  # 可编程 hooks（PreToolUse/PostToolUse）；None=无
        self.stuck_threshold = stuck_threshold  # 情境自启：反复改同一文件失败→提示 trace_run；0=关
        self.browse_nudge = browse_nudge        # 情境自启：大库里浏览太多→提示 search_code（按工作区规模启用）

    def run(
        self,
        messages: list[Message],
        system: str | None,
        emit: Callable[[str, object], None],
        cancel: threading.Event | None = None,
        take_injects=None,
    ) -> list[Message]:
        """跑完一整轮对话（可能含多步工具调用）。

        messages 会被原地追加 assistant 与 tool_result 消息；返回同一列表，
        供 bridge 写回会话历史。

        cancel：可选的取消标志，在每一回合开始前检查；置位则立即停止后续回合
        （不打断当前回合内已在进行的模型流，回合间生效，见 FR-8.3）。

        take_injects：可选 `() -> list[str]` 回调，返回并清空"用户在执行中追加的补充消息"
        （steering）。每次工具往返回灌时拉取，把补充附进同一条 user 消息——模型下一轮即看到
        「工具结果 + 用户补充」，可据此重新评估、调整当前任务方向，而非等任务做完再当新事处理。
        """
        tools = self.registry.to_schemas()
        # 用量累计（FR-11.8）：跨步累加 token，记步数，回合末发 usage 事件。
        total = {"input": 0, "output": 0, "cache_read": 0}
        steps = 0
        warned = False
        self.hit_max_steps = False   # 本轮是否撞步数上限（供委派标注"子任务未完成"）
        edit_counts: dict[str, int] = {}  # 情境自启：本轮各文件被编辑次数
        nudged: set[str] = set()          # 已提示过 trace_run 的文件（每文件只提一次）
        browse_state: dict = {}           # 情境自启：浏览计数 / 是否用过 search_code / 是否已提示
        login_state: dict = {}            # 登录墙：本轮是否已强制提示过"用 ask_user 登录、别换搜索引擎"
        _names = self.registry.names() if hasattr(self.registry, "names") else []
        browser_present = any(n.split("__", 1)[-1].startswith("browser_") for n in _names)

        for _ in range(self.max_steps):
            if cancel is not None and cancel.is_set():
                break  # 收到取消：停在回合边界，已追加的消息照常返回/落库
            steps += 1
            # 步数接近上限时预警一次（长任务"在推进还是打转"可感知）
            if not warned and self.max_steps >= 5 and steps >= int(self.max_steps * 0.8):
                warned = True
                emit("step_warning", {"steps": steps, "max_steps": self.max_steps})
            assistant_text = ""
            calls = []
            errored = False
            cancelled = False
            stop_reason = None

            for ev in self.provider.stream_chat(messages, system=system, tools=tools):
                if cancel is not None and cancel.is_set():  # 立即响应停止：中断流式（对标主流，不等回合结束）
                    cancelled = True
                    break
                if ev.type == "text":
                    assistant_text += ev.text
                    emit("chunk", ev.text)
                elif ev.type == "thinking":
                    emit("thinking", ev.text)  # 仅展示，不计入答案、不持久化
                elif ev.type == "tool_use":
                    calls.append(ev.meta["call"])
                elif ev.type == "error":
                    emit("error", ev.text)
                    errored = True
                    break
                elif ev.type == "done":
                    stop_reason = ev.meta.get("stop_reason")
                    u = ev.meta.get("usage")
                    if u:
                        for k in total:
                            total[k] += u.get(k, 0) or 0
                    break

            if cancelled:  # 流式被停止打断：保留已输出的部分文本，不执行本轮残缺工具调用
                if assistant_text.strip():
                    messages.append(Message("assistant", assistant_text))
                break
            if errored:
                break

            # 输出撞到 max_tokens 上限被截断：此时 tool_use 的入参（如 write_file 的
            # content）很可能不完整，执行它会写出空/残缺文件，模型见状又重试 -> 死循环。
            # 故记下已生成文本、明确报错并停止，不执行被截断的工具调用。
            if stop_reason in ("max_tokens", "length"):
                if assistant_text.strip():
                    messages.append(Message("assistant", assistant_text))
                emit("error",
                     f"模型输出达到 max_tokens 上限被截断（stop_reason={stop_reason}），"
                     "已停止以避免执行不完整的工具调用（如写出空文件）。"
                     "请在 config.yaml 调高该模型的 max_tokens，或让它分步/分块写入。")
                break

            if not calls:
                # 模型不再调用工具：本轮结束
                messages.append(Message("assistant", assistant_text))
                break

            # 1) 记录 assistant 这轮的 text + tool_use blocks
            blocks: list[dict] = []
            if assistant_text.strip():
                blocks.append({"type": "text", "text": assistant_text})
            for c in calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
            messages.append(Message("assistant", blocks))

            # 2) 执行工具，收集 tool_result blocks（按原调用顺序组装回灌）。
            #    同回合多个 parallel_safe 工具（目前=delegate）并发执行（FR-10.5）；
            #    富内容块（如截图 image）单独收集，作为并列块追加到同一条 user 消息
            #    （部分端点不解析 tool_result 内嵌图片，见 ToolOutput / ADR-0010）。
            results, extra_blocks = self._exec_calls(calls, emit)

            # 3) tool_result（+ 富内容并列块）作为 user 消息回灌，进入下一轮。
            #    若用户在执行中追加了补充（steering），附进**同一条** user 消息——既让模型下一轮
            #    立刻看到「工具结果 + 用户补充」并据此调整，又不破坏 user/assistant 交替。
            inject_blocks: list[dict] = []
            if take_injects is not None:
                for t in take_injects():
                    if t and t.strip():
                        inject_blocks.append({"type": "text", "text": f"[用户追加] {t.strip()}"})
            # 情境自启：反复改同一文件且仍失败 → 自动提示用 trace_run 看证据（不再盲改）
            if self.stuck_threshold > 0:
                out_by_id = {r["tool_use_id"]: r.get("content", "") for r in results}
                nudge = detect_stuck_edit(
                    calls, out_by_id, edit_counts, nudged,
                    self.stuck_threshold, "trace_run" in self.registry.names())
                if nudge:
                    inject_blocks.append({"type": "text", "text": nudge})
                    emit("stuck_hint", {"text": nudge})
            # 情境自启：大库里逐个浏览很多文件还没用 search_code → 提示按意图检索
            if self.browse_nudge:
                bn = detect_browse_nudge(calls, browse_state, True,
                                         "search_code" in self.registry.names())
                if bn:
                    inject_blocks.append({"type": "text", "text": bn})
                    emit("search_hint", {"text": bn})
            # 浏览器穿透下撞登录墙 → 当场强制注入：必须 ask_user 让用户登录，禁止换搜索引擎绕开
            # （静态 directive 压不住"绕去 google/baidu"，关键时刻硬怼更可靠）
            if browser_present:
                out_by_id = {r["tool_use_id"]: r.get("content", "") for r in results}
                lw = detect_login_wall(calls, out_by_id, login_state)
                if lw:
                    inject_blocks.append({"type": "text", "text": lw})
                    emit("login_hint", {"text": lw})
            messages.append(Message("user", results + extra_blocks + inject_blocks))
        else:
            self.hit_max_steps = True
            # 撞步数上限：强制收尾一轮——禁用工具，让模型基于已收集信息立即给出总结/结论。
            # 否则 messages 最后一条是 tool_result、无任何文本产出，委派子任务回灌空摘要（FR-9.3）、
            # 长任务也只能裸退。把收尾指令并入最后那条 user 消息（撞上限时它一定是 tool_result），
            # 避免两条连续 user 破坏交替。
            if cancel is None or not cancel.is_set():
                hint = {"type": "text", "text": (
                    "[系统] 已达到步数上限，不能再调用任何工具。请立即基于上面已经收集到的信息，"
                    "给出尽可能有用的总结/结论：包含已获得的关键数据/发现，以及尚未完成的部分。不要再请求工具。"
                )}
                last = messages[-1] if messages else None
                if last is not None and last.role == "user" and isinstance(last.content, list):
                    last.content.append(hint)
                else:
                    messages.append(Message("user", [hint]))
                final_text = ""
                try:
                    for ev in self.provider.stream_chat(messages, system=system, tools=[]):
                        if ev.type == "text":
                            final_text += ev.text
                            emit("chunk", ev.text)
                        elif ev.type == "done":
                            u = ev.meta.get("usage")
                            if u:
                                for k in total:
                                    total[k] += u.get(k, 0) or 0
                            break
                        elif ev.type == "error":
                            break
                except Exception:  # noqa: BLE001 — 收尾失败不影响已有结果返回
                    final_text = ""
                if final_text.strip():
                    messages.append(Message("assistant", final_text))
            emit("error", f"已达到最大步数上限（{self.max_steps}），已基于已收集信息收尾。")

        # 回合末上报用量（FR-11.8）：tokens 全 0（端点没回传用量）则不发，避免噪音
        if total["input"] or total["output"]:
            emit("usage", {**total, "steps": steps, "max_steps": self.max_steps})
        return messages

    _PARALLEL_CAP = 4  # 同回合并发执行的 parallel_safe 调用上限（FR-10.5）

    def _exec_calls(self, calls, emit) -> tuple[list[dict], list[dict]]:
        """执行一个回合内的全部工具调用，返回 (tool_result 块, 富内容并列块)，均按原调用顺序。

        同回合出现 ≥2 个 parallel_safe 工具调用（目前只有 delegate）时丢进线程池并发跑
        （对标 Claude Code 一轮发多个 Task），其余工具保持原有的顺序执行语义。
        gate/emit/记忆库/进程表均已线程安全；子任务事件带 sub_id，前端多块并存。
        """
        parallel_ids: set[str] = set()
        if len(calls) > 1:
            for c in calls:
                try:
                    if getattr(self.registry.get(c.name), "parallel_safe", False):
                        parallel_ids.add(c.id)
                except ToolError:
                    pass
            if len(parallel_ids) < 2:  # 只有一个可并行的：没有并发收益，走顺序路径
                parallel_ids.clear()

        outputs: dict[str, tuple[str, bool, list[dict]]] = {}
        futures: dict[str, object] = {}
        executor = None
        if parallel_ids:
            executor = ThreadPoolExecutor(max_workers=min(self._PARALLEL_CAP, len(parallel_ids)))
            for c in calls:
                if c.id in parallel_ids:
                    emit("tool_use", {"id": c.id, "name": c.name, "input": c.input})
                    futures[c.id] = executor.submit(self._exec_tool, c.name, c.input)
        try:
            for c in calls:  # 串行组照旧（与并行组并发进行）
                if c.id in futures:
                    continue
                emit("tool_use", {"id": c.id, "name": c.name, "input": c.input})
                outputs[c.id] = self._exec_tool(c.name, c.input)
                self._emit_result(emit, c, outputs[c.id])
            for c in calls:  # 收并行组结果（按原序等待/上报）
                if c.id in futures:
                    outputs[c.id] = futures[c.id].result()
                    self._emit_result(emit, c, outputs[c.id])
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        results: list[dict] = []
        extra_blocks: list[dict] = []
        for c in calls:
            output, _ok, blocks = outputs[c.id]
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": output})
            # diff 块仅供前端内联展示，不作并列块回灌模型（模型已知改了什么，回灌冗余+耗 token）
            extra_blocks.extend(b for b in blocks if b.get("type") != "diff")
        return results, extra_blocks

    @staticmethod
    def _emit_result(emit, call, out: tuple[str, bool, list[dict]]) -> None:
        output, ok, blocks = out
        ev = {"id": call.id, "name": call.name, "ok": ok, "output": output}
        img = next((b for b in blocks if b.get("type") == "image"), None)
        if img:  # 给前端一张缩略图
            src = img["source"]
            ev["image"] = f"data:{src['media_type']};base64,{src['data']}"
        d = next((b for b in blocks if b.get("type") == "diff"), None)
        if d:  # 写/编辑的本次 diff：内联展示在对话流（仅前端，不回灌模型）
            ev["diff"] = {"path": d["path"], "text": d["diff"]}
        emit("tool_result", ev)

    def _exec_tool(self, name: str, params: dict) -> tuple[str, bool, list[dict]]:
        """执行单个工具，返回 (结果文本, 是否成功, 额外内容块)。危险工具先过权限 gate。

        普通工具返回 str -> 额外块为空；返回 ToolOutput 的工具（如截屏）-> 带 image 块。
        """
        try:
            tool = self.registry.get(name)
        except ToolError as e:
            return str(e), False, []

        # PreToolUse hooks（程序化守卫）：可拦截（退出码 2）或放行+警告（退出码 1）。
        pre_warn = None
        if self.hook_runner is not None:
            allowed, msg = self.hook_runner.pre(name, params)
            if not allowed:
                return (msg or "操作被 PreToolUse hook 拦截。"), False, []
            pre_warn = msg

        if tool.dangerous and not self.gate.confirm(name, params):
            return _DENIED, False, []

        try:
            out = tool.run(params)
        except ToolError as e:
            return str(e), False, []
        except Exception as e:  # noqa: BLE001 — 工具内部异常也回灌给模型
            return f"工具执行异常：{type(e).__name__}: {e}", False, []

        text, blocks = (out.text, out.blocks) if isinstance(out, ToolOutput) else (out, [])
        # PostToolUse hooks：把 hook stdout 追加到结果回灌模型（如 linter 诊断）。
        if self.hook_runner is not None:
            post = self.hook_runner.post(name, params, text if isinstance(text, str) else str(text))
            if post:
                text = f"{text}\n{post}"
        if pre_warn:  # 放行但带警告：警告并入结果
            text = f"{text}\n{pre_warn}"
        return text, True, blocks
