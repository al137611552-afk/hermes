"""Shell 执行工具。平台相关逻辑隔离在此模块（见 CONVENTIONS §6）。

默认走 Windows PowerShell（OQ-2 已确认）。shell 可执行程序与超时由 config 注入，
便于在非 Windows 环境替换或测试。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

from ..diagnose import with_location
from .base import Tool, ToolError


def _terminate_tree(proc) -> None:
    """终止进程及其整棵子树（同 procs.ProcessManager._kill_tree）。前台命令超时时连带关掉它启动的 GUI/子进程，
    别留孤儿：否则启动了不自退的程序（如 GUI）会一直挂着、下次尝试再挂一个（真机反馈"打开程序就卡住、反复几次"）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # start_new_session 建的进程组，整组杀
    except Exception:  # noqa: BLE001 — 尽力而为，杀不掉退回单进程 kill，别抛
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
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
        # Popen（非 subprocess.run）：超时时能拿到 proc 去**杀整棵树**——run() 只杀直接子进程，
        # `&` 启动的 GUI 会成孤儿留在屏幕上（真机 bug：打开程序就卡住、关不掉、反复几次）。
        proc = None
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 防黑窗
        else:
            kwargs["start_new_session"] = True    # 独立进程组，便于超时 killpg 整组杀
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.workspace),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8", errors="replace",   # 必显式 utf-8：Windows 中文环境 text=True 默认 GBK，
                                                       # 撞命令的 UTF-8 输出会 UnicodeDecodeError 崩/卡住
                stdin=subprocess.DEVNULL,             # 交互式命令（npm create / npm init 等）拿到 EOF 快速失败
                **kwargs,
            )
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except FileNotFoundError:
            raise ToolError(f"找不到 {self.shell} 可执行程序。")
        except subprocess.TimeoutExpired:
            _terminate_tree(proc)                     # 杀整棵树：连同启动的 GUI/子进程一起关，别留孤儿卡住
            raise ToolError(
                f"命令超时（>{self.timeout}s）已终止（含其启动的子进程）。"
                "**若启动的是不会自己退出的常驻程序（GUI 应用 / dev server / watch / 安装向导），必须改用 "
                "background:true 后台启动**——前台执行会一直等它退出，只会再次超时，别重试前台。"
                "若是交互式命令（会问 y/n），加非交互参数（如 --yes / -y）。")

        parts = [f"[exit code] {proc.returncode}"]
        if stdout:
            parts.append(f"[stdout]\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"[stderr]\n{stderr.rstrip()}")
        # 报错定位（FR-13.B）：输出含指向工作区文件的 traceback 时附加 file:line + 源码上下文
        return with_location("\n".join(parts), self.workspace)
