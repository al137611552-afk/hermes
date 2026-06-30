"""自动生成项目规范 hermes.md 的纯逻辑（可单测、不碰网络）。

工作区缺 hermes.md 且已有项目内容时，据「全局开发标准 + 本项目摘要」提炼生成一份
**本项目专属**的规范。因工作区已按会话隔离，摘要只含本项目，不会卷入无关内容。
"""
from __future__ import annotations

import re
from pathlib import Path

from .providers import Message
from .workspace import build_tree

# 优先纳入摘要的关键文件（存在才读）
_KEY_FILES = [
    "README.md", "README", "readme.md", "README.rst",
    "package.json", "pyproject.toml", "setup.py", "requirements.txt",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "composer.json",
    "tsconfig.json", "Makefile", "Dockerfile", ".gitignore",
]
_DIGEST_MAX = 8000          # 摘要总字符上限
_PER_FILE_MAX = 2000        # 单个关键文件纳入的字符上限


def _flatten_tree(node: dict, depth: int = 0, lines: list[str] | None = None) -> list[str]:
    if lines is None:
        lines = []
    for c in node.get("children", []):
        mark = "📁" if c["type"] == "dir" else "📄"
        lines.append(f"{'  ' * depth}{mark} {c['name']}")
        if c["type"] == "dir":
            _flatten_tree(c, depth + 1, lines)
    return lines


def build_project_digest(root: Path) -> str:
    """构建项目摘要：目录结构 + 关键文件内容（均限定在 root 内、带上限）。"""
    root = Path(root)
    parts: list[str] = ["【目录结构】"]
    parts.extend(_flatten_tree(build_tree(root)))

    for name in _KEY_FILES:
        p = root / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:_PER_FILE_MAX]
        except OSError:
            continue
        parts.append(f"\n【{name}】\n{text}")

    digest = "\n".join(parts)
    return digest[:_DIGEST_MAX]


_SYSTEM = (
    "你在为一个软件项目生成「项目规范」文件（hermes.md，类似 Claude Code 的 CLAUDE.md），"
    "供后续开发时自动加载遵守。请**只依据下面给出的本项目摘要**，写出本项目**特有**的内容。\n"
    "注意：通用开发标准（如先读后改、分段交付、最小改动、如实报告等）**已在系统层面始终生效，"
    "无需写进本文件**；只需参考其组织方式，不要照搬其内容，也**绝不要纳入与本项目无关的内容**"
    "（例如其它项目、或开发工具自身的文件/细节）。\n"
    "包含（有信息才写、没有就略）：项目一句话、技术栈、常用命令（安装/运行/测试/构建）、"
    "目录结构速览、本项目特有的代码规范、已知坑。保持简洁、高信号。\n"
    "直接输出 hermes.md 的 Markdown 正文，不要任何额外解释、不要用 ``` 包裹整篇。"
)


def build_generate_request(digest: str, global_standard: str | None) -> tuple[str, list[Message]]:
    gs = (global_standard or "").strip()
    # 全局标准已始终生效，这里仅作为"组织方式/已覆盖项"的参考，避免重复写进 hermes.md
    gs_block = f"通用开发标准（已始终生效，仅供参考组织方式、勿照搬其内容）：\n{gs}\n\n" if gs else ""
    user = f"{gs_block}本项目摘要：\n{digest}\n\n请输出本项目专属的 hermes.md 正文。"
    return _SYSTEM, [Message("user", user)]


def clean_output(text: str) -> str:
    """清洗模型输出：去掉可能把整篇包起来的 ``` 代码围栏。"""
    s = (text or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```$", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    return s
