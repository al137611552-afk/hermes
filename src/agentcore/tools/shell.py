"""Shell 执行工具。平台相关逻辑隔离在此模块（见 CONVENTIONS §6）。

默认走 Windows PowerShell（OQ-2 已确认）。shell 可执行程序与超时由 config 注入，
便于在非 Windows 环境替换或测试。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

from ..diagnose import with_location
from .base import Tool, ToolError

# 前台命令输出上限（同 procs.py MAX_BUF_CHARS 思路）：疯狂刷 stdout 的命令（yes / 死循环 echo /
# 冗长构建日志）若无上限地读进内存，会在 timeout 到点前就把 Agent 进程 OOM 杀掉（压测实测 exit 137）。
_MAX_OUTPUT_CHARS = 200_000


def hardened_env() -> dict:
    """在用户环境基础上叠加"非交互硬化"变量，防命令因等交互输入/分页器/凭据/编辑器而静默挂死——这是
    主流 agent（gemini-cli #24707/#21052、claude-code #46078、zed #42943）的通病：`git log` 进 less 等 q、
    `git push` 私库等账号密码、`git commit`(无 -m) 开 vim、apt 问 y/n。Linux 上 start_new_session 无控制
    终端多半 fail-fast，但 **Windows 的 CREATE_NO_WINDOW 不脱离控制台，这些提示会真挂住**，故必须显式压成
    非交互。只叠加不清空用户环境（模型 key 等仍在）。"""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",   # git 绝不弹账号/密码提示：私库/需鉴权时快速失败而非静默挂死
        "GIT_PAGER": "cat",           # git 分页命令（log/diff/branch/show）不进 less 等 q
        "PAGER": "cat",               # 通用分页器兜底（git 未覆盖的命令 & 其它工具）
        "GIT_EDITOR": "true",         # git commit(无 -m)/rebase -i 不开 vim 干等，空操作快速返回
        "EDITOR": "true",             # 通用编辑器兜底
        "VISUAL": "true",
        "DEBIAN_FRONTEND": "noninteractive",  # apt/dpkg 不弹交互配置界面
        "PIP_NO_INPUT": "1",          # pip 不等输入
        "PYTHONUNBUFFERED": "1",      # 子 Python 输出即时刷出（否则块缓冲、超时前看不到进度/挂着像卡死）
    })
    return env


def _win_create_job():
    """建 kill-on-close 的 Windows Job Object，返回句柄（失败/非 Windows 返回 None）。归入 job 的进程，
    其子孙**自动入同一 job**，且**被 ShellExecute/Start-Process 重定父后仍留在 job 内**——这正是
    `taskkill /PID <ps> /T` 靠父子 PID 链遍历会漏掉 `Start-Process notepad` 的根因（真机实测：powershell
    被杀、记事本逃逸）。job 一键 TerminateJobObject 全杀，是 Chromium/VSCode/pytest-timeout 的通用做法。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _LIMIT), ("IoInfo", _IOC),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001 — 任何环境不支持都退回 taskkill
        return None


def _win_assign_job(job, proc) -> bool:
    """把进程并入 job（须在它派生子进程前尽早调用；powershell 引擎初始化耗时远大于此，实践中稳）。"""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:  # noqa: BLE001
        return False


def _win_kill_job(job) -> None:
    """终止 job 内全部进程并关句柄（KILL_ON_JOB_CLOSE 下关句柄本身也会杀，双保险）。"""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            k32.TerminateJobObject(job, 1)
        finally:
            k32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


