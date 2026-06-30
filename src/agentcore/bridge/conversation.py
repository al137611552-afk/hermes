"""单个对话的运行时（FR-8.1）。

把"每对话私有状态 + 操作这些状态的逻辑"从 `Api` 单例里抽出来，使一个进程能持有
多个独立对话——这是 P8 并发对话的结构性地基。本阶段（8.1）仍保持「单活动对话、
同步执行」语义，对外行为与 1.0.0 完全一致；真正的后台并发与事件路由留到 8.2。

- `Resources`：跨对话共享的资源与账本（config / store / memory / mcp / limits /
  workspaces_root / per_session / emit 回调），以及按 session_id 记账、需跨对话存活的
  `extracted_upto`（记忆自动抽取进度）与 `conv_attempted`（已尝试生成规范的会话）。
  由 `Api` 构造一份，注入给每个 `Conversation`。
- `Conversation`：持 session_id / history / workspace / registry / gate / active_model /
  pending_workspace，承载发消息主循环、上下文预算、长期记忆抽取、视觉预处理、
  自动生成项目规范、工作区切换与只读预览。
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

from ..agent import AgentLoop, PermissionGate
from ..agent.contract import verdict_to_need
from .. import checkpoints as ckpt
from ..changes import ChangeLedger
from ..config import AppConfig
from ..context import build_summary_request, compress
from ..conventions import build_generate_request, build_project_digest, clean_output
from .. import gitsupport
from ..longmem import (
    build_consolidate_request,
    build_extract_request,
    build_memory_block,
    build_transcript,
    parse_memories,
)
from ..multimodal import Limits, build_user_content, describe_image, preprocess_vision
from ..providers import Message, build_provider
from ..store import make_title
from ..tools import build_registry
from ..tools.ask import AskUserBinding
from ..tools.delegate import (
    _BROWSE_TOOLS,
    _READ_ONLY_TOOLS,
    SUBAGENT_DIRECTIVE,
    DelegateBinding,
    build_grader_prompt,
    build_roles,
    compose_task,
    extract_summary,
    parse_grade,
    resolve_role,
    summarize_activity,
)

# 规划模式（FR-11.5）：只读勘察工具 + 写计划用的 update_tasks/notes（其余写/命令/委派全屏蔽）
_PLAN_TOOLS = _READ_ONLY_TOOLS | {"update_tasks", "update_notes", "ask_user"}
_PLAN_DIRECTIVE = (
    "[规划模式] 你现在处于**只读规划模式**：只能勘察（读文件/检索/查资料）并用 update_tasks、"
    "update_notes 产出方案——**修改文件、执行命令、委派等写操作的工具已被禁用**。先把现状摸清楚，"
    "然后给出**清晰、结构化**的方案（不要堆大段叙述文字）：\n"
    "1. **用 update_tasks 列出有序实施步骤**——每步一个任务、简洁可执行，这是规划的主体；\n"
    "2. **在回复里附一张 mermaid 图**直观呈现结构：任务分解/模块拆解用 `mindmap`，有先后顺序或分支的"
    "执行流程用 `flowchart TD`——按任务性质自己选最合适的一种，节点文字精炼；\n"
    "3. 关键决定与取舍（为什么这么选、备选）写进 update_notes。\n"
    "**过程中凡遇到需要用户拍板的方向性取舍（技术栈/范围/风格等二选一），用 ask_user 给 2~4 个选项让用户勾选，"
    "别只把选择题写进文字里让用户打字答。**\n"
    "产出后停下等用户确认，不要假装已动手；用户关闭规划模式后你再按计划执行。"
)

# 自主 / crazy 模式（无人值守）：自动写目标 + 外层循环干到底，免确认、免问用户
_CRAZY_DIRECTIVE = (
    "[自主模式 / CRAZY] 你现在**无人值守自主工作**，没有用户可以问、也没人会回答。要求：\n"
    "1. 先把目标拆成**有序阶段**（P1 / P2 / P3…）：每个阶段写明【这阶段要达成什么 + 怎么验收（尽量是"
    "**能跑的测试**或可检查的产物，而非'写完了'这种主观判断）】，用 update_tasks 建成**阶段清单**——这是施工蓝图，"
    "先建清单再动手；阶段不必一开始就完美，后面可按进展调整/细化；\n"
    "2. 然后**一个阶段一个阶段推进**（别一上来铺开所有阶段并行硬干）：当前阶段动手→用工具真正执行→"
    "**跑该阶段的验收（测试）确认达成**，绿了再进下一阶段；**别停在规划、别只给方案**，要干到底；\n"
    "3. **任务由多个相对独立的子系统/模块组成时，用 delegate 把它们拆给子 agent 并行开发**"
    "（如解析器/存储引擎/CLI 各一块），再自己负责集成联调——别什么都自己串行硬扛，独立的活并行更快；\n"
    "4. **阶段内的零碎决策**自己按合理默认定、别打扰用户（也别调 ask_user 工具，那在自主模式被自动放行、没用）。"
    "但遇到**真正影响方向的设计岔路 / 下一阶段目标确实模糊 / 你判断必须用户拍板**时，**那一轮最后一行输出 "
    "`[[NEED_USER: 你要问的具体问题]]`**——外层会**停下来真的问用户**、把回复带给下一轮的你。别滥用（routine 决策别问），"
    "只在真岔路用；用了就停在那轮、等回复；\n"
    "5. **跨轮记忆只有任务清单 + 工作笔记**：每一轮开始你只会看到【目标 + 你的 update_tasks 清单 + "
    "update_notes 笔记 + 已改动文件清单】，**看不到上一轮的对话过程**（Ralph 式 fresh context，省上下文、不串味）。"
    "所以你**必须随时把「已完成什么 / 关键决定 / 还差什么 / 下一步」写进 update_tasks 和 update_notes**——"
    "没写进去的，下一轮的你就彻底忘了。把它们当成留给「下一轮的自己」的交接班记录，认真维护；\n"
    "6. 每一轮结束对照**当前阶段的验收标准**自评，并在回复的**最后一行**只输出下列标记之一：\n"
    "   · **所有阶段**都完成且验收通过 → `[[DONE]]`\n"
    "   · **当前阶段刚完成并通过它的验收、但后面还有阶段没做** → `[[PHASE_DONE: 刚完成的是哪个阶段 Pn + 下一阶段要做什么]]`"
    "（外层会让你下一轮先回顾、按这阶段实际学到的**重规划剩余阶段**再继续——计划本就该随进展演化，别死守初始拆分）；\n"
    "   · 当前阶段**还没做完**（仍在阶段中途） → `[[CONTINUE: 下一步具体做什么（标明在推进哪个阶段 Pn）]]`\n"
    "   · 撞到必须用户拍板的岔路/模糊 → `[[NEED_USER: 具体问题]]`（停下问用户）\n"
    "这个标记驱动外层循环：DONE 收工（会先过验收门，测试不绿不收）、PHASE_DONE 先重规划剩余阶段再推进、"
    "CONTINUE 带下一步继续、NEED_USER 停下问用户，直到所有阶段达成或用尽预算。"
)


def _parse_crazy_verdict(text: str) -> "tuple[str | None, str]":
    """解析自主模式每轮末尾的标记 →
    ('done','') / ('phase_done', 下一阶段) / ('continue', 下一步) /
    ('need_user', 要问用户的问题) / (None,'')。
    优先级 need_user > done > phase_done > continue：撞岔路先停下问（块3）；
    全部完成（done）优先于单阶段完成（phase_done，块4 重规划）。"""
    import re
    if not text:
        return (None, "")
    m = re.search(r"\[\[\s*NEED_USER\s*[:：]\s*(.+?)\]\]", text, re.I | re.S)
    if m:
        return ("need_user", m.group(1).strip())
    if re.search(r"\[\[\s*DONE\s*\]\]", text, re.I):
        return ("done", "")
    m = re.search(r"\[\[\s*PHASE_DONE\s*[:：]\s*(.+?)\]\]", text, re.I | re.S)
    if m:
        return ("phase_done", m.group(1).strip())
    m = re.search(r"\[\[\s*CONTINUE\s*[:：]\s*(.+?)\]\]", text, re.I | re.S)
    if m:
        return ("continue", m.group(1).strip())
    return (None, "")


def _turn_used_tools(msgs) -> bool:
    """本轮新增消息里有没有工具调用（改文件/执行命令）——crazy 防空转判断用。"""
    for m in msgs:
        if getattr(m, "role", None) == "assistant" and isinstance(getattr(m, "content", None), list):
            if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m.content):
                return True
    return False
from ..tools.procs import ProcessManager
from ..tools.notes import NotesBinding, build_notes_block
from ..tools.tasks import TaskBinding, build_task_block
from ..hooks import make_hook_runner
from ..profile import (
    compute_smart_defaults, describe_smart_defaults, detect_project_profile,
)
from ..verify import is_test_file, make_post_edit_checker
from ..workspace import build_tree, read_conventions, read_file as read_workspace_file


class Resources:
    """跨对话共享的资源与账本（单实例，由 Api 持有、注入给各 Conversation）。"""

    def __init__(
        self, *, config: AppConfig, memory, mcp, mcp_tools, store, limits,
        workspaces_root: Path, per_session: bool, emit,
    ) -> None:
        self.config = config
        self.memory = memory          # MemoryStore | None（已线程锁）
        self.mcp = mcp                # McpManager（常驻后台 loop）
        self.mcp_tools = mcp_tools    # list[McpTool]
        self.store = store            # Store | None（已线程锁）
        self.limits = limits          # multimodal.Limits
        self.workspaces_root = workspaces_root
        self.per_session = per_session
        self.emit = emit              # (event: str, data) -> None
        # 跨对话、按 session_id 记账的账本（需在对话切换间存活）：
        self.extracted_upto: dict[int, int] = {}  # 各会话已自动抽取到的消息条数（仅抽取成功后才推进）
        self.capturing: set = set()               # 正在抽取的会话（防并发；与 upto 解耦，失败/超时不丢）
        self.consolidated_facts: int = 0          # 上次固化时的 fact 碎片数（防重复固化）
        self.conv_attempted: set[int] = set()      # 已尝试自动生成规范的会话 id
        self.lock = threading.Lock()               # 保护上面两个账本


_RERUN = object()  # 队列哨兵：在（已截断的）现有历史上重跑一轮，不追加新用户消息


def _message_text(m: "Message") -> str:
    """取一条消息里的纯文本（content 为 str 直接返回；为 block 列表则拼接 text 块）。"""
    c = m.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


class Conversation:
    """一个对话的私有运行时状态与逻辑。"""

    def __init__(
        self, res: Resources, *, cid: int, session_id: int | None, history: list[Message],
        workspace: Path, pending_workspace: str | None, active_model: str,
    ) -> None:
        self.res = res
        self.cid = cid  # 进程内唯一对话 id（事件路由用，独立于 session_id）
        self.session_id = session_id
        self.history = history
        self.active_model = active_model
        self._pending_workspace = pending_workspace  # 打开已有项目后、首条消息建会话时绑定的路径
        self.lock = threading.Lock()
        # 本对话发往前端的事件统一带上 cid（FR-8.2 事件路由）。
        # 直接闭包捕获 Resources.emit（即 Api._emit），避免引用 self.emit 造成自递归。
        _base_emit = res.emit
        self.emit = lambda event, data: _base_emit(event, data, self.cid)
        # 后台执行：每对话一条 worker 线程 + 串行任务队列（保回合顺序；惰性启动、空闲退出）
        self.state = "idle"  # idle / queued / running / awaiting（done/error 由轮次事件表达）
        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._cancel = threading.Event()  # 取消当前/排队任务（回合间生效，FR-8.3）
        self._stop = False                # 应用退出：让 worker 收尾退出
        # 执行中追加（steering）：用户在一轮跑动中发的纯文本补充，注入当前任务的下一个工具边界，
        # 让模型据此重估、调方向（而非等任务做完再当独立新事处理，对标 Claude Code）。
        self._inject: list[str] = []
        self._running_turn = threading.Event()  # 标记"正有一轮在跑"：供 enqueue 判注入 vs 排队
        self._sub_seq = 0                 # 子 Agent 计数（FR-9.3，前端按 id 归集子任务块）
        self._sub_lock = threading.Lock() # 并行委派时计数安全（FR-10.5）
        self.plan_mode = False            # 规划模式（FR-11.5）：只读勘察+产出方案，运行时态不持久化
        self.crazy_mode = False           # 自主/crazy 模式（无人值守外层循环），运行时态不持久化
        self._last_turn_hit_max = False   # 上一轮 send_message 是否撞步数上限（crazy 外层据此强制续命）
        self._last_turn_had_inject = False # 上一轮是否有用户中途补充（crazy 外层据此不轻信 [[DONE]]）
        # 本回合自动检查点（P12 方案A）：累加各文件"改动前内容"到同一个检查点
        self._turn_label = "改动"         # 标签（取自本回合用户消息）
        self._turn_snap: dict[str, "str | None"] = {}  # relpath -> 本回合改动前内容
        self._turn_ckpt_id: "int | None" = None        # 本回合检查点 id（首次写时建）
        self._turn_meta = None                          # (tasks, notes) 定格于回合首个改动
        # 角色表：内置 + config agent.roles 自定义（同名覆盖；含按角色配模型）
        self._roles = build_roles(res.config.agent.roles)
        # 权限 gate：每对话独立（_allow_all 即「本会话全部允许」语义）。
        # 触发确认时进入 awaiting 态，供前端在该会话行提示（不静默卡住）。
        perm = res.config.agent.permissions
        self.gate = PermissionGate(self._on_permission_request,
                                   allow=perm.allow, deny=perm.deny,
                                   # 闭包现读，🛠 面板切「智能确认分级」即时生效
                                   auto_safe=lambda: self.res.config.agent.auto_approve_safe)
        self._ask = AskUserBinding(lambda req: self.emit("ask_user", req))  # ask_user 工具的阻塞桥
        # 后台进程管理器（FR-10.3）：每对话一个、跨工作区切换保留；shutdown 时杀全部
        self.procs = ProcessManager()
        # 压缩摘要缓存（FR-10.4a）：(已覆盖的被丢弃消息条数, 模型生成的摘要文本)。
        # 切点不动直接复用（零额外调用），切点前移增量合并；失败短时退避。
        self._compact: "tuple[int, str] | None" = None
        self._compact_failed_at = 0.0
        # 情境自启②：按工作区探测出的智能默认（如有测试自动开"改完跑定向测试"）；不覆盖用户面板选择
        self._smart_defaults: dict = {}
        self._smart_ws: "str | None" = None
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._extra_dirs: list = []   # 额外授权目录（add-dir，对标 Claude Code）；工具共享此引用
        self._build_registry()

    # ---- 后台执行：入队 + worker（FR-8.2） -------------------------------

    def enqueue(self, text: str, attachments=None) -> dict:
        """把一轮发送入队、立即返回（非阻塞）；worker 线程串行消费。

        两种语义：
        - **执行中追加纯文本 = steering**：当前正有一轮在跑（含卡在权限确认）时发的纯文本补充，
          注入当前任务的下一个工具边界——模型下一轮即看到并据此重估、调整方向，而非等任务做完
          再当独立新事处理（对标 Claude Code 的 steering）。
        - **其余 = 排队成新一轮**：空闲时发的、或带附件的（图片无法塞进 tool_result）追加，
          走原有串行队列，worker 处理完当前的再起这条。
        不在此清取消标志——避免运行中入队误解除「停止」；清标志移到 worker 取出新任务时。
        """
        if (not text or not text.strip()) and not attachments:
            return {"ok": False, "error": "空消息"}
        # 调用此刻是否已有一轮在跑（snapshot）：决定这条是「追加/排队」还是「全新任务」。
        # 必须在 put + _ensure_worker 之前判断——否则新启的 worker 会抢先把 state 改成 running，
        # 令空闲时发的新消息被误判成"排队"（bug：任务已结束却提示"已排队，当前任务完成后处理"）。
        # crazy 自主模式跑在后台、直接调 send_message 不经 worker，_running_turn 不置位；
        # 但此时补充也该作为 steering 注入当前自主任务（而非另起 worker 并发跑），故并入 busy。
        busy = self._running_turn.is_set() or self.crazy_mode
        if busy and not attachments:
            with self.lock:
                self._inject.append(text)
                pending = len(self._inject)
            self.emit("enqueued", {"pending": pending, "steering": True})
            return {"ok": True, "queued": True, "steering": True}
        self._queue.put((text, attachments))
        self._ensure_worker()
        if busy:
            self.emit("enqueued", {"pending": self._queue.qsize()})  # 有任务在跑、带附件→排队成新一轮
        else:
            self.state = "queued"  # 空闲：这条就是新任务，给个 queued 过渡（worker 随即设 running）
            self.emit("state", {"state": "queued"})
        return {"ok": True, "queued": True}

    def _take_injects(self) -> list[str]:
        """供 AgentLoop 在工具回灌时拉取并清空"执行中追加的补充"（steering）。"""
        with self.lock:
            out = [t for t in self._inject if t and t.strip()]
            self._inject = []
        if out and self.crazy_mode:  # crazy：把补充明确成"任务追加需求"，别让模型当对话结束就 [[DONE]]
            self._last_turn_had_inject = True
            out = [
                ("（这是用户对当前自主任务的补充要求，请并入目标继续干到底；"
                 f"不要因为答复了这条就停下或输出 [[DONE]]，所有目标都完成才收尾）：{t}")
                for t in out
            ]
        return out

    def _drain_injects_to_queue(self) -> None:
        """任务结束时仍有未被注入消费的追加（如纯文本回答无工具往返）→ 作为新一轮排队（兜底）。"""
        with self.lock:
            leftover, self._inject = self._inject, []
        for t in leftover:
            if t and t.strip():
                self._queue.put((t, None))

    def is_busy(self) -> bool:
        return self.state in ("queued", "running", "awaiting") or not self._queue.empty()

    # ---- 权限确认 / 停止 / 收尾（FR-8.3） -------------------------------

    def _on_permission_request(self, req: dict) -> None:
        """gate 触发确认：进入 awaiting 态并把请求推给前端（带本对话 cid）。"""
        self.state = "awaiting"
        self.emit("state", {"state": "awaiting"})
        self.emit("permission_request", req)

    def resolve_permission(self, req_id: int, decision: str) -> dict:
        """前端确认条回调：唤醒等待的 confirm()，回到 running 态。"""
        ok = self.gate.resolve(int(req_id), decision)
        if ok:
            self.state = "running"
            self.emit("state", {"state": "running"})
        return {"ok": ok}

    def stop(self) -> None:
        """请求中止本对话当前运行/排队的任务（回合间生效，不打断当前回合内的模型流）。"""
        self._cancel.set()
        with self.lock:  # 停止：连待注入的执行中追加一起清掉
            self._inject = []
        # 清掉尚未开始的排队任务
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        self.gate.reset()  # 若卡在权限确认上，解除阻塞（confirm 返回 deny，回合间再停）
        self._ask.reset()  # 同时解除 ask_user 的等待

    def shutdown(self, timeout: float = 2.0) -> None:
        """应用退出/运行时被移除：停 worker、解除权限等待、等线程收尾（带超时，绝不卡死），
        并清理本对话的全部后台进程（FR-10.3，dev server 等不残留）。"""
        self._stop = True
        self._cancel.set()
        self.gate.reset()
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout)
        try:
            self.procs.kill_all()
        except Exception:  # noqa: BLE001 — 清理尽力而为
            pass

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            if self._stop:  # 应用退出：立即收尾
                with self._worker_lock:
                    self.state = "idle"
                    self._worker = None
                return
            try:
                text, attachments = self._queue.get(timeout=0.05)
            except queue.Empty:
                with self._worker_lock:
                    # 队列确实空了才退出；退出与 enqueue 的启动都在 _worker_lock 下，无丢任务
                    if self._queue.empty():
                        self.state = "idle"
                        self._worker = None
                        return
                continue
            if self._stop:  # 取出后再确认一次：退出时不再开新任务
                self._queue.task_done()
                with self._worker_lock:
                    self.state = "idle"
                    self._worker = None
                return
            self._cancel.clear()  # 取出新任务：解除上一条遗留的停止标志（排队的下一条该正常跑）
            self._running_turn.set()  # 先标记本轮在跑（在 state=running 之前），让并发 enqueue 判断准确
            self.state = "running"
            self.emit("state", {"state": "running"})
            try:
                if text is _RERUN:                       # 重新生成/编辑重发：历史已截断，直接重跑
                    self._run_turn(list(self.history))
                else:
                    self.send_message(text, attachments)  # 同步跑一轮，内部已 emit chunk/done/error
            except Exception as e:  # noqa: BLE001 — 单个任务出错不能搞死 worker
                self.emit("error", f"{type(e).__name__}: {e}")
            finally:
                self._running_turn.clear()
                self._queue.task_done()
                self._drain_injects_to_queue()  # 收尾：仍有未注入的追加→作为新一轮排队（兜底）
                if self._queue.empty():
                    self.emit("ws_settle", {})  # 空闲了：补做运行中被跳过的工作区改名（api 拦截处理）

    # ---- 发消息主循环 ----------------------------------------------------

    def send_message(self, text: str, attachments=None) -> dict:
        """同步跑一轮 agent 循环；期间通过事件推回增量与工具调用。"""
        res = self.res
        if (not text or not text.strip()) and not attachments:
            return {"ok": False, "error": "空消息"}

        content = build_user_content(text or "", attachments, res.limits)
        content = self._maybe_preprocess_vision(content, text or "")

        self._reset_turn_checkpoint(text)

        # 首条消息时建会话；用户消息立即落库
        self._ensure_session(text or "")
        with self.lock:
            self.history.append(Message("user", content))
            messages = list(self.history)  # 完整历史副本
        self._persist(Message("user", content))
        return self._run_turn(messages)

    @staticmethod
    def _first_user_text(messages: "list[Message]") -> str:
        """取消息列表里第一条 user 消息的纯文本（供 fresh 轮的记忆召回 query）。"""
        for m in messages:
            if getattr(m, "role", None) == "user":
                c = m.content
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(b.get("text", "") for b in c
                                    if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def _reset_turn_checkpoint(self, text: "str | None") -> None:
        """本回合自动检查点（P12 方案A）：重置累计快照，用用户消息做标签。"""
        self._turn_label = (text or "改动").strip().replace("\n", " ")[:40] or "改动"
        self._turn_snap = {}
        self._turn_ckpt_id = None
        self._turn_meta = None

    def _run_turn(self, messages: "list[Message]", *, fresh: bool = False) -> dict:
        """在给定历史快照（末尾应已是触发本轮的用户消息）上跑一轮 agent 循环并落库新增。
        被 send_message（追加用户消息后）与 重新生成/编辑重发（截断历史后）共用。

        fresh=True（crazy Ralph 式）：**不喂累积历史**，只把 `messages`（本轮目标+状态）原样喂模型——
        每轮 fresh context、与历史隔离、跨轮记忆靠 notes/tasks。新增消息仍落 history/DB 供前端显示。"""
        res = self.res
        try:
            provider = build_provider(res.config, self.active_model)
        except Exception as e:  # 配置/密钥错误
            self.emit("error", str(e))
            return {"ok": False, "error": str(e)}

        if fresh:
            # Ralph 式：模型只看本轮目标+状态，不背全历史；system 仍注入 notes/tasks（跨轮记忆）
            model_messages = list(messages)
            system = self._effective_system(self._first_user_text(messages))
        else:
            # P6.2 上下文预算压缩：只裁喂给模型的副本，history / DB 保留完整历史。
            system, model_messages = self._budget(messages)

        registry = self.registry.filtered(lambda n: n in _PLAN_TOOLS) if self.plan_mode \
            else self.registry
        loop = AgentLoop(
            provider, registry, self.gate, max_steps=res.config.agent.max_steps,
            hook_runner=self._make_hook_runner(),
            stuck_threshold=res.config.agent.stuck_edit_threshold,
            browse_nudge=self._browse_nudge_enabled(),
            auto_retry=res.config.agent.auto_retry,
            retry_max_attempts=res.config.agent.retry_max_attempts,
            retry_backoff_base=res.config.agent.retry_backoff_base,
            failure_memory=self._get_failure_memory(res.config.agent.failure_memory),
            deadend_threshold=res.config.agent.deadend_threshold,
            research_refine=res.config.agent.research_refine,
            research_refine_max=res.config.agent.research_refine_max,
            research_judge=self._make_research_judge(provider, res.config.agent.research_judge),
        )
        n_in = len(model_messages)  # 压缩后喂入条数；loop 仅在其后追加新消息
        try:
            result = loop.run(model_messages, system, self.emit, cancel=self._cancel,
                              take_injects=self._take_injects)
            self._last_turn_hit_max = getattr(loop, "hit_max_steps", False)  # 供 crazy 外层判断本轮是否被步数截断
            # B 验证闭环（FR-11.2c）：本轮改过文件 -> 收尾跑 test_command；失败回灌输出、复用同一
            # loop 续跑让模型修，限 test_max_iters 次。原地往 result 追加修复轮消息（下方统一落库）。
            if not self._cancel.is_set() and self._changed_files_this_turn(result[n_in:]):
                self._auto_test_loop(loop, result, system)
        except Exception as e:  # noqa: BLE001 — 兜底，避免前端卡死
            self.emit("error", f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}

        new_msgs = result[n_in:]  # 主轮 + auto_test 修复轮的全部新增（取消时为已完成部分）
        with self.lock:
            self.history.extend(new_msgs)
        for m in new_msgs:
            self._persist(m)
        if res.store and self.session_id is not None:
            res.store.touch_session(self.session_id)

        if self._cancel.is_set():  # 被用户停止：已生成部分已落库，不再生成规范
            self.emit("stopped", "")
            return {"ok": True, "stopped": True}

        self._maybe_generate_conventions()  # 工作区有内容但缺 hermes.md -> 后台生成一版
        self._maybe_auto_review(new_msgs)   # 本轮改过文件 -> 收尾自动派 reviewer 审 diff（FR-11.2b）
        self.emit("done", "")
        return {"ok": True}

    # ---- 重新生成 / 编辑重发（覆盖式截断后重跑，FR-易用性 P1）---------------

    def _nth_user_index(self, n: int) -> "int | None":
        """返回历史里第 n 条（0-based）user 角色消息的下标；越界返 None。
        steering 注入工具结果、不产生独立 user 消息，故 user 消息 ≈ 用户轮次 1:1。"""
        seen = -1
        for i, m in enumerate(self.history):
            if m.role == "user":
                seen += 1
                if seen == n:
                    return i
        return None

    def _truncate_history(self, keep: int) -> None:
        """把内存历史与 DB 都截断到只保留前 keep 条；同步清理压缩缓存与抽取进度。"""
        with self.lock:
            self.history = self.history[:keep]
        if self.res.store and self.session_id is not None:
            self.res.store.truncate_messages_after(self.session_id, keep)
        self._compact = None  # 切点变了，旧压缩摘要作废
        if self.session_id is not None:
            up = self.res.extracted_upto.get(self.session_id)
            if up is not None and up > keep:
                self.res.extracted_upto[self.session_id] = keep

    def _enqueue_rerun(self, label: str) -> dict:
        """把「在已截断历史上重跑一轮」入队（走 worker，保证 state/事件/停止一致）。"""
        self._reset_turn_checkpoint(label)
        self._queue.put((_RERUN, None))
        self._ensure_worker()
        self.state = "queued"
        self.emit("state", {"state": "queued"})
        return {"ok": True, "queued": True}

    def regenerate(self, turn: int) -> dict:
        """重新生成第 turn（0-based）轮用户消息对应的回答：保留该用户消息、丢弃其后全部、重跑。"""
        if self.is_busy() or self.crazy_mode:
            return {"ok": False, "error": "当前对话正在运行，请先停止再重新生成"}
        idx = self._nth_user_index(int(turn))
        if idx is None:
            return {"ok": False, "error": "找不到对应的用户消息"}
        label = _message_text(self.history[idx])
        self._truncate_history(idx + 1)  # 保留到该用户消息（含）
        return self._enqueue_rerun(label)

    def edit_and_resend(self, turn: int, new_text: str) -> dict:
        """编辑第 turn（0-based）轮用户消息为 new_text：替换该消息、丢弃其后全部、重跑。
        v1 为纯文本编辑（原附件不保留）。"""
        if self.is_busy() or self.crazy_mode:
            return {"ok": False, "error": "当前对话正在运行，请先停止再编辑"}
        text = (new_text or "").strip()
        if not text:
            return {"ok": False, "error": "编辑内容为空"}
        idx = self._nth_user_index(int(turn))
        if idx is None:
            return {"ok": False, "error": "找不到对应的用户消息"}
        self._truncate_history(idx)  # 丢弃该消息及其后
        content = build_user_content(text, None, self.res.limits)
        with self.lock:
            self.history.append(Message("user", content))
        self._persist(Message("user", content))
        return self._enqueue_rerun(text)

    # ---- 收尾自动评审（FR-11.2b）----------------------------------------

    _WRITE_TOOLS = ("write_file", "edit_file", "multi_edit")

    def _changed_files_this_turn(self, new_msgs) -> list[str]:
        """从本轮新增消息里收集被写/编辑过的文件路径（去重保序）。"""
        seen: list[str] = []
        for m in new_msgs:
            if m.role != "assistant" or not isinstance(m.content, list):
                continue
            for b in m.content:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") in self._WRITE_TOOLS):
                    path = (b.get("input") or {}).get("path")
                    if path and path not in seen:
                        seen.append(path)
        return seen

    # ---- 验证闭环（FR-11.2c）：收尾跑测试，失败自动迭代修 -------------------

    def _effective_test_command(self) -> str:
        """项目级 `.hermes.yaml` 的 test_command 优先，否则回退全局 config.agent.test_command。
        这样在不同项目间切换时，各自用各自工作区的测试命令，不必改全局。"""
        from ..config import read_project_config
        cmd = read_project_config(self.workspace).get("test_command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
        return (self.res.config.agent.test_command or "").strip()

    def _run_test_command(self) -> "tuple[bool, str, int]":
        """在工作区跑 test_command（项目级优先），返回 (通过?, 合并输出, returncode)。
        shell、cwd=工作区、限时 180s；returncode=-1 表示没跑起来（超时/异常）。"""
        import subprocess
        cmd = self._effective_test_command()
        try:
            proc = subprocess.run(cmd, shell=True, cwd=str(self.workspace),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=180)
            return (proc.returncode == 0,
                    ((proc.stdout or "") + (proc.stderr or "")).strip(), proc.returncode)
        except subprocess.TimeoutExpired:
            return False, f"测试命令超时（>180s）：{cmd}", -1
        except Exception as e:  # noqa: BLE001
            return False, f"测试命令执行失败：{type(e).__name__}: {e}", -1

    @staticmethod
    def _is_launch_failure(output: str, rc: int) -> bool:
        """命令根本没跑起来（找不到/不可执行/超时）——是 test_command 配置/环境问题，不是测试断言失败，
        不该让模型进修复循环瞎改代码/环境（实测：kimi 会去造符号链接 hack 环境）。"""
        if rc in (-1, 126, 127, 9009):  # -1 超时/异常 126 不可执行 127 not found(unix) 9009 win
            return True
        low = (output or "").lower()
        return any(s in low for s in ("command not found", "not found",
                                      "is not recognized", "no such file"))

    def _auto_test_loop(self, loop, result, system) -> None:
        """收尾跑 test_command；失败把输出回灌、复用同一 loop 续跑让模型修，限 test_max_iters。
        原地往 result 追加修复轮的新消息（由 send_message 统一落库）。"""
        cfg = self.res.config.agent
        cmd = self._effective_test_command()
        if not cfg.auto_test or not cmd:
            return
        iters = max(1, cfg.test_max_iters)
        for i in range(iters):
            if self._cancel.is_set():
                break
            ok, output, rc = self._run_test_command()
            # 命令根本没跑起来 = test_command 配置/环境问题，提示用户、不让模型进修复循环瞎改
            if not ok and self._is_launch_failure(output, rc):
                self.emit("auto_test", {"ok": False, "iter": i + 1, "max": iters, "command": cmd,
                                        "output": output[-2000:], "config_error": True})
                self.emit("error",
                          f"自动测试命令没能执行（疑似 test_command 配置/环境问题，非代码错误）："
                          f"`{cmd}`\n{output[-500:]}\n请检查 config / .hermes.yaml 里的 test_command。")
                return
            self.emit("auto_test", {"ok": ok, "iter": i + 1, "max": iters,
                                    "command": cmd, "output": output[-2000:]})
            if ok or i == iters - 1:
                break  # 通过、或已是最后一次（结果已上报，不再迭代）
            # 断言失败且还有迭代余额：回灌测试输出，续跑一轮让模型修
            result.append(Message("user",
                f"自动测试未通过（命令 `{cmd}`）。输出：\n{output[-4000:]}\n\n"
                "请定位原因并修复，使测试通过。"))
            try:
                loop.run(result, system, self.emit, cancel=self._cancel,
                         take_injects=self._take_injects)
            except Exception as e:  # noqa: BLE001 — 修复轮出错就停
                self.emit("error", f"auto_test 修复轮出错：{type(e).__name__}: {e}")
                break

    def _maybe_auto_review(self, new_msgs) -> None:
        """开了 auto_review 且本轮改过文件时，派一个 reviewer 子 Agent 审本轮 diff（安全网）。

        只读评审、不改主历史；结论经子任务块呈现给用户。取消时不触发。
        """
        if not self.res.config.agent.auto_review or self._cancel.is_set():
            return
        changed = self._changed_files_this_turn(new_msgs)
        if not changed:
            return  # 纯对话/只读轮：零开销
        diffs: list[str] = []
        for rel in changed[:20]:
            d = self.get_file_diff(rel)
            if d:
                diffs.append(f"# {rel}\n{d[:4000]}")
        context = "\n\n".join(diffs) if diffs else "（改动文件：" + "、".join(changed) + "）"
        task = (
            "审查本轮刚做的代码改动：找 bug、逻辑遗漏、边界与风险、与项目风格不一致之处。"
            "简明列出问题与改进建议；确无问题就回复『评审通过』。这是收尾安全网，只读不改。"
        )
        try:
            self.run_subagent(task, context, role="reviewer")
        except Exception as e:  # noqa: BLE001 — 评审失败绝不影响本轮交付
            self.emit("error", f"自动评审跳过：{type(e).__name__}: {e}")

    # ---- 上下文预算（P6.2） ----------------------------------------------

    def _budget(self, messages):
        """按预算压缩喂给模型的消息，返回 (system, model_messages)。"""
        cc = self.res.config.context
        system = self._effective_system(self._latest_user_text(messages))
        if not cc.enabled:
            return system, messages
        res = compress(
            messages, system,
            budget=cc.max_input_tokens, keep_recent_turns=cc.keep_recent_turns,
            summarize=self._compact_summarize if cc.model_summary else None,
        )
        if res.compressed:
            mode = ("model" if res.dropped and self._compact
                    and self._compact[0] == res.dropped else "heuristic")
            self.emit("context_compressed", {
                "dropped": res.dropped, "before": res.before_tokens,
                "after": res.after_tokens, "budget": res.budget, "summary": mode,
            })
        return res.system, res.messages

    def _compact_summarize(self, dropped) -> "str | None":
        """模型生成压缩摘要（FR-10.4a，对标 /compact）；返回 None 则 compress 回退启发式。

        缓存语义：切点（被丢弃条数）不动 -> 直接复用上次摘要（零额外调用）；
        切点前移 -> 旧摘要 + 新增段增量合并（一次便宜调用）；失败 120s 内不再重试。
        在 worker 线程内同步执行（摘要必须先于本轮主请求就绪）。
        """
        cut = len(dropped)
        if self._compact and self._compact[0] == cut:
            return self._wrap_summary(cut, self._compact[1])
        if time.time() - self._compact_failed_at < 120:
            return None
        prev = None
        new_part = dropped
        if self._compact and self._compact[0] < cut:  # 增量：只喂新被丢弃的段
            prev = self._compact[1]
            new_part = dropped[self._compact[0]:]
        cfg = self.res.config
        try:
            provider = build_provider(cfg, cfg.context.summary_model or self.active_model)
        except Exception:  # noqa: BLE001 — 配置/密钥问题：回退启发式
            self._compact_failed_at = time.time()
            return None
        system, msgs = build_summary_request(new_part, prev)
        text = ""
        try:
            for ev in provider.stream_chat(msgs, system=system, tools=None):
                if ev.type == "text":
                    text += ev.text
                elif ev.type == "error":
                    raise RuntimeError(ev.text)
                elif ev.type == "done":
                    break
        except Exception:  # noqa: BLE001
            self._compact_failed_at = time.time()
            return None
        text = text.strip()
        if not text:
            self._compact_failed_at = time.time()
            return None
        self._compact = (cut, text)
        return self._wrap_summary(cut, text)

    @staticmethod
    def _wrap_summary(cut: int, text: str) -> str:
        return f"[此前对话摘要（{cut} 条较早消息已压缩，模型生成）]\n{text}"

    # ---- 长期记忆（P6.3）：system 组装 ----------------------------------

    @staticmethod
    def _latest_user_text(messages) -> str:
        """取最近一条 user 消息的纯文本，作为记忆检索的 query。"""
        for m in reversed(messages):
            if getattr(m, "role", None) == "user":
                c = m.content
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(b.get("text", "") for b in c
                                    if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def _recall_memories(self, query, limit: int) -> list[dict]:
        """分层召回长期记忆：稳定的用户事实/偏好常驻 + 其余按当前任务相关性 top-k（回退最近 N 条）。
        相关性用轻量词重叠打分（不依赖向量/外部依赖）；为第2步「框架原则优先」预留扩展位。"""
        import re
        from ..store.memory import normalize_kind
        mem = self.res.memory
        if mem is None:
            return []
        cand = mem.list(limit=max(limit * 5, 50))
        pinned = [m for m in cand if normalize_kind(m.get("kind")) in ("principle", "user", "preference")]
        q = (query or "").lower()
        terms = [t for t in re.split(r"[\s,，。、;；:：/()（）]+", q) if len(t) >= 2]

        def score(m):
            c = (m.get("content") or "").lower()
            return sum(1 for t in terms if t in c)

        if terms:
            hits = [m for m in sorted(cand, key=score, reverse=True) if score(m) > 0][:limit]
        else:
            hits = cand[:limit]
        seen, out = set(), []
        for m in pinned + hits:           # 稳定事实优先，其余按相关性
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            out.append(m)
        return out[:limit]

    def _effective_system(self, query: "str | None" = None) -> str | None:
        """组装 system：基础提示 + 项目规范(hermes.md) + 长期记忆。"""
        cfg = self.res.config
        parts: list[str] = []
        if cfg.system_prompt:
            parts.append(cfg.system_prompt)
        # 注入当前实际运行的模型身份：否则被问"你是什么模型"时，模型会按训练语料瞎答
        # （如 kimi 自称 Claude）。用当前对话选中的档案的真实 model id 据实告知。
        mc = cfg.models.get(self.active_model)
        if mc:
            parts.append(
                f"运行信息：你当前实际运行在「{mc.model}」模型上（模型档案 `{self.active_model}`）。"
                "被问及\"你是什么/哪个模型\"时据此如实回答，不要自称其它模型（如 Claude/GPT）。"
            )
        # 运行环境：把真实操作系统 + shell 工具名注入，避免模型在 macOS/Linux 上误用 Windows 命令
        import platform as _platform
        _os = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(
            _platform.system(), _platform.system())
        parts.append(
            f"[运行环境] 操作系统：{_os}；执行命令的 shell 工具是 `run_{cfg.agent.shell}`。"
            f"写命令请用该平台的语法（{'PowerShell' if cfg.agent.shell in ('powershell', 'pwsh') else cfg.agent.shell}）："
            f"{'Windows 路径用反斜杠、列目录 dir/Get-ChildItem' if _os == 'Windows' else 'POSIX 路径用正斜杠、列目录 ls，别用 Windows 专属命令'}。"
        )
        if self.plan_mode:
            parts.append(_PLAN_DIRECTIVE)
        if self.crazy_mode:
            parts.append(_CRAZY_DIRECTIVE)
        if self._extra_dirs:   # 让模型知道额外授权目录的存在与路径，否则它会读错/臆测拒绝
            dirs = "；".join(str(d) for d in self._extra_dirs)
            parts.append(
                f"[额外授权目录] 除当前工作区外，你已被授权可访问以下工作区外目录（用其完整绝对路径）：{dirs}。"
                "要读其中文件/列目录，直接用完整路径调 read_file / list_dir 即可——它们在授权范围内、不会被拒，"
                "不要因为路径不在工作区就拒绝或臆测「无权限」，先实际调用工具去读。"
            )

        conv = read_conventions(self.workspace, cfg.agent.conventions_file)
        if conv:
            parts.append(
                f"[项目规范] 本工作区 {cfg.agent.conventions_file} 中的开发规范，"
                f"开发时须遵守（与通用规范冲突时以此为准）：\n{conv}"
            )

        if self.res.memory:
            mc = cfg.memory
            block = build_memory_block(
                self._recall_memories(query, mc.max_inject),   # 分层召回：稳定事实常驻 + 按当前任务相关性
                max_items=mc.max_inject, max_chars=mc.max_inject_chars,
            )
            if block:
                parts.append(block)

        # 当前任务清单（FR-9.1）+ 工作笔记（FR-11.3a）：注入 system，使上下文压缩/重启后
        # 模型仍记得自己的计划与已沉淀的事实/决定。
        if self.res.store and self.session_id is not None:
            task_block = build_task_block(self.res.store.get_tasks(self.session_id))
            if task_block:
                parts.append(task_block)
            notes_block = build_notes_block(self.res.store.get_notes(self.session_id))
            if notes_block:
                parts.append(notes_block)

        return "\n\n".join(parts) if parts else None

    # ---- 任务清单（FR-9.1）---------------------------------------------

    def get_tasks(self) -> list[dict]:
        if self.res.store and self.session_id is not None:
            return self.res.store.get_tasks(self.session_id)
        return []

    def get_notes(self) -> str:
        if self.res.store and self.session_id is not None:
            return self.res.store.get_notes(self.session_id)
        return ""

    def set_plan_mode(self, on: bool) -> bool:
        """切换规划模式（FR-11.5），返回新状态。"""
        self.plan_mode = bool(on)
        return self.plan_mode

    # ---- 自主 / crazy 模式（无人值守外层目标循环）----------------------------

    def set_crazy_mode(self, on: bool) -> bool:
        """切换自主/crazy 模式：开启时危险操作免确认 + ask_user 自动放行（无人值守）。"""
        self.crazy_mode = bool(on)
        self.gate._allow_all = self.crazy_mode   # 危险操作免逐个确认
        self._ask.set_auto(self.crazy_mode)      # ask_user 不阻塞、按合理默认放行
        return self.crazy_mode

    def _last_assistant_text(self) -> str:
        """取历史里最后一条 assistant 消息的纯文本（解析 crazy 自评标记用）。"""
        with self.lock:
            for m in reversed(self.history):
                if m.role != "assistant":
                    continue
                if isinstance(m.content, str):
                    return m.content
                if isinstance(m.content, list):
                    return " ".join(
                        b.get("text", "") for b in m.content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                return ""
        return ""

    def run_autonomous(self, intent: str, max_rounds: "int | None" = None) -> dict:
        """自主/crazy 外层循环（同步）：自动写 GOAL → 反复跑 agent + 自评 → 达成或触护栏停。
        护栏：轮数 / 墙钟时间 / 累计 token 预算 + 连续空转检测；危险命令由 gate 黑名单兜底拦。
        复用 send_message 跑单轮；crazy_mode 已让 system 注入 _CRAZY_DIRECTIVE、免确认、免问用户。"""
        if not intent or not intent.strip():
            return {"ok": False, "error": "空意图"}
        import time
        cfg = self.res.config.agent
        budget = int(max_rounds or cfg.crazy_max_rounds)
        deadline = (time.monotonic() + cfg.crazy_max_seconds) if cfg.crazy_max_seconds > 0 else None
        self._crazy_tokens = 0
        self._cancel.clear()
        self.set_crazy_mode(True)
        orig_emit = self.emit
        def _counting_emit(event, data):   # 期间统计 token 用量，用于预算护栏
            if event == "usage" and isinstance(data, dict):
                self._crazy_tokens += (data.get("input", 0) or 0) + (data.get("output", 0) or 0)
            orig_emit(event, data)
        self.emit = _counting_emit
        self.emit("crazy_start", {"intent": intent.strip(), "max_rounds": budget})
        goal = intent.strip()
        nxt = None  # 上一轮自评给的"下一步"
        rnd, stale, reason = 0, 0, "budget_exhausted"
        verify_forced = False   # 块2 验收门：是否已逼过一次"实跑验收"（无 test_command 时只逼一次，防死循环）
        verify_fails = 0        # 块3：验收连续真红次数（到 crazy_verify_ask_at 就停下问用户）
        try:
            while rnd < budget:
                if self._cancel.is_set():
                    reason = "stopped"; break
                if deadline and time.monotonic() > deadline:
                    reason = "time_budget"; break
                if cfg.crazy_max_tokens > 0 and self._crazy_tokens >= cfg.crazy_max_tokens:
                    reason = "token_budget"; break
                rnd += 1
                self.emit("crazy_round", {"round": rnd, "max": budget, "tokens": self._crazy_tokens})
                self._last_turn_had_inject = False  # 本轮重置；_take_injects 消费到补充时会置位
                # Ralph 式 fresh context：每轮只喂【目标 + 任务/笔记 + 已改动文件 + 下一步】，
                # 不背累积历史——与 crazy 前的对话隔离、跨轮不串味，状态全靠 notes/tasks/文件。
                prompt = self._build_crazy_prompt(goal, nxt, first=(rnd == 1))
                with self.lock:
                    before = len(self.history)
                self._run_crazy_round(prompt)
                with self.lock:
                    new_msgs = list(self.history[before:])
                if self._cancel.is_set():
                    reason = "stopped"; break
                # 防空转：本轮没动用任何工具（纯文字）→ 累计；连续 stall_rounds 轮无工具就判空转停
                if _turn_used_tools(new_msgs):
                    stale = 0
                else:
                    stale += 1
                    if stale >= max(1, cfg.crazy_stall_rounds):
                        reason = "stalled"; break
                verdict, nxt = _parse_crazy_verdict(self._last_assistant_text())
                # 块A 契约观测：把 verdict 映射成稳定 Need 并随轮次上报（仅记账，不夺
                # 分支决策权——下面仍按 verdict 走，保证行为逐字节等价。Need 是后续
                # Learning 聚合的 key，见 docs/adr/0014）。
                self.emit("crazy_need", {"round": rnd, "need": verdict_to_need(verdict).value})
                hit = getattr(self, "_last_turn_hit_max", False)
                had_inject = getattr(self, "_last_turn_had_inject", False)
                # 块3 自适应过门：撞设计岔路/目标模糊 → 停下真问用户（gate_ask 关则按合理默认自走）
                if verdict == "need_user" and not hit and not had_inject:
                    q = nxt or "需要你拍板（未给出具体问题）"
                    if cfg.crazy_gate_ask:
                        self.emit("crazy_gate", {"kind": "need_user", "question": q})
                        ans = self._crazy_gate_ask(
                            q, ["你来定（在「其他」里补充具体指示）", "你按合理判断继续，不必等我"])
                        nxt = f"用户对你的提问『{q}』的回复：{ans}。据此继续推进当前/下一阶段。"
                    else:
                        nxt = f"（无人值守，没人回答你的提问『{q}』）按你自己的合理判断决定并继续。"
                    continue
                # 块4 阶段后重规划：当前阶段刚通过验收、还有后续阶段 → 下一轮先按这阶段学到的
                # 调整剩余阶段（计划随进展演化），再推进；replan 关则退化成普通 continue（死守初始拆分）。
                if verdict == "phase_done" and not hit and not had_inject:
                    if cfg.crazy_replan:
                        self.emit("crazy_replan", {"phase": nxt})
                        nxt = self._crazy_replan_directive(nxt)
                    continue
                if verdict == "done" and not hit and not had_inject:
                    # 块2 验收门：测试不绿不准收尾——把"完成"从模型自报改成测试驱动。
                    gate, verify_forced, failed = self._crazy_verify_gate(verify_forced)
                    if gate is None:
                        reason = "goal_reached"; break   # 验收通过 / 无可测产物 → 真收工
                    # 块3：验收"反复真红"到阈值 → 自适应停下问用户（换思路/跳过/接手），别闷头修到耗尽预算
                    if failed:
                        verify_fails += 1
                        if cfg.crazy_gate_ask and verify_fails >= max(1, cfg.crazy_verify_ask_at):
                            self.emit("crazy_gate", {"kind": "verify_stuck", "fails": verify_fails})
                            ans = self._crazy_gate_ask(
                                f"自主任务的验收反复修不过（已 {verify_fails} 次）。最近情况：\n{gate[:600]}\n你想怎么办？",
                                ["继续按当前思路修", "换个思路/简化方案再试", "跳过卡住项、先把能交付的收尾", "停下，我来处理"])
                            verify_fails = 0
                            if ("停下" in ans) or ("我来" in ans):
                                reason = "user_stopped"; break
                            nxt = (f"用户指示：{ans}。" + ("跳过卡住的验收项、把已完成能交付的部分收尾。"
                                   if "跳过" in ans else "据此调整后继续修复验收。"))
                            continue
                    nxt = gate                           # 没过 / 需实跑 → 带修复指令再跑一轮，不收工
                    continue
                # 撞步数上限、或本轮有用户中途补充 → 不轻信 [[DONE]]，再跑一轮确认原目标+补充都完成
                if (hit or had_inject) and not nxt:
                    nxt = ("上一轮被打断（步数上限或用户中途补充）、任务可能尚未全部完成："
                           "继续推进，并确认原目标与用户补充都已达成后再收尾。")
        finally:
            self.emit = orig_emit
            self.set_crazy_mode(False)
        self.emit("ws_settle", {})  # crazy 结束、空闲：补做 crazy 期间被跳过的工作区改名
        self.emit("crazy_done", {"round": rnd, "reason": reason, "tokens": self._crazy_tokens})
        return {"ok": True, "rounds": rnd, "reason": reason, "tokens": self._crazy_tokens}

    def _crazy_changed_files(self) -> str:
        """已改动文件清单（注入每轮 fresh 上下文，让模型不看历史也知道动过哪些文件）。"""
        try:
            files = [c.get("path", "") for c in (self.ledger.changes() or [])]
        except Exception:  # noqa: BLE001
            files = []
        files = [f for f in files if f][:30]
        return "；".join(files) if files else "（暂无）"

    def _crazy_verify_gate(self, forced: bool) -> "tuple[str | None, bool, bool]":
        """块2 验收门：crazy 声明 DONE 前强制验收。返回 (下一步指令 or None, 新 forced, 是否真失败)。
        None=放行收尾；非 None=没通过/需实跑，带着该指令再跑一轮。第三个=该指令是否为"测试真红"
        （供块3 自适应过门计数：反复真红才停下问用户；"逼一轮实跑"不算失败）。
        - 纯调研/无文件产物：无可测 → 放行。
        - 配了 test_command：真跑，红了不放行、带失败回去修（硬门）。
        - 没配 test_command：逼一轮"用项目自己的方式实跑验收并贴结果"（只逼一次，防死循环）。"""
        try:
            has_changes = bool(self.ledger.changes())
        except Exception:  # noqa: BLE001
            has_changes = True   # 拿不准就走验收门（保守）
        if not has_changes:
            return None, forced, False
        cmd = self._effective_test_command()
        if cmd:
            ok, output, rc = self._run_test_command()
            if ok:
                return None, forced, False
            return (f"[系统·验收门] 你声明完成，但测试命令 `{cmd}` **没通过**（exit {rc}）：\n{(output or '')[-1500:]}\n"
                    "**先把测试修到全绿再收尾**，别跳过、别只口头说通过。", forced, True)
        if not forced:
            return ("[系统·验收门] 收尾前最后一步：**用本项目自己的方式实际跑一遍所有阶段的验收/测试**"
                    "（pytest / npm test / 直接跑程序看输出等），把**真实输出**贴出来确认全部通过——"
                    "别只凭印象说完成。确认全绿再 [[DONE]]；有失败就修复后再来。", True, False)
        return None, forced, False   # 已逼过一次实跑、又声明 DONE → 信任收尾

    def _crazy_gate_ask(self, question: str, options: list) -> str:
        """crazy 阶段门：临时关掉 ask 的自动放行、**真问一次用户**、等回复（停下让人拍板），
        问完恢复无人值守（阶段内的零碎决策仍自动、不打扰用户）。"""
        self._ask.set_auto(False)
        try:
            return self._ask.ask(question, options)
        finally:
            self._ask.set_auto(True)

    def _crazy_replan_directive(self, next_step: "str | None") -> str:
        """块4：一个阶段刚通过验收 → 下一轮先按这阶段实际学到的重规划剩余阶段，再推进。
        把模型自报的"下一阶段"接在指令末尾，组成下一轮的 nxt（注入 _build_crazy_prompt）。"""
        nxt = (next_step or "推进下一阶段").strip()
        return (
            "[阶段完成·重规划] 你刚完成并通过了一个阶段的验收。动手下一阶段前，先用这一步**回顾并重规划剩余阶段**："
            "结合这阶段实际踩到的难点 / 新发现的约束依赖 / 验证暴露的问题 / 有没有更省事的做法，"
            "判断 update_tasks 里**尚未完成**的阶段要不要调整——补遗漏、删多余、重排顺序、过粗的拆细 / 过细的合并"
            "（**已完成的阶段别动**）。需要就更新 update_tasks，不需要就保持原计划。"
            f"重规划好后继续推进：{nxt}。"
        )

    def _build_crazy_prompt(self, goal: str, step: "str | None", first: bool) -> str:
        """组织一轮 fresh 上下文的 user 消息：目标 + 当前状态指针 + 本轮要求（Ralph 式）。"""
        parts = [f"[自主目标] {goal.strip()}"]
        parts.append(
            "[当前进度] 你已完成的工作记录在上方系统提示的「任务清单」和「工作笔记」里（每轮都更新它们）；"
            f"已改动的文件：{self._crazy_changed_files()}。需要看具体内容用 read_file / git_diff 自己查。")
        if first:
            parts.append("[本轮] 先把目标拆成**有序阶段**（每阶段带【目标 + 怎么验收/测试】）写进 update_tasks、"
                         "关键决定/约束写进 update_notes，然后**从第一阶段开始动手**。")
        else:
            nxt = (step or "继续推进尚未完成的部分").strip()
            parts.append(f"[本轮] 下一步：{nxt}。先按需读取已有改动/笔记了解现状；**聚焦当前阶段**，把它做完并跑验收"
                         "（测试）确认达成后再进下一阶段——别跳着做。")
        parts.append("**随时把进展/决定/下一步写进 update_tasks / update_notes**（这是你唯一的跨轮记忆）。"
                     "全部完成并验证通过后，最后一行只输出 `[[DONE]]`；否则最后一行输出 "
                     "`[[CONTINUE: 下一步具体要做的事]]`。")
        return "\n\n".join(parts)

    def _run_crazy_round(self, prompt: str) -> dict:
        """跑一轮 crazy（fresh context）：把 prompt 当本轮唯一输入喂模型，不背累积历史。
        prompt 仍落 history/DB 供前端显示；模型只看 prompt + 系统提示(含 notes/tasks)。"""
        self._reset_turn_checkpoint(prompt[:40])
        self._ensure_session(prompt[:80])
        user_msg = Message("user", prompt)
        with self.lock:
            self.history.append(user_msg)
        self._persist(user_msg)
        return self._run_turn([user_msg], fresh=True)

    def start_autonomous(self, intent: str, max_rounds: "int | None" = None) -> dict:
        """异步启动自主模式：后台线程跑 run_autonomous，立即返回（不阻塞 UI）。"""
        if self.crazy_mode:
            return {"ok": False, "error": "已在自主模式运行中"}
        threading.Thread(
            target=self.run_autonomous, args=(intent, max_rounds), daemon=True
        ).start()
        return {"ok": True, "started": True}

    # ---- 委派子 Agent（FR-9.3）------------------------------------------

    def _subagent_system(self, role) -> str:
        """子 Agent 的 system：基础提示 + 项目规范 + 基础角色指令 + 该角色职责（不含主任务清单/记忆）。"""
        cfg = self.res.config
        parts: list[str] = []
        if cfg.system_prompt:
            parts.append(cfg.system_prompt)
        conv = read_conventions(self.workspace, cfg.agent.conventions_file)
        if conv:
            parts.append(
                f"[项目规范] 本工作区 {cfg.agent.conventions_file} 中的开发规范，须遵守：\n{conv}"
            )
        parts.append(SUBAGENT_DIRECTIVE)
        if role.directive:
            parts.append(role.directive)
        if self._extra_dirs:   # 同主 Agent：子 Agent 也得知道额外授权目录，否则读错工作区/臆测无权限
            dirs = "；".join(str(d) for d in self._extra_dirs)
            parts.append(
                f"[额外授权目录] 除当前工作区外，你已被授权可访问以下工作区外目录（用其完整绝对路径）：{dirs}。"
                "要读其中文件/列目录，直接用完整路径调 read_file / list_dir 即可——它们在授权范围内、不会被拒，"
                "不要因为路径不在工作区就拒绝或臆测「无权限」，先实际调用工具去读。"
            )
        return "\n\n".join(parts)

    def _grade_subagent(self, grader, task: str, acceptance: str | None,
                        summary: str, transcript=None) -> "tuple[bool, str]":
        """用 lead 模型对子 Agent 产出评分（无工具、一次性短调用），返回 (是否通过, 反馈)。
        据子 Agent 的**执行证据**（实际工具调用）评分，而非只看摘要自述。
        调用/解析任何异常都判通过——评分是增强项，绝不因它卡死或误退已完成的子任务。"""
        try:
            evidence = summarize_activity(transcript) if transcript else ""
            prompt = build_grader_prompt(task, acceptance, summary, evidence)
            text = ""
            for ev in grader.stream_chat([Message("user", prompt)], system=None, tools=[]):
                if ev.type == "text":
                    text += ev.text
                elif ev.type in ("done", "error"):
                    break
            return parse_grade(text)
        except Exception:  # noqa: BLE001
            return True, ""

    def run_subagent(self, task: str, context: str | None = None, role: str = "general",
                     acceptance: str | None = None) -> str:
        """起一个独立上下文的子 Agent 跑完子任务，只返回摘要（供 delegate 工具回灌主 Agent）。

        role（FR-9.5）：general/researcher/reviewer/tester——决定子 Agent 的职责指令与工具限权。
        在主 worker 线程内同步执行；共用本对话的 gate（危险操作照常过权限）与 _cancel（停止级联）。
        子事件经 subagent_event 路由到前端的可折叠子任务块。
        """
        res = self.res
        cfg = res.config
        task = (task or "").strip()
        role_obj = resolve_role(role, self._roles)
        with self._sub_lock:  # 并行委派时多线程进入（FR-10.5）
            self._sub_seq += 1
            sub_id = self._sub_seq
        self.emit("subagent_start",
                  {"id": sub_id, "task": task, "role": role_obj.name, "role_label": role_obj.label})

        def sub_emit(event: str, data) -> None:
            self.emit("subagent_event", {"id": sub_id, "event": event, "data": data})

        # 模型优先级：角色配置 > agent.subagent_model > 当前对话模型（FR-10.5 按角色配模型）
        model = role_obj.model or cfg.agent.subagent_model or self.active_model
        try:
            provider = build_provider(cfg, model)
        except Exception as e:  # noqa: BLE001 — 配置/密钥错误
            self.emit("subagent_done", {"id": sub_id, "ok": False, "summary": str(e)})
            return f"子任务无法启动：{e}"

        loop = AgentLoop(
            provider, self._subagent_registry(role_obj), self.gate,
            max_steps=cfg.agent.subagent_max_steps,
            hook_runner=self._make_hook_runner(),  # 子 Agent 同样受 hooks 约束
            stuck_threshold=cfg.agent.stuck_edit_threshold,
            browse_nudge=self._browse_nudge_enabled(),
            auto_retry=cfg.agent.auto_retry,
            retry_max_attempts=cfg.agent.retry_max_attempts,
            retry_backoff_base=cfg.agent.retry_backoff_base,
            failure_memory=self._get_failure_memory(cfg.agent.failure_memory),
            deadend_threshold=cfg.agent.deadend_threshold,
            research_refine=cfg.agent.research_refine,
            research_refine_max=cfg.agent.research_refine_max,
            research_judge=self._make_research_judge(provider, cfg.agent.research_judge),
        )
        # 子循环抛异常时自动重试一次（附上失败原因），仍失败才回灌主 Agent（FR-11.6b）。
        # 取消时不重试（用户主动停止）。
        result = None
        for attempt in range(2):
            ctx = context
            if attempt == 1:  # 重试：把上次失败原因补进上下文
                ctx = (f"{context}\n\n" if context else "") + \
                    f"（上一次尝试失败，原因：{last_err}。请换个稳妥的做法重试。）"
            messages = [Message("user", compose_task(task, ctx))]
            try:
                result = loop.run(messages, self._subagent_system(role_obj), sub_emit,
                                  cancel=self._cancel)
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                if attempt == 1 or self._cancel.is_set():
                    self.emit("subagent_done", {"id": sub_id, "ok": False, "summary": last_err})
                    return f"子任务出错（已重试）：{last_err}"
                sub_emit("error", f"出错，自动重试一次：{last_err}")

        summary = extract_summary(result)

        # 评分回炉（借 Claude Code Performance Outcomes）：lead 模型按验收标准给子产出打分，
        # 不达标带具体反馈打回、复用子循环上下文重做，最多 delegate_max_revisions 轮。0=关。
        max_rev = max(0, int(getattr(cfg.agent, "delegate_max_revisions", 0)))
        if max_rev > 0 and result and not self._cancel.is_set():
            try:
                grader = build_provider(cfg, self.active_model)  # lead 评 sub（独立于子模型）
            except Exception:  # noqa: BLE001 — 评分器建不起来就跳过，不影响已有产出
                grader = None
            for rev in range(max_rev):
                if grader is None or self._cancel.is_set():
                    break
                passed, feedback = self._grade_subagent(grader, task, acceptance, summary, result)
                sub_emit("grade", {"round": rev + 1, "max": max_rev,
                                   "passed": passed, "feedback": feedback})
                if passed:
                    break
                # 打回重做：反馈作为新 user 消息续到**同一子循环**（保留它已有的上下文），再跑一轮。
                result.append(Message("user", f"[验收反馈 · 请据此改进后重新完成本子任务] {feedback}"))
                try:
                    result = loop.run(result, self._subagent_system(role_obj), sub_emit,
                                      cancel=self._cancel)
                except Exception as e:  # noqa: BLE001 — 回炉出错：保留上一版摘要，停止回炉
                    sub_emit("error", f"评分回炉时出错，用上一版结果：{type(e).__name__}: {e}")
                    break
                summary = extract_summary(result)

        if getattr(loop, "hit_max_steps", False):  # 子任务撞步数上限：强命令式告知主 Agent 别直接用
            summary = (
                "⚠【子任务未完成 · 撞步数上限】下面只是子 Agent 在步数用尽前的**部分**成果，"
                "**不完整、不可直接当最终答案**。你必须：①先检查还缺什么（哪些平台/来源/数据没拿到）；"
                "②自己补充检索或继续把缺口补全；③补全后再给结论。"
                "**禁止**把这段不完整的部分结果直接当成完整结果总结输出、也不要据此就判定任务完成。部分成果如下：\n"
                + summary
            )
        self.emit("subagent_done", {"id": sub_id, "ok": True, "summary": summary})
        return summary

    # ---- 长期记忆（P6.3）：离开会话时自动抽取 ---------------------------

    def capture_sync(self) -> None:
        """同步整理记忆（关程序时用，不起后台线程）：抽取新消息→碎片并触发固化。
        否则用户直接关程序（没切换过会话）时，最后一段对话不会被整理、记忆丢失。"""
        res = self.res
        if not res.memory or not res.config.memory.auto_capture or self.session_id is None:
            return
        mc = res.config.memory
        sid = self.session_id
        with res.lock:
            upto = res.extracted_upto.get(sid, 0)
            total = len(self.history)
            if total <= upto or total < mc.min_messages_to_capture or sid in res.capturing:
                return
            res.capturing.add(sid)  # 占位防并发（不提前推进 upto；整理失败/超时被杀也不丢、下次重试）
        self._capture_worker(list(self.history), upto, sid, self.active_model)  # 同步跑（末尾含固化）

    def capture_async(self) -> None:
        """离开本对话时，后台线程从刚结束的对话里抽取长期记忆。

        只抽取自上次抽取以来的新消息；先占位 extracted_upto 防止并发重复触发。
        账本 extracted_upto 在 Resources 上（按 session_id 记账、跨对话存活）。
        """
        res = self.res
        if not res.memory or not res.config.memory.auto_capture:
            return
        if self.session_id is None:
            return
        mc = res.config.memory
        sid = self.session_id
        with res.lock:
            upto = res.extracted_upto.get(sid, 0)
            total = len(self.history)
            if total <= upto or total < mc.min_messages_to_capture or sid in res.capturing:
                return
            res.capturing.add(sid)  # 占位防并发（不提前推进 upto；整理失败/超时也不丢、下次重试）
        snapshot = list(self.history)
        threading.Thread(
            target=self._capture_worker,
            args=(snapshot, upto, sid, self.active_model),
            daemon=True,
        ).start()

    def _capture_worker(
        self, history: list[Message], start_idx: int, session_id: int, model_name: str
    ) -> None:
        res = self.res
        ok = False
        try:
            provider = build_provider(res.config, model_name)
            transcript = build_transcript(history[start_idx:])
            if not transcript.strip():
                ok = True  # 没文本可抽，算处理过：推进进度避免反复扫
                return
            system, messages = build_extract_request(transcript, res.memory.contents())
            text = ""
            errored = False
            for ev in provider.stream_chat(messages, system=system, tools=None):
                if ev.type == "text":
                    text += ev.text
                elif ev.type == "error":
                    errored = True
                    break
                elif ev.type == "done":
                    break
            if errored:
                return  # 抽取失败：不推进 upto，下次重试
            added = []
            for f in parse_memories(text):
                mid = res.memory.add(f["content"], f["kind"], source=f"auto:{session_id}")
                if mid is not None:
                    added.append({"id": mid, "content": f["content"], "kind": f["kind"]})
            if added:
                self.emit("memory_captured", {"count": len(added), "items": added})
            self._maybe_consolidate()  # 攒够碎片 -> 离线固化成框架原则（类人记忆 第2步）
            ok = True  # 抽取 + 固化都完成
        except Exception:  # noqa: BLE001 — 后台尽力而为；失败不推进 upto，下次重试
            pass
        finally:
            with res.lock:
                if ok:
                    res.extracted_upto[session_id] = len(history)  # 成功才推进进度（超时/失败被杀则不丢）
                res.capturing.discard(session_id)

    def _maybe_consolidate(self) -> None:
        """攒够 fact 碎片就离线固化成框架原则：碎片 -> 几条 principle（原碎片保留作细节）。
        principle 存为高优先级 kind，_recall_memories 让其优先常驻；防重复：较上次固化新增够才再触发。"""
        res = self.res
        mc = res.config.memory
        if not mc.auto_consolidate or res.memory is None:
            return
        from ..store.memory import normalize_kind
        all_mem = res.memory.list()
        facts = [m for m in all_mem if normalize_kind(m.get("kind")) == "fact"]
        with res.lock:
            if len(facts) < mc.consolidate_threshold:
                return
            if len(facts) - res.consolidated_facts < mc.consolidate_threshold:
                return  # 距上次固化新增不够，不重复触发
            res.consolidated_facts = len(facts)  # 占位，避免并发重复
        frags = [m["content"] for m in facts]
        principles = [m["content"] for m in all_mem if normalize_kind(m.get("kind")) == "principle"]
        try:
            provider = build_provider(res.config, self.active_model)
        except Exception:  # noqa: BLE001 — 后台尽力而为
            return
        system, messages = build_consolidate_request(frags, principles)
        text = ""
        try:
            for ev in provider.stream_chat(messages, system=system, tools=None):
                if ev.type == "text":
                    text += ev.text
                elif ev.type == "error":
                    return
                elif ev.type == "done":
                    break
        except Exception:  # noqa: BLE001
            return
        new_ps = [p["content"].strip() for p in parse_memories(text) if p.get("content", "").strip()]
        if not new_ps:
            return
        # 重算替换：新原则已融合旧的（prompt 让模型参考合并），故删旧 principle 再存新——
        # principle 数稳定不膨胀、自动去重，旧的不再相关就被自然淘汰（老化）。
        for m in all_mem:
            if normalize_kind(m.get("kind")) == "principle" and m.get("id") is not None:
                res.memory.delete(m["id"])
        n = 0
        for c in new_ps:
            if res.memory.add(c, "principle", source="consolidate") is not None:
                n += 1
        if n:
            self.emit("memory_consolidated", {"count": n})

    # ---- 持久化辅助 ------------------------------------------------------

    def _ensure_session(self, first_text: str) -> None:
        res = self.res
        if res.store and self.session_id is None:
            # 打开了已有项目则绑定该路径；否则默认隔离文件夹（workspace 列存 NULL，按 id 推导）
            bound = self._pending_workspace
            self.session_id = res.store.create_session(
                make_title(first_text), self.active_model, workspace=bound
            )
            if res.per_session:
                self.set_workspace(
                    Path(bound) if bound else (res.workspaces_root / str(self.session_id))
                )
            self._pending_workspace = None
            self.emit("session_created", {"id": self.session_id})

    def _persist(self, msg: Message) -> None:
        res = self.res
        if res.store and self.session_id is not None:
            res.store.add_message(self.session_id, msg.role, msg.content)

    # ---- 自动生成项目规范 hermes.md --------------------------------------

    def _maybe_generate_conventions(self) -> None:
        """工作区已有项目内容、但缺 conventions_file 时，后台据「全局标准 + 本项目」生成一版。"""
        res = self.res
        ac = res.config.agent
        name = ac.conventions_file
        if not (ac.auto_conventions and name) or self.session_id is None:
            return
        with res.lock:
            if self.session_id in res.conv_attempted:
                return
        ws = self.workspace
        try:
            if (ws / name).exists():
                return  # 已有规范，不动
            has_content = any(p.name != name for p in ws.iterdir())  # 除规范本身外有内容
        except OSError:
            return
        if not has_content:
            return  # 空项目，没东西可提炼
        with res.lock:
            res.conv_attempted.add(self.session_id)
        threading.Thread(
            target=self._conv_worker, args=(ws, self.active_model, name), daemon=True,
        ).start()

    def _conv_worker(self, ws: Path, model_name: str, name: str) -> None:
        res = self.res
        try:
            provider = build_provider(res.config, model_name)
        except Exception:  # noqa: BLE001 — 后台尽力而为
            return
        digest = build_project_digest(ws)
        if not digest.strip():
            return
        system, messages = build_generate_request(digest, res.config.system_prompt)
        text = ""
        try:
            for ev in provider.stream_chat(messages, system=system, tools=None):
                if ev.type == "text":
                    text += ev.text
                elif ev.type == "error":
                    return
                elif ev.type == "done":
                    break
        except Exception:  # noqa: BLE001
            return
        content = clean_output(text)
        if not content.strip():
            return
        try:
            (ws / name).write_text(content, encoding="utf-8")
        except OSError:
            return
        self.emit("conventions_generated", {"file": name})

    # ---- 视觉预处理回退 --------------------------------------------------

    def _maybe_preprocess_vision(self, content, user_text: str):
        """主模型不支持视觉、且 content 含图时，把图转成文字描述。"""
        res = self.res
        vf = res.config.vision_fallback
        if not vf.enabled or not isinstance(content, list):
            return content
        if not any(b.get("type") == "image" for b in content):
            return content
        mc = res.config.get_model(self.active_model)
        if mc.vision:  # 主模型原生支持图像
            return content

        n_imgs = sum(1 for b in content if b.get("type") == "image")
        print(f"  -> 主模型不支持视觉，触发视觉预处理（{n_imgs} 张图 -> 文字描述）",
              file=sys.stderr, flush=True)
        self.emit("vision_start", {"count": n_imgs})

        try:
            api_key = res.config.resolve_api_key_env(vf.api_key_env)
        except Exception as e:  # noqa: BLE001
            self.emit("vision_done", {"ok": False, "error": str(e)})
            return content  # 拿不到 key 就退回原 content（让模型自行处理/报错）

        def describe(image_b64, media_type, prompt):
            return describe_image(
                image_b64, media_type, prompt,
                api_key=api_key, endpoint=vf.endpoint, timeout=vf.timeout,
            )

        new_content = preprocess_vision(content, user_text, describe, vf.prompt)
        descs = [b.get("text", "") for b in new_content if b.get("type") == "text"]
        self.emit("vision_done", {"ok": True, "summary": "；".join(descs)[:500]})
        return new_content

    # ---- 工作区切换（按会话隔离） ----------------------------------------

    def _make_hook_runner(self):
        """按当前工作区 + config.agent.hooks 建可编程 hooks 运行器（无 hook 时返回 None，零开销）。"""
        return make_hook_runner(self.workspace, self.res.config.agent.hooks)

    def _get_failure_memory(self, enabled: bool):
        """块E：懒建并复用单个 FailureMemory（跨会话死路记忆，data/failures.db）。

        enabled=False → None（功能关）。打开失败也降级 None，绝不阻断对话。
        """
        if not enabled:
            return None
        fm = getattr(self, "_failure_memory_cache", None)
        if fm is None:
            try:
                from ..config import ROOT
                from ..agent.world_state import FailureMemory
                fm = FailureMemory(ROOT / "data" / "failures.db")
            except Exception:  # noqa: BLE001
                fm = None
            self._failure_memory_cache = fm
        return fm

    def _make_research_judge(self, provider, enabled: bool):
        """块H3a：构造模型裁判 judge_fn(prompt, images)->str，用当前 provider 跑一次只读判断。

        enabled=False → None（只用 H1/H2 正则）。判断纯文本（H3a）；images 预留给 H3b 接图。
        裁判调用本身由 judge_research 包 try/except，故障一律放行不拦。
        """
        if not enabled or provider is None:
            return None
        from ..providers.base import Message

        def judge_fn(prompt, images=None):
            out = []
            for ev in provider.stream_chat([Message("user", prompt)], system=None, tools=[]):
                if getattr(ev, "type", None) == "text":
                    out.append(ev.text)
            return "".join(out)
        return judge_fn

    def _browse_nudge_enabled(self) -> bool:
        """情境自启：项目代码文件数 ≥ 阈值时，启用「浏览太多→提示 search_code」。"""
        n = self.res.config.agent.search_nudge_files
        prof = getattr(self, "_profile", None)
        return bool(n > 0 and prof is not None and prof.n_code_files >= n)

    def _make_verifier(self):
        """落盘后检查回调：零成本语法校验（FR-11.2a）+ 受影响定向测试（FR-13.C）。

        返回**每次调用现读 config** 的稳定闭包——这样 GUI「功能开关」面板改 auto_verify/
        auto_affected_test 后即时生效，无需重建 registry（重建会重置改动台账）。
        """
        def verify(relpath: str) -> "str | None":
            agent = self.res.config.agent
            # 情境自启②增强：开发中途新建了测试文件 → 重探测、自动开启（不必重开项目）
            if (not agent.auto_affected_test
                    and not self._smart_defaults.get("auto_affected_test")
                    and is_test_file(relpath)):
                self._smart_ws = None            # 失效缓存，强制重算
                self._refresh_smart_defaults()
            # 有效值 = config/面板显式开 或 情境智能默认（情境自启②）；现读，开关即时生效
            auto_test = agent.auto_affected_test or self._smart_defaults.get("auto_affected_test", False)
            checker = make_post_edit_checker(
                self.workspace,
                auto_verify=agent.auto_verify,
                auto_affected_test=auto_test,
                runner=agent.affected_test_runner,
            )
            return checker(relpath) if checker else None
        return verify

    def _refresh_smart_defaults(self) -> None:
        """绑定/切换工作区时探测项目情境，自动给内置行为设智能默认（情境自启②）。

        不覆盖用户在面板的显式选择；只对当前工作区算一次（缓存）；自动开了什么发事件告知前端。
        """
        ws = str(self.workspace)
        if getattr(self, "_smart_ws", None) == ws:
            return
        self._smart_ws = ws
        self._smart_defaults = {}
        self._profile = None
        try:
            from ..config import read_feature_flags
            profile = detect_project_profile(self.workspace)
            self._profile = profile
            user_keys = read_feature_flags().keys()  # 用户在面板设过的键 -> 不动
            applied = compute_smart_defaults(profile, user_keys, self.res.config.agent)
        except Exception:  # noqa: BLE001 — 探测失败不影响正常使用
            return
        self._smart_defaults = applied
        msg = describe_smart_defaults(applied)
        if msg:
            self.emit("smart_default", {"applied": applied, "message": msg})

    def _build_registry(self) -> None:
        """按当前 self.workspace 重建工具注册表（记忆/MCP 工具与工作区无关，复用）。

        改动台账（FR-9.4a）随工作区走：换工作区即换新台账（旧项目的改动记录不再适用）。
        """
        res = self.res
        self.ledger = ChangeLedger(self.workspace)
        self._refresh_smart_defaults()   # 情境自启②：探测项目、设智能默认（不覆盖用户面板选择）
        verifier = self._make_verifier()
        task_binding = (
            TaskBinding(res.store, lambda: self.session_id, self.emit) if res.store else None
        )
        notes_binding = (
            NotesBinding(res.store, lambda: self.session_id, self.emit) if res.store else None
        )
        self.registry = build_registry(
            self.workspace,
            shell=res.config.agent.shell,
            shell_timeout=res.config.agent.shell_timeout,
            screenshot=res.config.agent.screenshot,
            memory_store=res.memory,
            mcp_tools=res.mcp_tools,
            task_binding=task_binding,
            notes_binding=notes_binding,
            delegate_binding=DelegateBinding(self.run_subagent, self._roles),
            change_tracker=self._on_change,
            process_manager=self.procs,
            web=res.config.web,
            verifier=verifier,
            extra_dirs=self._extra_dirs,
            ask_user_binding=self._ask,
            history_search=(self.res.store.search_messages if self.res.store else None),
        )
        # 浏览器穿透开着时，主 agent 也去掉 web_fetch + web_search（同子 agent 的 researcher）：
        # 断掉「浏览器 snapshot 一时读不出内容 → 误判没加载/要登录 → 跳回 web_search 绕路」的退路。
        # 实测真机暴露：委派子 agent 不跳（已被结构约束）、主 agent 自己查就跳（之前没约束）。
        self.registry = self._drop_web_when_browser(self.registry)

    @staticmethod
    def _drop_web_when_browser(reg):
        """注册表里若已挂上浏览器穿透工具（browser_*），就去掉 web_fetch + web_search，
        逼「读不动也只能在浏览器里 scroll/wait/点进结果」，不再有 web_search 可退。没开穿透时原样返回。"""
        has_browser = any(n.split("__", 1)[-1] in _BROWSE_TOOLS for n in reg.names())
        if has_browser:
            return reg.filtered(lambda n: n not in ("web_fetch", "web_search"))
        return reg

    # ---- 额外授权目录（add-dir，对标 Claude Code）------------------------
    def add_dir(self, path: str) -> dict:
        """授权一个工作区外的目录，之后工具可读写其中文件（默认仍只限工作区）。"""
        try:
            p = Path(path).expanduser().resolve()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        if not p.is_dir():
            return {"ok": False, "error": f"不是有效目录：{path}"}
        if p not in self._extra_dirs:
            self._extra_dirs.append(p)   # 原地改，工具持同一引用、实时生效
        self.emit("extra_dirs", {"dirs": [str(d) for d in self._extra_dirs]})
        return {"ok": True, "dirs": [str(d) for d in self._extra_dirs]}

    def remove_dir(self, path: str) -> dict:
        try:
            p = Path(path).expanduser().resolve()
        except Exception:  # noqa: BLE001
            p = None
        self._extra_dirs[:] = [d for d in self._extra_dirs if d != p]
        self.emit("extra_dirs", {"dirs": [str(d) for d in self._extra_dirs]})
        return {"ok": True, "dirs": [str(d) for d in self._extra_dirs]}

    def get_extra_dirs(self) -> dict:
        return {"dirs": [str(d) for d in self._extra_dirs]}

    def resolve_ask_user(self, req_id: int, answer: str) -> dict:
        """前端回调：用户对 ask_user 的勾选/补充，唤醒等待的工具。"""
        return {"ok": self._ask.resolve(int(req_id), answer)}

    def _subagent_registry(self, role=None):
        """子 Agent 的工具集：同主工具，但**不含** delegate / update_tasks（防嵌套、不碰主清单）；
        再按角色（FR-9.5）做工具限权——只读角色拿不到写/命令工具。role=None 等价 general（全工具）。
        改动台账与主 Agent 共用（子 Agent 写的文件同样可评审/回退）。"""
        res = self.res
        reg = build_registry(
            self.workspace,
            shell=res.config.agent.shell,
            shell_timeout=res.config.agent.shell_timeout,
            screenshot=res.config.agent.screenshot,
            memory_store=res.memory,
            mcp_tools=res.mcp_tools,
            change_tracker=self._on_change,
            process_manager=self.procs,
            web=res.config.web,
            verifier=self._make_verifier(),
            extra_dirs=self._extra_dirs,
            ask_user_binding=self._ask,   # 子 Agent 也能 ask_user：遇登录墙时暂停、让用户在浏览器登录后再继续
        )
        filtered = reg if role is None or role.allow_all else reg.filtered(role.allows)
        # 浏览器穿透开着时，让会浏览的角色（researcher）**一切走浏览器**：去掉 web_fetch + web_search——
        # 没有它们可退，就不会出现「浏览器搜索页读不动→跳回 web_search 绕路瞎逛」（实测真机暴露）。
        # 它会 navigate 到目标站/搜索引擎、点进具体结果、到内容页 snapshot 读。没开穿透时不动、照常都有。
        if role is not None and getattr(role, "allow_browse", False):
            filtered = self._drop_web_when_browser(filtered)
        return filtered

    # ---- 改动评审与回退（FR-9.4a 台账 / FR-10.1 git 语义） ----------------
    # 工作区是 git 仓库时走 git：列全部未提交改动（跨重启、含用户手改），diff 对 HEAD，
    # 回退=丢弃未提交改动；否则沿用内存台账兜底。每次调用动态判定（中途 git init 也生效）。

    def changes_mode(self) -> str:
        return "git" if gitsupport.is_git_workspace(self.workspace) else "ledger"

    def get_changes(self) -> list[dict]:
        if self.changes_mode() == "git":
            try:
                return gitsupport.changes(self.workspace)
            except gitsupport.GitError:
                return []  # git 异常（如未装 git）：面板宁可空，不误导
        return self.ledger.changes()

    def get_file_diff(self, path: str) -> "str | None":
        if self.changes_mode() == "git":
            try:
                return gitsupport.file_diff(self.workspace, path)
            except gitsupport.GitError:
                return None
        return self.ledger.diff(path)

    def revert_file(self, path: str) -> bool:
        if self.changes_mode() == "git":
            try:
                return gitsupport.revert_file(self.workspace, path)
            except gitsupport.GitError:
                return False
        return self.ledger.revert(path)

    def revert_all(self) -> int:
        if self.changes_mode() == "git":
            try:
                return gitsupport.revert_all(self.workspace)
            except gitsupport.GitError:
                return 0
        return self.ledger.revert_all()

    # ---- 检查点（FR-11.6 + P12 方案A 自动打点）：自动/用户创建，用户回退 ------

    def _on_change(self, relpath: str) -> None:
        """写工具落盘前的回调：①给本回合自动检查点累加"该文件改动前的内容"；②记改动台账基线。

        本回合每个文件**首次**改动前，把它当前内容(还没被本次写入改动)记进同一个检查点——
        所以回退它＝撤销本回合对**所有**文件的改动。不靠模型自觉，对标 Claude Code/Cursor。
        """
        rel = (relpath or "").replace("\\", "/").strip()
        if (rel and rel not in self._turn_snap and self.res.config.agent.auto_checkpoint
                and self.res.store and self.session_id is not None):
            p = self.workspace / rel
            try:
                self._turn_snap[rel] = (p.read_text(encoding="utf-8", errors="replace")
                                        if p.is_file() else None)
            except OSError:
                self._turn_snap[rel] = None
            self._upsert_turn_checkpoint()
        self.ledger.snapshot(relpath)

    def _upsert_turn_checkpoint(self) -> None:
        """把本回合累计的改动前快照写进同一个检查点（首次新建、之后更新）。"""
        store = self.res.store
        if self._turn_meta is None:  # 回合内首个被改文件：定格当时的任务/笔记
            self._turn_meta = (store.get_tasks(self.session_id), store.get_notes(self.session_id))
        payload = ckpt.make_payload(dict(self._turn_snap), self._turn_meta[0], self._turn_meta[1])
        if self._turn_ckpt_id is None:
            label = f"改动前 · {self._turn_label}"
            self._turn_ckpt_id = store.add_checkpoint(self.session_id, label, payload)
            store.prune_checkpoints(self.session_id, keep=30)
            self.emit("checkpoint_created", {"id": self._turn_ckpt_id, "label": label, "auto": True})
        else:
            store.update_checkpoint(self._turn_ckpt_id, payload)

    def create_checkpoint(self, label: str, auto: bool = False) -> "int | None":
        """快照 {改过的文件 + 任务清单 + 工作笔记} 存库，返回检查点 id（无会话/无 store→None）。

        auto=True 为系统自动打点（回合改动前）；False 为用户手动。两者都剪枝到最近上限。
        """
        store = self.res.store
        if not store or self.session_id is None:
            return None
        files = ckpt.capture_files(self.workspace, list(self.ledger._baselines))
        payload = ckpt.make_payload(
            files, store.get_tasks(self.session_id), store.get_notes(self.session_id)
        )
        cid = store.add_checkpoint(self.session_id, label, payload)
        store.prune_checkpoints(self.session_id, keep=30)  # 自动打点会累积，留最近 30 个
        self.emit("checkpoint_created", {"id": cid, "label": label, "auto": auto})
        return cid

    def list_checkpoints(self) -> list[dict]:
        store = self.res.store
        if not store or self.session_id is None:
            return []
        return store.list_checkpoints(self.session_id)

    def restore_checkpoint(self, checkpoint_id: int) -> dict:
        """回退到某检查点（破坏性，仅由用户经前端确认触发）：回写文件 + 还原任务/笔记。"""
        store = self.res.store
        if not store or self.session_id is None:
            return {"ok": False, "error": "未启用持久化或会话未保存"}
        cp = store.get_checkpoint(int(checkpoint_id))
        if not cp or cp["session_id"] != self.session_id:
            return {"ok": False, "error": "检查点不存在或不属于本会话"}
        payload = cp["payload"]
        n = ckpt.restore_files(self.workspace, payload.get("files", {}))
        store.set_tasks(self.session_id, payload.get("tasks", []))
        store.set_notes(self.session_id, payload.get("notes", ""))
        self.emit("tasks_updated", {"tasks": payload.get("tasks", [])})
        self.emit("checkpoint_restored", {"id": int(checkpoint_id), "files": n})
        return {"ok": True, "files": n, "label": cp["label"]}

    def set_workspace(self, path) -> None:
        self.workspace = Path(path)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._build_registry()
        self.emit("workspace_changed",
                      {"root": str(self.workspace), "label": self.workspace_label()})

    def workspace_label(self) -> str:
        """顶部标题用的名字：真实项目 -> 文件夹名；空白会话 -> 会话标题；都没有 -> ""。"""
        res = self.res
        ws = self.workspace
        if res.per_session:
            try:
                is_default = (ws == res.workspaces_root / "_scratch"
                              or ws.parent == res.workspaces_root)
            except Exception:  # noqa: BLE001
                is_default = True
            if is_default:  # 非真实项目（空白会话）-> 用会话标题
                if res.store and self.session_id is not None:
                    return res.store.get_session_title(self.session_id) or ""
                return ""
        return ws.name

    # ---- 工作区文件预览（右侧面板，只读） --------------------------------

    def get_workspace_tree(self) -> dict:
        try:
            return {"ok": True, "root": str(self.workspace), "label": self.workspace_label(),
                    "tree": build_tree(self.workspace)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def read_workspace_file(self, path: str) -> dict:
        try:
            return {"ok": True, **read_workspace_file(self.workspace, path or "")}
        except ValueError as e:  # 越界
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def open_workspace_file(self, path: str) -> dict:
        import webbrowser
        from ..workspace import resolve_within
        try:
            p = resolve_within(self.workspace, path or "")
            if not p.is_file():
                return {"ok": False, "error": "文件不存在"}
            webbrowser.open(p.as_uri())
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
