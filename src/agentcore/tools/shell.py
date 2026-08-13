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
import time
from pathlib import Path

from ..artifacts import format_with_handle, head_tail_of_file
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
        # ---- 下面这批同属"别等人"，按生态补齐（真机反复撞超时后加的，2026-08-11）----
        "SSH_ASKPASS_REQUIRE": "never",   # ssh 不弹 GUI 询问口令的窗口（弹了在无窗口环境=永久挂）
        "GH_PROMPT_DISABLED": "1",        # gh cli 不进交互问答
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NPM_CONFIG_YES": "true",         # npm/npx 的 "Ok to proceed? (y)" 自动过（npm 认 npm_config_*，
        "npm_config_yes": "true",         # 大小写两写：不同版本/平台读法不一致，都给上更保险
        "COMPOSER_NO_INTERACTION": "1",
        "HUSKY": "0",                     # git hook 里跑 lint/开编辑器 → commit 挂住
        "DOTNET_NOLOGO": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "POWERSHELL_UPDATECHECK": "Off",  # pwsh 启动时的更新检查横幅（要联网，网差时拖慢每条命令）
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",   # mac：brew install 前自动 update 常跑几分钟，前台必超时
        "NO_COLOR": "1",                  # 输出里的 ANSI 转义序列对模型是纯噪声（还占 token）
    })
    # 会改变命令语义/可能被用户刻意设过的，用 setdefault：用户显式设了就尊重他的。
    # CI=1 是覆盖面最大的一个开关（大量 CLI 一看到就切非交互），但它也会改测试框架行为
    # （如 jest 不再自动写新快照、playwright 改重试策略）——那是**更该有的** CI 语义，故默认给上，
    # 但不硬覆盖：用户在 .env 里写 CI=0 就按他的来。
    env.setdefault("CI", "1")
    # ssh 首次连主机会问 "Are you sure you want to continue connecting (yes/no)?"——BatchMode 直接失败。
    # 用户配过自己的 GIT_SSH_COMMAND 就不动（他可能挂了代理/指定了私钥）。
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    # 注意：**刻意不设 TERM=dumb**——git 遇到 dumb 终端会打印 "terminal is not fully functional -
    # press RETURN"，反而多一个挂死点。颜色靠 NO_COLOR 关就够了。
    return env


# PowerShell 前缀：进度条在无窗口环境下不但没用，还会把 Invoke-WebRequest / Expand-Archive 拖慢一个
# 数量级（PS 5.1 的老毛病，实测下载能慢 10 倍以上）——慢到撞 timeout，看起来就像"卡死"。
# 只关进度显示，**不碰 $ConfirmPreference/$ErrorActionPreference**：那两个会改命令语义，
# 等于替用户偷偷 auto-yes / 吞错误，属于越界。
_PS_PREFIX = "$ProgressPreference='SilentlyContinue'; "


def build_argv(shell: str, command: str) -> list:
    """拼出真正要执行的 argv。纯函数，便于单测。

    PowerShell 系加进度条前缀；其它 shell 原样。**只影响执行，不影响给模型/产物看的 command 原文**。
    """
    argv = list(_SHELLS[shell])
    if shell in ("powershell", "pwsh"):
        return argv + [_PS_PREFIX + command]
    return argv + [command]