def _terminate_tree(proc, pgid=None, job=None) -> None:
    """终止进程及其整棵子树（同 procs.ProcessManager._kill_tree）。前台命令收尾/超时时连带关掉它启动的
    GUI/后台子进程，别留孤儿：否则启动了不自退的程序（GUI / `&` 起的守护）会一直挂着、下次尝试再挂一个
    （真机反馈"打开程序就卡住、反复几次"）。Windows 优先用传入的 job 整体杀（含被重定父的 GUI，taskkill
    /T 会漏），再 taskkill 兜底；POSIX 用建组时抓好的 pgid——直接子进程一旦被 wait 回收，getpgid(pid) 会
    失败，但用一开始存下的 pgid 仍能把整组（含继承管道的孤儿）杀干净。"""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            if job is not None:
                _win_kill_job(job)   # 整个 job 全杀：含 Start-Process/ShellExecute 重定父的 GUI 进程
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            gid = pgid
            if gid is None:
                try:
                    gid = os.getpgid(proc.pid)
                except OSError:
                    gid = None
            if gid is not None:
                os.killpg(gid, signal.SIGKILL)   # start_new_session 建的进程组，整组杀（含孤儿）
    except (OSError, ProcessLookupError):
        pass
    try:
        if proc.poll() is None:
            proc.kill()          # 兜底：组杀没覆盖到就单杀直接子进程
    except Exception:  # noqa: BLE001
        pass


def _drain(stream, sink) -> None:
    """读线程：把一路输出增量收进 sink（{'parts','total','truncated'}），超上限就丢弃后续但继续读到 EOF
    —— 若停读，写端会因管道写满而永久阻塞（进程卡在 write 上），所以必须一直排空。"""
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if sink["total"] < _MAX_OUTPUT_CHARS:
                sink["parts"].append(chunk)
                sink["total"] += len(chunk)
            else:
                sink["truncated"] = True
    except (OSError, ValueError):
        pass

# config.agent.shell 取值 -> 命令行模板。{cmd} 处填模型给的命令。
_SHELLS = {
    "powershell": ["powershell", "-NoProfile", "-NonInteractive", "-Command"],
    "pwsh": ["pwsh", "-NoProfile", "-NonInteractive", "-Command"],
    "cmd": ["cmd", "/c"],
    "bash": ["bash", "-lc"],  # macOS / Linux 默认
    "zsh": ["zsh", "-lc"],    # macOS 登录 shell（可在 config 显式选）
}


