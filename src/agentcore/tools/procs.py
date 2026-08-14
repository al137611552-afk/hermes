"""后台命令/长进程（FR-10.3）：ProcessManager + list/read/stop 三工具。

对标 Claude Code 的 run_in_background / BashOutput / KillShell：
- run_<shell> 加 background:true 后台启动（见 shell.py），返回进程号；
- read_process_output **增量语义**——每次只回上次读取之后的新输出（轮询日志）；
- 输出由读线程收进环形缓冲（上限 MAX_BUF_CHARS，溢出丢最旧并标记）；
- stop_process / 关窗清理时**杀整棵进程树**（shell 下面挂的 dev server 一起走）：
  Windows 用 taskkill /T /F + CREATE_NO_WINDOW 防黑窗；POSIX 用进程组 killpg。
平台相关逻辑集中本模块（CONVENTIONS §6）；list/read 只读不过 gate，stop 只能停
本对话后台启动的进程（也不过 gate）。
"""
from __future__ import annotations

import os
import signal
import re
import subprocess
import threading
import time

from .base import Tool, ToolError
from .shell import (hardened_env, looks_waiting_input, _StreamDecoder,
                    _win_create_job, _win_assign_job, _win_kill_job)

MAX_BUF_CHARS = 200_000   # 每进程输出环形缓冲上限
# 回投给会话的尾部输出上限：够判断成败即可。投多了白烧上下文，模型要细节可以再 read_process_output。
_NOTIFY_TAIL_CHARS = 2_000
MAX_WAIT_MINUTES = 120        # 等待器时长硬上限：**不许无声挂死**（ADR 0026 已知限制）
MIN_POLL_SECONDS = 5          # 轮询下限：别把站外接口打成 DDoS，也别把本机烤了
MAX_READ_CHARS = 50_000   # 单次 read_process_output 返回上限
MAX_PROCS = 8             # 每对话并发后台进程上限
PROMPT_QUIET_SECONDS = 2.0  # 后台进程静止多久后才敢说它"停在提示上等输入"（前台是 5s：前台判错要杀
                            # 进程，代价大；后台只是多给一句提示，可以更灵敏）

# 实时预览面板（UX Tier1-②）：从 dev server 输出/命令里识别本地 URL，供前端 iframe 自动对准。
_LOCAL_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d{2,5})?(?:/[^\s'\"]*)?", re.I)


def extract_localhost_url(text: str) -> "str | None":
    """从一段文本（dev server 输出/命令）里抽第一个本地 URL；0.0.0.0 归一成 localhost，
    去掉尾随标点。识别不到返回 None。纯函数、便于单测。"""
    if not text:
        return None
    m = _LOCAL_URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;)]}'\"")
    return url.replace("://0.0.0.0", "://localhost")


# 命令行里识别 dev server 端口（兜底）：很多 server 的 "Serving on http://..." 打在 stdout，
# piped 时块缓冲、短时间刷不出来 → 从命令行抽端口拼 http://localhost:PORT 让前端先对准。
_PORT_RES = [
    re.compile(r"--port[=\s]+(\d{2,5})", re.I),
    re.compile(r"\bhttp[.\-]server\s+(\d{2,5})", re.I),       # python -m http.server 8000 / http-server 8080
    re.compile(r"(?:^|\s)-p[=\s]+(\d{2,5})\b"),               # -p 3000
    re.compile(r"\brunserver\b\D{0,12}(\d{2,5})", re.I),      # django runserver [host:]8000
    re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})", re.I),
]


def url_from_command(command: str) -> "str | None":
    """从命令行抽 dev server 端口拼成 http://localhost:PORT（buffer 抓不到 URL 时兜底）。
    保守：只认显式端口/已知 server 形态；拼出的 URL 仅作前端预填、用户可改。识别不到返回 None。"""
    if not command:
        return None
    for rx in _PORT_RES:
        m = rx.search(command)
        if m:
            return f"http://localhost:{m.group(1)}"
    return None