# 疑似"常驻/不自退"服务的命令特征：dev server / watch / REPL / 静态服。命中且用户前台跑时，
# 不再干等满整个 timeout，而是用一个短探针窗口（_PROBE_SECONDS）快速兜底——真是服务就早杀早提示
# "改 background:true"，把"白等几分钟"压成"等十几秒"；若其实会自退（误判），探针内正常结束、零影响。
# 只做"缩短等待+更精准的提示"，不改写命令、不硬拦，故对误判安全。
# `(?:['\"]?[^\s;&|]*[\\/])?` = 可选的**路径前缀**（含可能的开引号）。原来只认裸 `python3`，
# 而真机上路径限定的写法是常态：`.venv\Scripts\python.exe -m http.server`、
# PowerShell 的 `& 'C:\...\python.exe' -m ...`、`/usr/bin/python3 -m ...` 全都匹配不上，
# 于是探针静默失效、用户白等满整个 timeout（2026-08-13 移植测试时发现）。
_LONG_RUNNING_RE = re.compile(
    r"(?:^|[\s;&|])(?:['\"]?[^\s;&|]*[\\/])?(?:"
    r"streamlit\s+run|uvicorn|gunicorn|hypercorn|daphne|"          # Python web/ASGI/WSGI
    r"flask\s+run|python3?(?:\.exe)?['\"]?\s+-m\s+(?:http\.server|flask|streamlit)|"  # flask / 内置静态服 / streamlit
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


# ---- 交互提示识别（P2）：认出"这条命令在等你敲字"，别干等满整个 timeout 再报一句笼统的超时 ----
# 判据刻意保守，三个条件同时成立才算：① 进程还活着 ② 输出**最后一行**长得像提示 ③ 已经安静够久。
# 只靠 ② 会误伤——`--help` 里就有 "[y/N]"、日志里也可能出现 "Password:"；加上 ①③ 后，
# 那些情形要么早退出了、要么还在继续刷输出，都不会命中。
_PROMPT_PATTERNS = [
    r"\[y/n\]\s*[:?]?$",                        # Overwrite? [y/N]
    r"\((?:y|yes)(?:/n|/no)\)\s*[:?]?$",        # (y/n) (yes/no) Ok to proceed? (y)
    r"\(y\)\s*$",
    r"\b(?:y/n|yes/no)\s*[:?]?$",
    r"(?:password|passphrase)[^\n]{0,40}:\s*$",             # Password: / Passphrase for key ...:
    r"username[^\n]{0,40}:\s*$",
    r"are you sure[^\n]{0,60}\?\s*$",
    r"continue connecting[^\n]{0,30}\?\s*$",                # ssh 首次连主机
    r"press (?:any key|enter|return)[^\n]{0,30}$",
    r"请按任意键[^\n]{0,10}$",
    r"(?:是否继续|确认[要否]?继续|继续吗)[^\n]{0,6}[?？]?\s*$",
    # inquirer/prompts 风格：`? Project name:` / `? Select a framework › - Use arrow-keys.`
    # 要求以 "? " 开头，且要么带箭头指示符、要么以冒号/问号收尾——只凭开头的 "?" 太宽。
    r"^\?\s+\S[^\n]*(?:[›❯▸][^\n]*|[:?])\s*$",
    r"^--more--\s*$|^\(end\)\s*$",                          # 分页器停在这儿等 q/空格
    r"(?:select|choose|请选择)[^\n]{0,40}[:：]\s*$",
]
_PROMPT_RE = re.compile("|".join(_PROMPT_PATTERNS), re.IGNORECASE | re.MULTILINE)
_PROMPT_QUIET_SECONDS = 5.0   # 输出静止多久之后才敢下"它在等输入"的结论（防长任务打完提示还接着干活被误杀）
_PROMPT_POLL_SECONDS = 0.25


def looks_waiting_input(text: str) -> "str | None":
    """输出尾巴看起来像"停在提示上等输入"→ 返回那一行提示原文；否则 None。纯函数，便于单测。

    只看**最后一个非空行**：提示的特征就是打完不换行、停在行尾等人。日志里顺带提到 `[y/N]`
    的那行后面通常还有别的输出，于是不是最后一行，自然不会命中。
    """
    tail = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in tail.split("\n") if ln.strip()]
    if not lines:
        return None
    last = lines[-1].strip()
    if len(last) > 300:            # 超长行多半是日志/数据，不是提示
        return None
    return last if _PROMPT_RE.search(last) else None


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
                           capture_output=True, timeout=10,   # **必须带 timeout**：taskkill 本身也是子进程，
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # 机器满载时它自己会卡，
                           # 无 timeout 就把收尾整个挂死→工具永不返回、UI 一直"运行中"（真机 >5min 未返回的根因之一）
        else:
            gid = pgid
            if gid is None:
                try:
                    gid = os.getpgid(proc.pid)
                except OSError:
                    gid = None
            if gid is not None:
                os.killpg(gid, signal.SIGKILL)   # start_new_session 建的进程组，整组杀（含孤儿）
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass   # taskkill 超时也别卡死收尾：下面还有 proc.kill() 兜底
    try:
        if proc.poll() is None:
            proc.kill()          # 兜底：组杀没覆盖到就单杀直接子进程
    except Exception:  # noqa: BLE001
        pass


