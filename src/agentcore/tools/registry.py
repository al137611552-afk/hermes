"""工具注册表：集中构造所有工具、产出 schema、按名取用。"""
from __future__ import annotations

from pathlib import Path

from ..store.memory import MemoryStore
from .ask import AskUserBinding, AskUserTool
from .recall import RecallHistoryTool
from .base import Tool, ToolError
from .codesearch import CodeOutlineTool, FindSymbolTool
from .delegate import DelegateBinding, DelegateTool
from .fixture import CaptureFixtureTool
from .fs import EditFileTool, ListDirTool, MultiEditTool, ReadFileTool, WriteFileTool
from .git import GitBranchTool, GitCommitTool, GitDiffTool, GitLogTool, GitStatusTool
from .memory import ForgetTool, RecallTool, RememberTool
from .notes import NotesBinding, UpdateNotesTool
from .procs import ProcessListTool, ProcessOutputTool, ProcessStopTool
from .screenshot import ScreenshotTool
from .search import GlobSearchTool, GrepSearchTool
from .searchcode import SearchCodeTool
from .shell import RunShellTool
from .tasks import TaskBinding, UpdateTasksTool
from .trace import TraceRunTool
from .web import WebFetchTool, WebSearchTool


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def to_schemas(self) -> list[dict]:
        """产出给 provider 的工具 schema 列表（Anthropic 原生格式）。"""
        return [t.to_schema() for t in self._tools.values()]

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"未知工具：{name}")
        return self._tools[name]

    def is_dangerous(self, name: str) -> bool:
        return name in self._tools and self._tools[name].dangerous

    def names(self) -> list[str]:
        return list(self._tools)

    def filtered(self, keep) -> "ToolRegistry":
        """返回只保留 keep(tool_name)==True 的工具的新注册表（用于子 Agent 角色限权）。"""
        return ToolRegistry([t for t in self._tools.values() if keep(t.name)])


def build_registry(
    workspace: Path,
    *,
    shell: str = "powershell",
    shell_timeout: int = 60,
    screenshot: bool = True,
    memory_store: MemoryStore | None = None,
    mcp_tools: list[Tool] | None = None,
    task_binding: TaskBinding | None = None,
    notes_binding: NotesBinding | None = None,
    delegate_binding: DelegateBinding | None = None,
    change_tracker=None,
    process_manager=None,
    web=None,
    verifier=None,
    extra_dirs=None,
    ask_user_binding=None,
    history_search=None,
) -> ToolRegistry:
    """按 config 构造默认工具集。

    screenshot=False 时不注册截屏工具（隐私 kill-switch；即便注册，执行也过权限 gate）。
    memory_store 非 None 时注册长期记忆工具 remember / recall / forget（P6.3）。
    mcp_tools 为来自外部 MCP server 的工具（P6.4），默认 dangerous、过权限 gate。
    task_binding 非 None 时注册任务清单工具 update_tasks（FR-9.1，非危险）。
    delegate_binding 非 None 时注册委派工具 delegate（FR-9.3）；子 Agent 的注册表不传它
    （也不传 task_binding），从而排除 delegate / update_tasks，避免无限嵌套与污染主清单。
    change_tracker 为改动台账回调（FR-9.4a，写/编辑前快照基线）；主与子 Agent 传同一个。
    process_manager 为后台进程管理器（FR-10.3）：注入 run_<shell> 的 background 模式并注册
    list/read/stop 三工具；每对话一个、主与子 Agent 共用；None 时不支持后台（行为同 2.1.0）。
    web 非 None 且 enabled 时注册联网检索 web_search/web_fetch（FR-11.1）。
    verifier 为写入后零成本语法校验回调（FR-11.2a）：注入给 write/edit/multi_edit，落盘后校验。
    """
    tools: list[Tool] = [
        ReadFileTool(workspace),
        WriteFileTool(workspace, tracker=change_tracker, verifier=verifier),
        EditFileTool(workspace, tracker=change_tracker, verifier=verifier),
        MultiEditTool(workspace, tracker=change_tracker, verifier=verifier),
        ListDirTool(workspace),
        GrepSearchTool(workspace),
        GlobSearchTool(workspace),
        CodeOutlineTool(workspace),
        FindSymbolTool(workspace),
        SearchCodeTool(workspace),  # 按相关性检索代码（大库按意图定位）
        RunShellTool(workspace, shell=shell, timeout=shell_timeout,
                     process_manager=process_manager),
        TraceRunTool(workspace),  # 运行时值追踪（FR-13.D）：debug 看中间值，dangerous 过 gate
        CaptureFixtureTool(workspace),  # 失败固化 fixture（FR-13.E）：复现变可复现，dangerous 过 gate
        # git 工具（FR-10.1）常注册：非 git 仓库时返回可读错误（中途 git init 后即可用）
        GitStatusTool(workspace),
        GitDiffTool(workspace),
        GitLogTool(workspace),
        GitCommitTool(workspace),
        GitBranchTool(workspace),
    ]
    if process_manager is not None:
        tools += [
            ProcessListTool(process_manager),
            ProcessOutputTool(process_manager),
            ProcessStopTool(process_manager),
        ]
    if web is not None and getattr(web, "enabled", False):
        # 联网检索（FR-11.1）：只读、免 gate；enabled:false 不注册（行为同 3.0.0）
        tools += [
            WebSearchTool(engine=web.search_engine, timeout=web.timeout,
                          max_results=web.max_results),
            WebFetchTool(timeout=web.timeout, max_chars=web.fetch_max_chars),
        ]
    if screenshot:
        tools.append(ScreenshotTool(workspace))
    if memory_store is not None:
        tools += [
            RememberTool(memory_store),
            RecallTool(memory_store),
            ForgetTool(memory_store),
        ]
    if task_binding is not None:
        tools.append(UpdateTasksTool(task_binding))
    if notes_binding is not None:
        tools.append(UpdateNotesTool(notes_binding))
    if delegate_binding is not None:
        tools.append(DelegateTool(delegate_binding))
    if ask_user_binding is not None:
        tools.append(AskUserTool(ask_user_binding))
    if history_search is not None:
        tools.append(RecallHistoryTool(history_search))
    if mcp_tools:
        tools += mcp_tools
    if extra_dirs is not None:  # 额外授权目录（add-dir）：注入共享引用，add/remove 后所有工具实时生效
        for t in tools:
            t.extra_dirs = extra_dirs
    return ToolRegistry(tools)