class _Entry:
    """一个后台进程：Popen + 输出缓冲 + 增量读游标。"""

    notify_on_exit = False    # ADR 0026 W1：退出时是否回投事实给会话

    def __init__(self, pid_id: int, command: str, proc: subprocess.Popen, job=None) -> None:
        self.id = pid_id
        self.command = command
        self.proc = proc
        self.job = job          # Windows Job Object 句柄（含被 Start-Process 重定父的 GUI，taskkill 会漏）
        self.buffer = ""        # 环形缓冲（超限丢最旧）
        self.read_upto = 0      # 增量读游标（相对当前 buffer）
        self.trimmed = False    # 是否丢过最旧输出
        self.tee = None         # ADR 0021 §7：读线程边收边落盘的完整日志产物（None=没接产物入口）
        self.started_at = time.time()
        self.last_output_at = 0.0   # 最后一次有输出的时刻（判"是不是停在提示上等输入"）

    def status(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited({code})"


class ProcessManager:
    """每对话一个：后台启动、增量读输出、停止、退出时全部清理。线程安全。"""

    def __init__(self, artifacts=None, on_event=None) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._procs: dict[int, _Entry] = {}
        # 等待器（ADR 0026 W1）：id -> {"thread","cancel","command","deadline"}
        self._waiters: dict[int, dict] = {}
        # 条件成立/进程退出时把**事实**回投给会话（由 Conversation 注入）。
        # **只投事实不下结论**（ADR 0026 决策 3）：要不要继续、怎么继续由模型判断。
        self.on_event = on_event
        # ADR 0021 §7：产物入口。环形缓冲是"一边收一边丢最旧"，**重跑也拿不回来**被冲掉的早期日志，
        # 所以这里不能等到工具返回时才落盘，必须在读线程里 tee。随工作区变（由 conversation 赋值）。
        self.artifacts = artifacts

    # ---- 启动 -------------------------------------------------------------

    def start(self, argv: list[str], cwd: str, command: str) -> _Entry:
        with self._lock:
            running = sum(1 for e in self._procs.values() if e.proc.poll() is None)
            if running >= MAX_PROCS:
                raise ToolError(
                    f"后台进程已达上限（{MAX_PROCS} 个运行中）。"
                    "先用 stop_process 停掉不需要的，或用 list_processes 查看。"
                )
        kwargs: dict = {}
        if os.name == "nt":
            # 杀树靠 taskkill /T；CREATE_NO_WINDOW 防 GUI 应用下黑窗闪烁
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True  # 独立进程组，便于 killpg 杀树
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # stdin 开 PIPE（原来是 DEVNULL）：后台进程是**唯一还能回答交互提示**的形态，
                # 由 write_process_input 往里写（过权限 gate）。注意这不等于"自动 yes"——
                # 每一句都是模型或用户显式发出、可见、可确认的（ADR 0022）。
                stdin=subprocess.PIPE,
                # 二进制管道 + 自己解码：同 shell.py 的理由，`for line in stdout` 只按行切，
                # **交互提示恰恰不带换行**（"Ok to proceed? (y) "），按行读就永远看不到它。
                env=hardened_env(),   # 同前台：非交互硬化，防后台 dev server 因分页/凭据/编辑器提示卡住
                **kwargs,
            )
        except FileNotFoundError:
            raise ToolError(f"找不到可执行程序：{argv[0]}")
        job = None
        if os.name == "nt":
            job = _win_create_job()               # 并入 job：子孙（含 Start-Process 起的 GUI）重定父也跑不掉
            if job is not None and not _win_assign_job(job, proc):
                _win_kill_job(job)                # 并入失败：回收空 job，退回 taskkill
                job = None
        with self._lock:
            self._seq += 1
            entry = _Entry(self._seq, command, proc, job)
            self._procs[entry.id] = entry
        sink = self.artifacts
        if sink is not None:
            # min_chars=0：始终保留。小输出虽然环形缓冲也没丢，但句柄一旦给出去就不能中途消失。
            entry.tee = sink.open_tee(tool="run_shell(background)", origin=command)
        threading.Thread(target=self._reader, args=(entry,), daemon=True).start()
        return entry

    def _reader(self, entry: _Entry) -> None:
        """读线程：把进程输出收进环形缓冲，同时 tee 一份完整日志到产物（进程退出/管道关闭即结束）。

        用 `read1()` 而非按行迭代：**交互提示不带换行**（`Ok to proceed? (y) `），按行读会把它
        一直压在缓冲里，`read_process_output` 永远看不到 → "起后台再回答"这条路根本走不通。
        """
        dec = _StreamDecoder()
        try:
            stream = entry.proc.stdout
            while True:
                raw = stream.read1(4096)  # type: ignore[union-attr]
                chunk = dec.feed(raw or b"", final=not raw)
                if chunk:
                    if entry.tee is not None:
                        entry.tee.write(chunk)  # 落盘在裁剪之前：被环形缓冲冲掉的早期输出仍留在产物里
                    with self._lock:
                        entry.buffer += chunk
                        entry.last_output_at = time.time()
                        if len(entry.buffer) > MAX_BUF_CHARS:
                            cut = len(entry.buffer) - MAX_BUF_CHARS
                            entry.buffer = entry.buffer[cut:]
                            entry.read_upto = max(0, entry.read_upto - cut)
                            entry.trimmed = True
                if not raw:
                    break
        except (OSError, ValueError):
            pass
        finally:
            if entry.tee is not None:
                entry.tee.close()   # 进程结束即定稿；文件在这之前也一直可读（append + flush）
            # ADR 0026 W1：**进程退出即通知**（调别的 agent / 长跑软件的主场景）。
            # 这个 finally 本来只是收尾，加一行就成了"站外任务干完了"的信号源——
            # 以前它只是自己结束、谁也不告诉，于是 agent 永远不知道那件事已经完了。
            if getattr(entry, "notify_on_exit", False) and self.on_event:
                # **管道关闭 ≠ 进程已退出**：这个 finally 是在 stdout EOF 时跑的，此刻
                # 直接 poll() 可能还拿到 None，通知里就成了 "exit=None"——把"不知道"
                # 说成了一个退出码。先等它真的收尾（管道已排干，wait 不会卡住），
                # 加个上限兜底防意外挂死。**CI 在 Windows 上抓到的就是这个**：
                # Linux 下这场竞争通常侥幸赢了，Windows 下输了。
                try:
                    code = entry.proc.wait(timeout=10)
                except Exception:  # noqa: BLE001 — 超时/异常就如实报未知，别编一个码
                    code = entry.proc.poll()
                with self._lock:
                    tail = entry.buffer[-_NOTIFY_TAIL_CHARS:]
                shown = "未知" if code is None else code
                self._fire(f"后台进程 #{entry.id} 已退出（exit={shown}）：{entry.command}",
                           tail, entry.id)

    # ---- 回投事实 / 等待器（ADR 0026 W1）------------------------------------

    def _fire(self, headline: str, tail: str, ref: int) -> None:
        """把一条**事实**回投给会话。绝不加"你应该去修"这类指导（决策 3）。"""
        if not self.on_event:
            return
        body = headline
        if tail.strip():
            body += f"\n--- 尾部输出 ---\n{tail.strip()}"
        try:
            self.on_event(body, ref)
        except Exception:  # noqa: BLE001 — 回投失败不能影响进程管理本身
            pass

    def start_waiter(self, argv: list[str], cwd: str, command: str,
                     poll_seconds: int, timeout_minutes: int) -> int:
        """周期重跑 `command` 直到它 exit 0（或超时），期间**零模型成本**。

        用于站外条件（CI 跑完了没、云端任务好了没）——那些事没有本地进程可等，
        只能问。区别于 `/crazy` 自驱轮询：那是每轮烧一次模型调用去问"好了没"。
        """
        poll = max(int(poll_seconds or 30), MIN_POLL_SECONDS)
        minutes = min(max(int(timeout_minutes or 30), 1), MAX_WAIT_MINUTES)
        deadline = time.time() + minutes * 60
        with self._lock:
            self._seq += 1
            wid = self._seq
            cancel = threading.Event()
            self._waiters[wid] = {"cancel": cancel, "command": command, "deadline": deadline}

        def _loop() -> None:
            n = 0
            started = time.time()
            while not cancel.is_set():
                # deadline **以台账里的值为准**，不用闭包快照：否则 waiters() 报的剩余时间
                # 和实际生效的可能是两个数（单一事实来源）。取不到＝已被摘掉，直接收工。
                with self._lock:
                    w = self._waiters.get(wid)
                    dl = w["deadline"] if w else None
                if dl is None:
                    return
                if time.time() >= dl:
                    self._drop_waiter(wid)
                    self._fire(f"等待超时：条件在 {int(time.time() - started)}s 内始终未成立"
                               f"（已试 {n} 次）：{command}", "", wid)
                    return
                n += 1
                try:
                    r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=poll * 2)
                    code = r.returncode
                    out = (r.stdout or b"").decode("utf-8", "replace")[-_NOTIFY_TAIL_CHARS:]
                except Exception as e:  # noqa: BLE001 — 单次探测失败不算条件成立，也不该终止等待
                    code, out = None, f"（第 {n} 次探测出错：{type(e).__name__}: {e}）"
                if code == 0:
                    self._drop_waiter(wid)
                    self._fire(f"等待条件已成立（第 {n} 次探测，等了 "
                               f"{int(time.time() - started)}s）：{command}", out, wid)
                    return
                cancel.wait(poll)   # 可被 stop 立即打断，不是死 sleep

        t = threading.Thread(target=_loop, daemon=True)
        with self._lock:
            self._waiters[wid]["thread"] = t
        t.start()
        return wid

    def _drop_waiter(self, wid: int) -> None:
        with self._lock:
            self._waiters.pop(wid, None)

    def stop_waiter(self, wid: int) -> bool:
        with self._lock:
            w = self._waiters.get(wid)
        if not w:
            return False
        w["cancel"].set()
        self._drop_waiter(wid)
        return True

    def waiters(self) -> list[dict]:
        with self._lock:
            return [{"id": k, "command": v["command"],
                     "remaining_s": max(0, int(v["deadline"] - time.time()))}
                    for k, v in self._waiters.items()]

    # ---- 写输入（P3 / ADR 0022）---------------------------------------------

    def write_input(self, pid_id: int, text: str, submit: bool = True) -> str:
        """往运行中的后台进程 stdin 写一行。**这是"能回答提示"的唯一通道**。

        写进去的内容会**回显进输出缓冲**（`[已输入] …`）——终端里你敲的字本来就会回显，
        这里进程拿不到 TTY 不会自己回显，那就由我们补上：否则日志上下文对不齐，
        模型（和人）翻记录时看不出这个 `y` 是谁答的。
        """
        entry = self._get(pid_id)
        if entry.proc.poll() is not None:
            raise ToolError(f"进程 #{pid_id} 已经结束（{entry.status()}），没法再写输入。")
        stdin = entry.proc.stdin
        if stdin is None:
            raise ToolError(f"进程 #{pid_id} 没有可写的 stdin（可能不是本对话 background 启动的）。")
        payload = text if not submit else (text + "\n")
        try:
            stdin.write(payload.encode("utf-8"))
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise ToolError(
                f"往进程 #{pid_id} 写输入失败（{e.__class__.__name__}）——它可能刚退出或已关闭 stdin。"
                "用 read_process_output 看看它最后输出了什么。")
        echo = f"\n[已输入] {text}\n"
        if entry.tee is not None:
            entry.tee.write(echo)
        with self._lock:
            entry.buffer += echo
            entry.last_output_at = time.time()
        return (f"已向进程 #{pid_id} 写入：{text}"
                + ("（含回车）" if submit else "（未加回车）")
                + "\n用 read_process_output 看它接下来的反应。")

    # ---- 查询 / 读输出 -----------------------------------------------------

    def _get(self, pid_id: int) -> _Entry:
        entry = self._procs.get(pid_id)
        if entry is None:
            raise ToolError(f"没有进程 #{pid_id}（用 list_processes 查看现有后台进程）。")
        return entry

    def list(self) -> list[dict]:
        with self._lock:
            entries = list(self._procs.values())
        return [{
            "id": e.id, "pid": e.proc.pid, "status": e.status(),
            "command": e.command, "elapsed": int(time.time() - e.started_at),
            "output_chars": len(e.buffer),
        } for e in entries]

    def preview_targets(self) -> list[dict]:
        """实时预览面板用：运行中的后台进程里，能识别出本地预览 URL 的（先扫输出 buffer，
        回退命令）。最新启动的排前面（dev server 通常是最近开的）。已退出的不列。"""
        with self._lock:
            entries = sorted(self._procs.values(), key=lambda e: e.started_at, reverse=True)
            snap = [(e.id, e.command, e.buffer, e.proc.poll()) for e in entries]
        targets = []
        for pid_id, command, buffer, code in snap:
            if code is not None:           # 只列运行中的
                continue
            url = (extract_localhost_url(buffer)        # 输出里有完整 URL（最可靠）
                   or extract_localhost_url(command)    # 命令里写了完整 URL（少见）
                   or url_from_command(command))        # 命令里能抽出端口（stdout 缓冲抓不到时兜底）
            if url:
                targets.append({"id": pid_id, "command": command, "url": url})
        return targets

    def read(self, pid_id: int) -> dict:
        entry = self._get(pid_id)
        with self._lock:
            new = entry.buffer[entry.read_upto:]
            entry.read_upto = len(entry.buffer)
            trimmed = entry.trimmed
            entry.trimmed = False
        truncated = len(new) > MAX_READ_CHARS
        if truncated:
            new = new[-MAX_READ_CHARS:]
        art = entry.tee.artifact if entry.tee is not None else None
        return {"new_output": new, "status": entry.status(),
                "trimmed": trimmed, "truncated": truncated,
                "artifact_rel": art.rel if art else "", "artifact_id": art.id if art else "",
                "waiting_on": self.waiting_prompt(pid_id)}

    def waiting_prompt(self, pid_id: int) -> "str | None":
        """这个后台进程是不是**停在交互提示上等输入**？是则返回提示原文。

        判据与前台一致（`looks_waiting_input` + 静止阈值 + 进程还活着），保持一套口径：
        前台会因此被杀掉并劝改写命令，后台则给出"可以用 write_process_input 回答"的出口。
        """
        entry = self._procs.get(pid_id)
        if entry is None or entry.proc.poll() is not None:
            return None
        with self._lock:
            tail = entry.buffer[-2000:]
            last_at = entry.last_output_at
        if not last_at or (time.time() - last_at) < PROMPT_QUIET_SECONDS:
            return None
        return looks_waiting_input(tail)

    # ---- 停止 / 清理 -------------------------------------------------------

    def _kill_tree(self, entry: _Entry) -> None:
        proc = entry.proc
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                if entry.job is not None:
                    _win_kill_job(entry.job)      # 整个 job 全杀：含被重定父的 GUI（taskkill /T 会漏）
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,   # 带 timeout：满载时 taskkill 自己会卡，无它则收尾挂死
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass   # taskkill 超时也别卡死：下面 proc.wait/kill 兜底
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    def stop(self, pid_id: int) -> str:
        entry = self._get(pid_id)
        if entry.proc.poll() is not None:
            return f"进程 #{pid_id} 早已结束（{entry.status()}）。"
        self._kill_tree(entry)
        return f"已停止进程 #{pid_id}（{entry.command}）。"

    def cancel_all_waiters(self) -> int:
        """会话关闭 / app 退出时清掉等待器——别留下没人认领的幽灵轮询。"""
        with self._lock:
            ws = list(self._waiters.values())
            self._waiters.clear()
        for w in ws:
            w["cancel"].set()
        return len(ws)

    def kill_all(self) -> int:
        """杀掉所有仍在运行的后台进程（关窗/删会话运行时调用），返回清理数。"""
        with self._lock:
            entries = list(self._procs.values())
        n = 0
        for e in entries:
            if e.proc.poll() is None:
                self._kill_tree(e)
                n += 1
        return n