def _drain(stream, sink, on_delta=None, tee_factory=None) -> None:
    """读线程：把一路输出增量收进 sink（{'parts','total','truncated'}），超上限就丢弃后续但继续读到 EOF
    —— 若停读，写端会因管道写满而永久阻塞（进程卡在 write 上），所以必须一直排空。
    on_delta(chunk)：可选，前台实时流输出用——每读到一段就回调推给前端（读满 4096 才回一段＝天然节流）。
    tee_factory()：可选，**第一次溢出时**才开产物（ADR 0021）——把已缓冲的头部连同后续全部落盘，
    于是"超上限被丢掉的部分"不再永久消失，模型可以 grep/read 产物而不必重跑命令。
    正常大小的命令不会触发，零开销。"""
    tee = None
    dec = _StreamDecoder()
    try:
        while True:
            raw = stream.read1(4096)        # read1：**有多少给多少**，不等攒满（见 _StreamDecoder 注释）
            if not raw:
                chunk = dec.feed(b"", final=True)
                if chunk:
                    sink["parts"].append(chunk)
                    sink["total"] += len(chunk)
                break
            chunk = dec.feed(raw)
            if not chunk:
                continue                    # 半个多字节字符，等下一块凑齐
            sink["ts"] = time.monotonic()   # 最后一次有输出的时刻（交互提示识别要靠它判"安静多久了"）
            if sink["total"] < _MAX_OUTPUT_CHARS:
                sink["parts"].append(chunk)
                sink["total"] += len(chunk)
                if on_delta is not None:
                    try:
                        on_delta(chunk)
                    except Exception:  # noqa: BLE001 — 推流失败绝不影响命令执行/收集
                        pass
            else:
                if not sink["truncated"] and tee_factory is not None:
                    tee = tee_factory()
                    if tee is not None:
                        sink["artifact"] = tee.artifact
                        tee.write("".join(sink["parts"]))   # 补上已在内存里的头部，产物才完整
                sink["truncated"] = True
                if tee is not None:
                    tee.write(chunk)
    except (OSError, ValueError):
        pass
    finally:
        if tee is not None and not tee.close():
            sink["artifact"] = None    # 收尾时不够大被销毁了，别在提示里给个死句柄

class _StreamDecoder:
    """增量解码 + 换行归一，配合 `read1()` 用。

    为什么不再用 `text=True` 让 Python 帮忙解码：`TextIOWrapper.read(4096)` **会一直阻塞到读满
    4096 字符或 EOF**（实测：命令先 printf 一段再 sleep，read 要等到进程结束才返回）。后果有两个——
    ① 所谓"前台实时流输出"对绝大多数命令根本不实时（输出攒不满 4096 就只能等它退出）；
    ② 停在交互提示上的命令，提示文字卡在缓冲里，外面根本看不见、无从识别。
    改成读底层二进制 `read1()`（有多少给多少）+ 这里自己解码，两个问题一起解决。
    """

    def __init__(self) -> None:
        import codecs
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending_cr = False

    def feed(self, raw: bytes, final: bool = False) -> str:
        text = self._dec.decode(raw, final)
        if self._pending_cr:                 # 上一块结尾的 \r 与本块开头的 \n 是同一个换行，别拆成两行
            text = ("" if text.startswith("\n") else "\n") + text
            self._pending_cr = False
        if text.endswith("\r") and not final:
            text = text[:-1]
            self._pending_cr = True
        # 复刻 text=True 的 universal newlines：\r\n 和裸 \r 都归一成 \n（进度条靠 \r 刷新，
        # 归一后是多行，与改动前行为一致）。
        return text.replace("\r\n", "\n").replace("\r", "\n")


def _tail_text(sink, n: int = 2000) -> str:
    """取某一路输出的尾巴（读线程在并发追加，故先快照再拼，不加锁：最坏是少看一段，下轮再看）。"""
    parts = list(sink["parts"])[-8:]
    return "".join(parts)[-n:]


