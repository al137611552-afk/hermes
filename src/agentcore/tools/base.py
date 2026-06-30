"""工具抽象与工作区路径安全。

所有工具实现统一 schema 并注册到 registry，优先兼容 MCP 的 name/description/
input_schema 三要素（见 CONVENTIONS §6）。危险工具（写文件/执行命令）标记
dangerous=True，由 agent 循环在执行前过权限 gate。
"""
from __future__ import annotations

import difflib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class ToolError(Exception):
    """工具执行中可预期的错误（路径越界、文件不存在等），信息直接回灌给模型。"""


@dataclass(eq=False)
class ToolOutput:
    """工具的结构化返回（用于需要回灌图片等富内容块的工具，如截屏）。

    - text：给模型看的纯文本，作为该工具的 tool_result 内容。
    - blocks：额外内容块（如 image 块）。部分 Anthropic 兼容端点（实测火山方舟）
      不解析 tool_result 内嵌的图片，故这些块由 agent 循环作为「并列块」追加到
      tool_result 所在的同一条 user 消息里，模型即可正常看到。

    普通工具直接返回 str 即可（向后兼容），无需用本类。
    """
    text: str
    blocks: list[dict] = field(default_factory=list)

    def __eq__(self, other) -> bool:  # 与 str 比较按 text（向后兼容旧断言 out == "..."）
        if isinstance(other, str):
            return self.text == other
        return (isinstance(other, ToolOutput)
                and self.text == other.text and self.blocks == other.blocks)

    def __contains__(self, item: str) -> bool:  # 让 `"x" in out` 等价于 in out.text（向后兼容）
        return item in self.text

    def __str__(self) -> str:
        return self.text


def make_diff_block(rel: str, before: str, after: str, max_lines: int = 240) -> "dict | None":
    """生成给前端内联展示的 diff 块（type=diff）。无变化返回 None。
    注意：该块**不回灌给模型**（见 loop._exec_calls 过滤）——模型自己就知道改了什么。"""
    if before == after:
        return None
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=rel, tofile=rel, lineterm="", n=2,
    ))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… (diff 已截断，本次共 {len(lines)} 行)"]
    return {"type": "diff", "path": rel, "diff": "\n".join(lines)}


def output_with_diff(text: str, block: "dict | None"):
    """有 diff 块则返回 ToolOutput（带 diff，仅前端展示），否则返回纯 str（向后兼容）。"""
    return ToolOutput(text, [block]) if block else text


class Tool(ABC):
    name: str
    description: str
    input_schema: dict
    dangerous: bool = False
    extra_dirs: "tuple | list" = ()  # 额外授权目录（对标 Claude Code add-dir）；build_registry 注入共享引用

    def __init__(self, workspace: Path) -> None:
        # 所有相对路径都相对工作区根目录解析
        self.workspace = workspace.resolve()

    @abstractmethod
    def run(self, params: dict) -> "str | ToolOutput":
        """执行工具，返回给模型看的结果。

        通常返回纯文本 str；需要回灌图片等富内容的工具返回 ToolOutput。
        可预期错误请抛 ToolError（会被转成 tool_result 回灌模型，不中断循环）。
        """
        raise NotImplementedError

    # ---- 路径安全 --------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        """把工具入参里的路径解析到工作区（或已授权的额外目录）内，拒绝其它路径。
        相对路径相对工作区根；绝对路径须落在工作区或某个已授权目录（add-dir）里。"""
        if not rel:
            raise ToolError("路径不能为空")
        p = (self.workspace / rel).resolve()
        if self._within(p, self.workspace) or any(self._within(p, d) for d in self.extra_dirs):
            return p
        raise ToolError(f"拒绝访问工作区外的路径：{rel}")

    @staticmethod
    def _within(p: Path, base: Path) -> bool:
        return p == base or base in p.parents

    def to_schema(self) -> dict:
        """Anthropic 原生工具 schema（OpenAI provider 内部再转换）。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