# ---- 工具 --------------------------------------------------------------------

class ProcessListTool(Tool):
    name = "list_processes"
    description = "列出本对话用 background:true 启动的后台进程（编号/状态/命令/运行时长）。只读。"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, manager: ProcessManager) -> None:  # 不需要 workspace
        self._m = manager

    def run(self, params: dict) -> str:
        procs = self._m.list()
        if not procs:
            return "(没有后台进程)"
        lines = [
            f"#{p['id']} [{p['status']}] {p['elapsed']}s pid={p['pid']} "
            f"输出{p['output_chars']}字符  {p['command']}"
            for p in procs
        ]
        return "\n".join(lines)


class ProcessOutputTool(Tool):
    name = "read_process_output"
    description = (
        "读取某个后台进程自上次读取以来的**新增**输出（增量，适合轮询 dev server / 长任务日志），"
        "并报告其运行状态。只读。"
    )
    input_schema = {
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "list_processes 里的进程编号"}},
        "required": ["id"],
    }

    def __init__(self, manager: ProcessManager) -> None:
        self._m = manager

    def run(self, params: dict) -> str:
        try:
            pid_id = int(params.get("id"))
        except (TypeError, ValueError):
            raise ToolError("id 应为整数（list_processes 里的进程编号）")
        r = self._m.read(pid_id)
        parts = [f"[状态] {r['status']}"]
        handle = (f"——但**完整日志已落产物 {r['artifact_id']}**：{r['artifact_rel']}"
                  "（grep_search / read_file 它，别重启进程）") if r.get("artifact_rel") else ""
        if r["trimmed"]:
            parts.append("[提示] 输出过多，最旧部分已被丢弃" + handle)
        if r["truncated"]:
            parts.append(f"[提示] 本次新增超 {MAX_READ_CHARS} 字符，只保留末尾" + handle)
        parts.append(f"[新增输出]\n{r['new_output'].rstrip()}" if r["new_output"].strip()
                     else "(无新输出)")
        if r.get("waiting_on"):
            # 认出"它停在提示上等你回答"，并直接给出出口——这是后台相对前台的**唯一**优势：
            # 前台没有句柄、只能杀掉劝你改命令；后台还能回答。
            parts.append(
                f"[提示] 这个进程似乎**停在交互提示上等输入**：`{r['waiting_on']}`\n"
                f"       要回答就用 write_process_input(id={pid_id}, text=\"y\")；"
                "不该继续就 stop_process。")
        return "\n".join(parts)