def _wait_with_prompt_watch(proc, timeout: float, sinks):
    """等直接子进程退出，期间盯着输出尾巴认交互提示。

    返回 None＝正常退出；返回字符串＝判定"停在这条提示上等输入"（调用方负责杀树+报错）；
    抛 `subprocess.TimeoutExpired`＝真到点了（调用方沿用原有超时处理）。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=_PROMPT_POLL_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        last_ts = max((s.get("ts") or 0.0) for s in sinks)
        # 一个字都没输出过（last_ts=0）就谈不上"停在提示上"——那是纯挂死，交给 timeout 处理。
        if last_ts and (now - last_ts) >= _PROMPT_QUIET_SECONDS:
            for s in sinks:
                hit = looks_waiting_input(_tail_text(s))
                if hit:
                    return hit
        if now >= deadline:
            raise subprocess.TimeoutExpired(getattr(proc, "args", "?"), timeout)


def _render_stream(text: str, sink, workspace) -> str:
    """把一路输出渲染成给模型看的内容。

    没超上限：原样给（绝大多数命令走这条，零变化）。
    超了上限且落了产物：**回摘要（头+尾）+ 句柄**而不是 20 万字符的头部截断——
      老行为只留头部，恰好把结论（失败汇总/退出码）丢在被截掉的尾部；有了产物两头都能给，
      顺带把这条工具结果从 ~20 万字符压到几 K（ADR 0021 §3）。
    超了上限但没产物：沿用老提示（行为同 3.53）。
    """
    body = text.rstrip()
    if not sink["truncated"]:
        return body
    art = sink.get("artifact")
    if art is None:
        return body + "\n…（输出超上限已截断，需完整日志请把命令输出重定向到文件后分段读）"
    summary = head_tail_of_file(workspace / art.rel, total_lines=art.lines) or body[:8000]
    return format_with_handle(summary, art)


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
    wants_stream = True   # 前台命令支持实时流输出：loop 会给 run() 传 stream 回调，边跑边把输出推前端
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
            "cwd": {
                "type": "string",
                "description": "在工作区内的哪个子目录执行（相对工作区，默认工作区根）。"
                               "在子项目里跑构建/测试时用它，别在 command 里写 `cd xxx && …`。",
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace, *, shell: str = "powershell", timeout: int = 60,
                 process_manager=None, artifacts=None) -> None:
        super().__init__(workspace)
        if shell not in _SHELLS:
            raise ValueError(f"不支持的 shell：{shell}（可选 {list(_SHELLS)}）")
        self.shell = shell
        self.timeout = timeout
        self._procs = process_manager  # FR-10.3：后台进程管理器（None=不支持 background）
        self._artifacts = artifacts    # ADR 0021：产物入口（None=超上限照旧丢弃）
        self.name = f"run_{shell}"
        self.description = (
            f"在工作区目录下执行一条 {shell} 命令并返回输出。"
            "**要在子目录里跑就传 cwd（相对工作区），别写 `cd xxx && …`。**"
            "**启动常驻/不自退的进程（dev server、watch、REPL：如 streamlit run、uvicorn、flask run、"
            "npm run dev、vite、next dev、http.server、带 --watch/--reload 的命令）必须一开始就传 "
            "background:true——别前台跑，前台会一直等它退出、白白卡到超时才被杀。**"
            "**执行环境无终端、stdin 已关闭：任何会等人回答（y/n、选模板、输密码）的命令都过不去，"
            "一开始就加非交互参数（--yes / -y / --non-interactive / --force）或把参数一次给全。**"
            "**读/看文件内容请用 read_file、列目录用 list_dir（它们受工作区与已授权目录约束）；"
            "不要用本工具的 type/cat/Get-Content/dir 去读文件、也不要访问工作区外的路径——"
            "shell 留给真正需要执行的命令。**"
        )

    def _resolve_cwd(self, params: dict) -> str:
        """解析可选的 cwd 入参（受 Tool.resolve 的工作区/add-dir 约束），缺省=工作区根。

        给了这个参数，模型就不必在 command 里拼 `cd sub && …`——那种写法既多一段要过安全判定的
        命令（`&&` 串接里每段都得在只读白名单内才免确认），跨平台写法也不一致（PowerShell/bash）。
        注意这**不是** shell 状态持久化：每次调用仍是独立子进程，cwd 只作用于本次。
        """
        raw = params.get("cwd")
        if not isinstance(raw, str) or not raw.strip():
            return str(self.workspace)
        p = self.resolve(raw.strip())          # 越界路径在这里抛 ToolError
        if not p.is_dir():
            raise ToolError(f"cwd 不是目录：{raw}")
        return str(p)

    def run(self, params: dict, stream=None) -> str:
        command = (params.get("command") or "").strip()
        if not command:
            raise ToolError("命令不能为空")
        argv = build_argv(self.shell, command)
        workdir = self._resolve_cwd(params)
        if params.get("background"):
            if self._procs is None:
                raise ToolError("当前环境未启用后台进程支持，请直接前台执行。")
            entry = self._procs.start(argv, workdir, command)
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
                cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # **二进制管道 + 自己解码**（_StreamDecoder）：text=True 的 read() 会阻塞到读满/EOF，
                # 既让"实时流输出"名不副实，也让停在提示上的命令看不见提示。解码仍是 utf-8/replace——
                # 必显式 utf-8：Windows 中文环境默认 GBK，撞命令的 UTF-8 输出会 UnicodeDecodeError 崩/卡住。
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
        out_sink = {"parts": [], "total": 0, "truncated": False, "artifact": None, "ts": 0.0}
        err_sink = {"parts": [], "total": 0, "truncated": False, "artifact": None, "ts": 0.0}
        def _mk_tee(kind):
            """溢出时才开产物（ADR 0021）；没接产物入口就返回 None，行为同以前=丢弃。"""
            if self._artifacts is None:
                return None
            return lambda: self._artifacts.open_tee(
                tool=self.name, origin=f"{command}  [{kind}]",
                min_chars=self._artifacts.threshold)
        # 前台实时流输出：把每段增量推给前端。共享事件上限，防疯狂刷屏命令灌爆前端（完整输出结束时仍会
        # 一次性返回，流只是"边跑边看"）；超上限后停止推流但命令照常跑、输出照常收集。
        _stream_budget = {"n": 0}
        _STREAM_MAX_EVENTS = 800
        def _mk_delta(kind):
            if stream is None:
                return None
            def _on(chunk):
                if _stream_budget["n"] >= _STREAM_MAX_EVENTS:
                    return
                _stream_budget["n"] += 1
                stream(kind, chunk)
            return _on
        t_out = threading.Thread(target=_drain,
                                 args=(proc.stdout, out_sink, _mk_delta("stdout"), _mk_tee("stdout")),
                                 daemon=True)
        t_err = threading.Thread(target=_drain,
                                 args=(proc.stderr, err_sink, _mk_delta("stderr"), _mk_tee("stderr")),
                                 daemon=True)
        t_out.start()
        t_err.start()
        # 疑似常驻服务前台跑：先用短探针窗口等，超过它还没退就当服务处理，早杀早提示（不干等满 180s）。
        suspected = _looks_long_running(command)
        wait_timeout = min(self.timeout, _PROBE_SECONDS) if suspected else self.timeout
        try:
            waiting_on = _wait_with_prompt_watch(proc, wait_timeout, (out_sink, err_sink))
        except subprocess.TimeoutExpired:
            _terminate_tree(proc, pgid, job)          # 杀整棵树：连同启动的 GUI/子进程一起关，别留孤儿卡住
            if suspected:
                raise ToolError(
                    f"这条命令看起来是**常驻/不会自己退出的服务**（dev server / watch / REPL），"
                    f"前台跑 {wait_timeout}s 仍未退出，已终止（含其子进程）。**请改用 background:true 后台启动**，"
                    "再用 read_process_output 看输出、stop_process 停止——别前台重试，只会再被杀。")
            # 笼统的超时按概率把三种成因分开讲：混成一句会让模型把"等输入"的命令也丢去 background，
            # 结果它在后台照样等输入、更没人管（真机反复出现过）。
            raise ToolError(
                f"命令超时（>{self.timeout}s）已终止（含其启动的子进程）。按可能性排查：\n"
                "① **它根本不会自己退出**（GUI 应用 / dev server / watch / 安装向导）→ 改用 "
                "background:true 后台启动，再用 read_process_output 看输出；前台重试只会再超时。\n"
                "② 它确实慢（装依赖 / 编译 / 全量测试）→ 缩小范围分批跑，或同样改 background:true 再轮询输出。\n"
                "③ 它在等输入但没打印出可识别的提示 → 加非交互参数（--yes / -y / --non-interactive）后重试。")
        if waiting_on is not None:
            _terminate_tree(proc, pgid, job)
            raise ToolError(
                f"命令停在**交互提示**上等输入（已静止 {int(_PROMPT_QUIET_SECONDS)}s），已终止。\n"
                f"提示原文：`{waiting_on}`\n"
                "前台执行**没有终端、stdin 已关闭**，没人能替它敲这个回答。两条出路，按顺序试：\n"
                "① **首选**改成非交互写法重跑：加 --yes / -y / --non-interactive / --force，"
                "或一次把参数给全（如 `npm create vite@latest app -- --template react`）。\n"
                "② 确实没有非交互写法时：用 background:true 重新起，再 read_process_output 看到提示、"
                "用 write_process_input 逐条回答。\n**别原样前台重试**，只会再次停在同一处。")
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
            parts.append(f"[stdout]\n{_render_stream(stdout, out_sink, self.workspace)}")
        if stderr:
            parts.append(f"[stderr]\n{_render_stream(stderr, err_sink, self.workspace)}")
        # 报错定位（FR-13.B）：输出含指向工作区文件的 traceback 时附加 file:line + 源码上下文。
        # 按**实际 cwd** 解析：traceback 里的相对路径是相对命令的工作目录的，传 workspace 会在
        # cwd 是子目录时找不到文件（定位块静默消失）。cwd 缺省即 workspace，常见情形行为不变。
        return with_location("\n".join(parts), Path(workdir))
