"""Shell 执行工具。平台相关逻辑隔离在此模块（见 CONVENTIONS §6）。

默认走 Windows PowerShell（OQ-2 已确认）。shell 可执行程序与超时由 config 注入，
便于在非 Windows 环境替换或测试。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading

from ..diagnose import with_location
from .base import Tool, ToolError

# 前台命令输出上限（同 procs.py MAX_BUF_CHARS 思路）：疯狂刷 stdout 的命令（yes / 死循环 echo /
# 冗长构建日志）若无上限地读进内存，会在 timeout 到点前就把 Agent 进程 OOM 杀掉（压测实测 exit 137）。
_MAX_OUTPUT_CHARS = 200_000


# hermes 自身模型 provider 的计费密钥（从 .env 经 load_dotenv 进了 os.environ）。这些是给 hermes 调
# 大模型用的，**shell 命令永远用不到**；若原样透传给子 shell，模型跑一句 `env`/`echo $ARK_API_KEY`
# 就能把明文计费密钥打进对话上下文（→ 可能被日志/模型服务端留存＝盗刷风险）。故执行外部命令前剥掉。
# 只剥内置 6 家 provider 的 key（自定义 provider 若用别的 env 名不覆盖，见 CHANGELOG 说明）。
_PROVIDER_KEY_ENVS = frozenset({
    "ARK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY", "MINIMAX_API_KEY", "MOONSHOT_API_KEY",
})


def _strip_secrets(env: dict) -> dict:
    """从将传给子 shell 的环境里剥掉 hermes 的模型 provider 计费密钥（命令用不到、泄露即盗刷）。"""
    for name in _PROVIDER_KEY_ENVS:
        env.pop(name, None)
    return env


def hardened_env() -> dict:
    """在用户环境基础上叠加"非交互硬化"变量，防命令因等交互输入/分页器/凭据/编辑器而静默挂死——这是
    主流 agent（gemini-cli #24707/#21052、claude-code #46078、zed #42943）的通病：`git log` 进 less 等 q、
    `git push` 私库等账号密码、`git commit`(无 -m) 开 vim、apt 问 y/n。Linux 上 start_new_session 无控制
    终端多半 fail-fast，但 **Windows 的 CREATE_NO_WINDOW 不脱离控制台，这些提示会真挂住**，故必须显式压成
    非交互。只叠加不清空用户环境；但会剥掉 hermes 自己的模型 provider 计费密钥（见 _strip_secrets）。"""
    env = _strip_secrets(dict(os.environ))
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


# 疑似"常驻/不自退"服务的命令特征：dev server / watch / REPL / 静态服。命中且用户前台跑时，
# 不再干等满整个 timeout，而是用一个短探针窗口（_PROBE_SECONDS）快速兜底——真是服务就早杀早提示
# "改 background:true"，把"白等几分钟"压成"等十几秒"；若其实会自退（误判），探针内正常结束、零影响。
# 只做"缩短等待+更精准的提示"，不改写命令、不硬拦，故对误判安全。
_LONG_RUNNING_RE = re.compile(
    r"(?:^|[\s;&|])(?:"
    r"streamlit\s+run|uvicorn|gunicorn|hypercorn|daphne|"          # Python web/ASGI/WSGI
    r"flask\s+run|python3?\s+-m\s+(?:http\.server|flask|streamlit)|"  # flask / 内置静态服 / streamlit
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)|"  # 前端脚本约定
    r"vite|next\s+dev|nuxt\s+dev|ng\s+serve|"                      # 前端 dev server
    r"webpack(?:\s+serve|-dev-server)|rollup\s+.*-w\b|"            # 打包器 watch
    r"nodemon|node\s+--watch|tsc\s+.*(?:-w\b|--watch)|"            # node/ts watch
    r"jekyll\s+serve|hugo\s+server|mkdocs\s+serve|"               # 静态站点 dev server
    r"tail\s+-f|watch\s+"                                          # 长跟随
    r")",
    re.IGNORECASE,
)


def _looks_long_running(command: str) -> bool:
    """启发式判断命令是否像"不会自己退出的常驻服务"。命中未必真是（仅缩短前台等待窗口，不改写命令）。"""
    if re.search(r"(?:^|[\s;&|])(?:--watch|--reload|-w\b)", command, re.IGNORECASE):
        return True
    return bool(_LONG_RUNNING_RE.search(command))


_PROBE_SECONDS = 12   # 疑似常驻服务前台跑时的探针窗口：超过它还没退就判定为服务，早杀早提示


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
        # **必须声明 argtypes/restype**：64 位 Windows 上 HANDLE 是 64 位，ctypes 默认把参数/返回值当
        # 32 位 int 会**截断句柄**→ Set/Assign 全失败、job 静默作废退回 taskkill（真机实测记事本仍漏杀）。
        H = wintypes.HANDLE           # = c_void_p，保住 64 位
        k32.CreateJobObjectW.restype = H
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [H, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.CloseHandle.argtypes = [H]
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
    """把进程并入 job（须在它派生子进程前尽早调用；powershell 引擎初始化耗时远大于此，实践中稳）。
    argtypes 必声明，否则 64 位进程句柄被 ctypes 截断→并入失败。"""
    try:
        import ctypes
        from ctypes import wintypes
        H = wintypes.HANDLE
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [H, H]
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:  # noqa: BLE001
        return False


def _win_kill_job(job) -> None:
    """终止 job 内全部进程并关句柄（KILL_ON_JOB_CLOSE 下关句柄本身也会杀，双保险）。
    argtypes 必声明，否则 64 位 job 句柄被截断→杀不到。"""
    try:
        import ctypes
        from ctypes import wintypes
        H = wintypes.HANDLE
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [H, wintypes.UINT]
        k32.CloseHandle.argtypes = [H]
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
                "description": "后台启动**常驻/不会自己退出**的进程，立即返回进程编号；之后用 "
                               "read_process_output 看输出、stop_process 停止。默认 false。"
                               "**判据：命令是否会自己结束？会 dev server/watch/REPL 就设 true。**"
                               "典型必须 true：streamlit run、uvicorn/gunicorn、flask run、npm/pnpm/yarn "
                               "run dev、vite、next dev、python -m http.server、任何带 --watch/--reload 的。"
                               "前台（false）跑这些只会一直等它退出、白白卡到超时再被杀。",
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
            "**启动常驻/不自退的进程（dev server、watch、REPL：如 streamlit run、uvicorn、flask run、"
            "npm run dev、vite、next dev、http.server、带 --watch/--reload 的命令）必须一开始就传 "
            "background:true——别前台跑，前台会一直等它退出、白白卡到超时才被杀。**"
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
        # 疑似常驻服务前台跑：先用短探针窗口等，超过它还没退就当服务处理，早杀早提示（不干等满 180s）。
        suspected = _looks_long_running(command)
        wait_timeout = min(self.timeout, _PROBE_SECONDS) if suspected else self.timeout
        try:
            proc.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc, pgid, job)          # 杀整棵树：连同启动的 GUI/子进程一起关，别留孤儿卡住
            if suspected:
                raise ToolError(
                    f"这条命令看起来是**常驻/不会自己退出的服务**（dev server / watch / REPL），"
                    f"前台跑 {wait_timeout}s 仍未退出，已终止（含其子进程）。**请改用 background:true 后台启动**，"
                    "再用 read_process_output 看输出、stop_process 停止——别前台重试，只会再被杀。")
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