class ProcessInputTool(Tool):
    """往运行中的后台进程写一行输入（P3 / ADR 0022）。

    **立场**：hermes 不做全局 auto-yes（确认框是防误删的最后一道闸），但可以"逐次、看得见、
    过确认地回答"。所以这是个 dangerous 工具：每一句输入都会弹权限确认，用户能看清写的是什么。
    """

    dangerous = True
    name = "write_process_input"
    description = (
        "向某个**运行中的后台进程**的 stdin 写一行（用于回答它的交互提示：y/n、选项、名称等）。"
        "典型用法：命令需要交互 → 用 run_<shell> 的 background:true 起 → read_process_output 看到提示 "
        "→ 本工具回答 → 再 read_process_output 看反应。"
        "**注意：能不交互就别交互**——优先给命令加 --yes/-y/--non-interactive 或一次把参数给全；"
        "只有在没有非交互写法时才用这条路。危险确认（删除/覆盖/发布）请如实转达给用户，别替他答应。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "list_processes 里的进程编号"},
            "text": {"type": "string", "description": "要写入的一行内容，如 y / 1 / my-app（不含换行）"},
            "submit": {"type": "boolean",
                       "description": "是否自动补回车提交，默认 true。少数需要单键响应的场景设 false。"},
        },
        "required": ["id", "text"],
    }

    def __init__(self, manager: ProcessManager) -> None:
        self._m = manager

    def run(self, params: dict) -> str:
        try:
            pid_id = int(params.get("id"))
        except (TypeError, ValueError):
            raise ToolError("id 应为整数（list_processes 里的进程编号）")
        text = params.get("text")
        if text is None or not str(text).strip():
            raise ToolError("text 不能为空（要回答什么就写什么，如 y）")
        text = str(text)
        if "\n" in text or "\r" in text:
            # 一次一行：多行会把后续几个提示一股脑answered掉，用户在确认条上也看不清到底答了什么。
            raise ToolError("text 只能是一行（要连续回答多个提示，就分多次调用，每次一行）。")
        return self._m.write_input(pid_id, text, submit=params.get("submit", True) is not False)


class ProcessStopTool(Tool):
    name = "stop_process"
    description = "停止某个后台进程（连同其子进程整树终止）。只能停本对话 background 启动的进程。"
    input_schema = {
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "list_processes 里的进程编号"}},
        "required": ["id"],
    }

    def __init__(self, manager: ProcessManager) -> None:
        self._m = manager

    def run(self, params: dict) -> str:
        try:
            pid_id = int(params.get("id"))
        except (TypeError, ValueError):
            raise ToolError("id 应为整数（list_processes 里的进程编号）")
        return self._m.stop(pid_id)
