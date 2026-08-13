"""跨平台 shell 测试底座：把「用哪个 shell / 命令怎么写」收敛到一处。

**为什么要有它**（2026-08-13 CI 首次在真 Windows 上跑回归时暴露）：测试里 `shell="bash"` 和
`["bash", "-lc"]` 散落在 9 个文件共 37 处。Windows runner 上 `bash` 解析到
`C:\\Windows\\System32\\bash.exe`——那是 **WSL 存根**，没装发行版时只会返回 exit 1 加一段
UTF-16LE 的英文提示。于是一批测试在 Windows 上必红，而且**每修一层才露出下一层**
（这些 runner 遇错即停，一轮 CI 每个文件只吐一个 bit）。

规矩：
- 要跑真命令的测试，一律用 `SHELL` / `SHELL_ARGV`，别写死 `"bash"`。
- 命令文本用下面的构造器，别直接拼 POSIX 语法。
- **真·POSIX 专属的语义**（`&` 后台 + `wait`、`read -n 1` 单键读）没有干净的 PowerShell 等价物，
  那种用例请显式 `if IS_WIN: return` 跳过并在注释里写清为什么，别硬凑一个语义不同的版本——
  凑出来的"绿"比红更糟。
"""
from __future__ import annotations

import shlex
import sys

IS_WIN = sys.platform.startswith("win")

# 与 config._resolve_shell 同一套映射：Windows→powershell，其余→bash。
# 用 Windows PowerShell（5.1，系统自带）而非 pwsh：runner 和用户真机都一定有前者。
SHELL = "powershell" if IS_WIN else "bash"
SHELL_ARGV = ["powershell", "-NoProfile", "-Command"] if IS_WIN else ["bash", "-lc"]

# 注册表里的执行工具随 shell 改名（run_bash / run_powershell）——
# 写死 "run_bash" 在 Windows 上 reg.get 直接找不到。
RUN_TOOL = f"run_{SHELL}"


# ---- hook 命令（**cmd.exe 味，不是 PowerShell**）---------------------------------
# HookRunner 在 Windows 上走 ["cmd", "/c", cmd]（见 hooks.py），跟上面的 SHELL_ARGV 是两套东西。
# **hook 的 stdout 一律只用 ASCII**：英文 Windows 的 cmd 代码页是 cp437/850，中文经 cmd 的 echo
# 出来就是那个代码页的字节，而 hooks.py 按 utf-8 解码（errors="replace"）——中文断言必然落空。
# 这不是测试将就，是 hook 作者在 Windows 上真会踩的坑。

def hook_exit(code: int) -> str:
    """以指定码退出。cmd 里 `exit N` 会关掉整个 cmd 进程，要用 `exit /b N` 才是返回码。"""
    return f"exit /b {code}" if IS_WIN else f"exit {code}"


def hook_echo(text: str) -> str:
    """打印一行（ASCII）。cmd 的 echo **不剥引号**，写 `echo 'x'` 会把引号一起打出来。"""
    return f"echo {text}" if IS_WIN else f"echo '{text}'"


def hook_echo_exit(text: str, code: int) -> str:
    """打印一行再以指定码退出。"""
    return f"{hook_echo(text)}& {hook_exit(code)}" if IS_WIN else f"{hook_echo(text)}; {hook_exit(code)}"


def hook_deny_if_stdin_has(needle: str, marker: str) -> str:
    """stdin（hook 收到的 JSON）里含 needle 就打印 marker 并 exit 2，否则 exit 0。"""
    if IS_WIN:
        return f'findstr /C:"{needle}" >nul && (echo {marker}& exit /b 2) || exit /b 0'
    return f"grep -q {needle} <<<\"$(cat)\" && {{ echo '{marker}'; exit 2; }} || exit 0"


def hook_stdin_to_file(path) -> str:
    """把 hook 收到的 stdin 原样落盘。cmd 没有 `cat`，`findstr "^"` 是最接近的替代
    （匹配所有行、原样转发）。**注意它按行处理**：末行没有换行也会被补上 CRLF，
    所以只适合落 JSON 这类对尾部空白不敏感的内容。"""
    return f'findstr "^" > "{path}"' if IS_WIN else f"cat > {path}"


def echo(text: str) -> str:
    """打印一行。两边的 `echo` 都成立（PowerShell 里是 Write-Output 的别名）。"""
    return f"echo {text}"


def sleep(seconds: float) -> str:
    """睡 N 秒。"""
    return f"Start-Sleep {seconds}" if IS_WIN else f"sleep {seconds}"


def seq(*cmds: str) -> str:
    """顺序执行；两边都用 `;` 分隔。"""
    return "; ".join(cmds)


def big_output(lines: int, width: int) -> str:
    """产出 `lines` 行、每行 `width` 个 x——用来撑爆输出上限、验产物落盘。"""
    if IS_WIN:
        return f"1..{lines} | ForEach-Object {{ 'x' * {width} }}"
    return f"for i in $(seq 1 {lines}); do printf 'x%.0s' {{1..{width}}}; echo; done"


def read_var(name: str) -> str:
    """读一行标准输入到变量（等回车）。注意 PowerShell 的 `Read-Host` **会回显**读到的内容，
    bash 的 `read` 在非 tty 下不回显——断言别依赖"缓冲里只出现一次"。
    单键读（bash 的 `read -n 1`）没有可用等价物，见模块头部说明。
    """
    return f"${name} = Read-Host" if IS_WIN else f"read {name}"


def tick_loop(n: int, text: str, gap: float) -> str:
    """连打 n 行、每行间隔 gap 秒——验「还在刷输出时不该判定为在等输入」。"""
    if IS_WIN:
        return f"1..{n} | ForEach-Object {{ echo \"{text} $_\"; Start-Sleep {gap} }}"
    return f"for i in $(seq 1 {n}); do echo {text} $i; sleep {gap}; done"


def python_c(code: str) -> str:
    """跑一小段 Python。用 `sys.executable` 而不是字面量 `python3`——**Windows 上 PATH 里
    经常只有 `python`**，写死 `python3` 在真机和 runner 上都可能落空。
    PowerShell 里可执行路径要用 `&` 调用运算符，否则带空格/盘符的路径会被当成裸字符串。
    """
    if IS_WIN:
        return f"& '{sys.executable}' -c \"{code}\""
    return f"{shlex.quote(sys.executable)} -c \"{code}\""


def echo_no_newline(text: str) -> str:
    """打印但**不补换行**——验"提示停在行尾也要能看见"。"""
    if IS_WIN:
        return f"Write-Host -NoNewline '{text}'"
    return f"printf '{text}'"