class RunShellTool(Tool):
    dangerous = True
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "background": {
                "type": "boolean",
                "description": "后台启动长进程（dev server/watch 等），立即返回进程编号；"
                               "之后用 read_process_output 看输出、stop_process 停止。默认 false",
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace, *, shell: str = "powershell", timeout: int = 60,
                 process_manager=None) -> None:
        super().__init__(workspace)
        if shell not in _SHELLS:
            raise ValueError(f"不支持的 shell：{shell}（可选 {list(_SHELLS)}）")
        self.shell = shell
        self.timeout = timeout
        self._procs = process_manager  # FR-10.3：后台进程管理器（None=不支持 background）
        self.name = f"run_{shell}"
        self.description = (
            f"在工作区目录下执行一条 {shell} 命令并返回输出。"
            "长时间运行的命令（dev server、watch）传 background:true 后台启动。"
            "**读/看文件内容请用 read_file、列目录用 list_dir（它们受工作区与已授权目录约束）；"
            "不要用本工具的 type/cat/Get-Content/dir 去读文件、也不要访问工作区外的路径——"
            "shell 留给真正需要执行的命令。**"
        )

    def run(self, params: dict) -> str:
        command = (params.get("command") or "").strip()
        if not command:
            raise ToolError("命令不能为空")
        argv = _SHELLS[self.shell] + [command]
        if params.get("background"):
            if self._procs is None:
                raise ToolError("当前环境未启用后台进程支持，请直接前台执行。")
            entry = self._procs.start(argv, str(self.workspace), command)
            return (f"已在后台启动进程 #{entry.id}（pid {entry.proc.pid}）：{command}\n"
                    "用 read_process_output 看输出（增量）、list_processes 查看、stop_process 停止。")
        # 前台执行用 Popen + **等直接子进程退出**（proc.wait），而不是 communicate()。
        # 关键区别（压测实测）：communicate() 等的是"管道 EOF"——若命令用 `&` 后台起了继承 stdout 的
        # 子进程（如 `sleep 30 & echo started`），shell 明明已 echo 完退出，管道却因孤儿还占着写端而不 EOF，
        # 于是白白挂满 timeout 再被当超时报错、还误杀了守护。改为只等直接子进程退出：shell 一退就立即返回。
        # 输出走带上限的读线程，避免疯狂刷屏命令把内存撑爆（OOM）。
        proc = None
        pgid = None
        job = None
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 防黑窗
        else:
            kwargs["start_new_session"] = True    # 独立进程组，便于 killpg 整组杀（含继承管道的孤儿）
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.workspace),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8", errors="replace",   # 必显式 utf-8：Windows 中文环境 text=True 默认 GBK，
                                                       # 撞命令的 UTF-8 输出会 UnicodeDecodeError 崩/卡住
                stdin=subprocess.DEVNULL,             # 交互式命令（npm create / npm init 等）拿到 EOF 快速失败
                env=hardened_env(),                   # 非交互硬化：防 git 分页器/凭据/编辑器、apt y/n 等挂死
                **kwargs,
            )
        except FileNotFoundError:
            raise ToolError(f"找不到 {self.shell} 可执行程序。")
        if sys.platform == "win32":
            job = _win_create_job()                   # 尽早把 shell 并入 job，其子孙（含 Start-Process
            if job is not None and not _win_assign_job(job, proc):  # 起的 GUI）自动入 job，重定父也跑不掉
                _win_kill_job(job)                    # 并入失败：回收空 job（内无进程，不误杀），退回 taskkill
                job = None
        else:
            try:
                pgid = os.getpgid(proc.pid)           # 趁子进程还活着抓好进程组号，供收尾/超时整组杀
            except OSError:
                pgid = proc.pid
        out_sink = {"parts": [], "total": 0, "truncated": False}
        err_sink = {"parts": [], "total": 0, "truncated": False}
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_sink), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_sink), daemon=True)
        t_out.start()
        t_err.start()
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc, pgid, job)          # 杀整棵树：连同启动的 GUI/子进程一起关，别留孤儿卡住
            raise ToolError(
                f"命令超时（>{self.timeout}s）已终止（含其启动的子进程）。"
                "**若启动的是不会自己退出的常驻程序（GUI 应用 / dev server / watch / 安装向导），必须改用 "
                "background:true 后台启动**——前台执行会一直等它退出，只会再次超时，别重试前台。"
                "若是交互式命令（会问 y/n），加非交互参数（如 --yes / -y）。")
        # 直接子进程已退出：先杀掉任何被它 `&` 留下、继承了管道的孤儿——前台契约=同步跑完，命令退出后
        # 不该还有它派生的进程存活（正是最初 GUI 挂住的根因）。**必须先杀再 join**：孤儿占着管道写端时
        # 读线程的 read() 会一直阻塞等 EOF（读不到 shell 已写入的短输出如 "started"）；杀掉孤儿→写端全关
        # →管道 EOF，读线程读完 OS 缓冲里的残余输出后自然结束。已写入缓冲的数据不会因杀进程而丢。
        _terminate_tree(proc, pgid, job)
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stdout = "".join(out_sink["parts"])
        stderr = "".join(err_sink["parts"])

        parts = [f"[exit code] {proc.returncode}"]
        if stdout:
            note = "\n…（输出超上限已截断，需完整日志请把命令输出重定向到文件后分段读）" if out_sink["truncated"] else ""
            parts.append(f"[stdout]\n{stdout.rstrip()}{note}")
        if stderr:
            note = "\n…（输出超上限已截断）" if err_sink["truncated"] else ""
            parts.append(f"[stderr]\n{stderr.rstrip()}{note}")
        # 报错定位（FR-13.B）：输出含指向工作区文件的 traceback 时附加 file:line + 源码上下文
        return with_location("\n".join(parts), self.workspace)
