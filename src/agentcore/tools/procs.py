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

MAX_BUF_CHARS = 200_000   # 每进程输出环形缓冲上限
MAX_READ_CHARS = 50_000   # 单次 read_process_output 返回上限
MAX_PROCS = 8             # 每对话并发后台进程上限

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

    def __init__(self, pid_id: int, command: str, proc: subprocess.Popen) -> None:
        self.id = pid_id
        self.command = command
        self.proc = proc
        self.buffer = ""        # 环形缓冲（超限丢最旧）
        self.read_upto = 0      # 增量读游标（相对当前 buffer）
        self.trimmed = False    # 是否丢过最旧输出
        self.started_at = time.time()

    def status(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited({code})"


class ProcessManager:
    """每对话一个：后台启动、增量读输出、停止、退出时全部清理。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._procs: dict[int, _Entry] = {}

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
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", **kwargs,
            )
        except FileNotFoundError:
            raise ToolError(f"找不到可执行程序：{argv[0]}")
        with self._lock:
            self._seq += 1
            entry = _Entry(self._seq, command, proc)
            self._procs[entry.id] = entry
        threading.Thread(target=self._reader, args=(entry,), daemon=True).start()
        return entry

    def _reader(self, entry: _Entry) -> None:
        """读线程：把进程输出收进环形缓冲（进程退出/管道关闭即结束）。"""
        try:
            for line in entry.proc.stdout:  # type: ignore[union-attr]
                with self._lock:
                    entry.buffer += line
                    if len(entry.buffer) > MAX_BUF_CHARS:
                        cut = len(entry.buffer) - MAX_BUF_CHARS
                        entry.buffer = entry.buffer[cut:]
                        entry.read_upto = max(0, entry.read_upto - cut)
                        entry.trimmed = True
        except (OSError, ValueError):
            pass

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
        return {"new_output": new, "status": entry.status(),
                "trimmed": trimmed, "truncated": truncated}

    # ---- 停止 / 清理 -------------------------------------------------------

    def _kill_tree(self, entry: _Entry) -> None:
        proc = entry.proc
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
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
        if r["trimmed"]:
            parts.append("[提示] 输出过多，最旧部分已被丢弃")
        if r["truncated"]:
            parts.append(f"[提示] 本次新增超 {MAX_READ_CHARS} 字符，只保留末尾")
        parts.append(f"[新增输出]\n{r['new_output'].rstrip()}" if r["new_output"].strip()
                     else "(无新输出)")
        return "\n".join(parts)


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
