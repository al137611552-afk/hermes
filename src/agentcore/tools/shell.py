"""Shell 执行工具。平台相关逻辑隔离在此模块（见 CONVENTIONS §6）。

默认走 Windows PowerShell（OQ-2 已确认）。shell 可执行程序与超时由 config 注入，
便于在非 Windows 环境替换或测试。
"""
from __future__ import annotations

import subprocess

from ..diagnose import with_location
from .base import Tool, ToolError

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
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",   # 必显式 utf-8：Windows 中文环境 text=True 默认 GBK，
                                                       # 撞命令的 UTF-8 输出会在读取线程 UnicodeDecodeError 崩/卡住
                stdin=subprocess.DEVNULL,             # 交互式命令（npm create / npm init 等）拿到 EOF 快速失败，
                                                       # 而非干等输入卡到超时（后台进程那条路已是 DEVNULL）
                timeout=self.timeout,
            )
        except FileNotFoundError:
            raise ToolError(f"找不到 {self.shell} 可执行程序。")
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"命令超时（>{self.timeout}s）。若是长运行进程（dev server / 安装），改用 background:true 后台启动；"
                "若是交互式命令（npm create / npm init 等会问 y/n），加非交互参数（如 --yes / -y）。")

        parts = [f"[exit code] {proc.returncode}"]
        if proc.stdout:
            parts.append(f"[stdout]\n{proc.stdout.rstrip()}")
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
        # 报错定位（FR-13.B）：输出含指向工作区文件的 traceback 时附加 file:line + 源码上下文
        return with_location("\n".join(parts), self.workspace)
