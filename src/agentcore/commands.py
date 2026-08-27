"""自定义斜杠命令（FR-13.C1）：把常用问法固化成 `/盯盘` 这种确定性入口。

对标 Claude Code 的 `.claude/commands/*.md`——**一个命令 = 一个 Markdown 文件**，文件名即命令名，
frontmatter 放元数据、正文是提示词模板。与技能的分工：

  技能（skills.py）＝ 教模型"什么时候用、怎么用"，**语义触发**，用不用由模型判断；
  命令（本模块）　　＝ 用户打出来就必然执行的**确定性入口**，可以绑定技能。

两种模式：
  - `prompt`（默认）：正文是提示词模板，`$ARGUMENTS` 替换成命令后面跟的参数，展开后当作用户输入发出去。
    模型照常判断要调什么工具/技能——能带参数、能组合，代价是多一次模型判断。
  - `exec`：frontmatter 的 `command:` 是命令行模板（同样支持 `$ARGUMENTS`），打了就跑。
    正文可选，用来交代"结果回来后要模型做什么"；正文为空＝输出直接贴回对话，不惊动模型。
    **命令照常过权限 gate**（要免确认走 `agent.permissions.allow` 的具体命令前缀规则，不因为它是命令就放行）。

安全立场：**内置命令不可被同名自定义命令覆盖**。`/crazy` 是免确认自主模式，允许覆盖等于给了一个
"看起来是自己写的命令、实际接管危险入口"的口子；同名文件一律跳过并报出来，不静默生效。

纯逻辑（解析/校验/展开/合并）与 IO（扫目录）分开，便于脱离环境单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .skills import split_frontmatter

# 命令名：Python 的 \w 在 unicode 下已含中文（`/盯盘` 要能用）；额外允许连字符。
# 不许空白、斜杠、点——避免与路径、文件后缀、内置前缀混淆。
_NAME_RE = re.compile(r"^[\w-]+$", re.UNICODE)
MAX_NAME_LEN = 32
MAX_DESC_LEN = 200
MAX_BODY_CHARS = 8000       # 提示词模板上限，防止把整份文档塞进一次输入
MAX_COMMANDS = 200
MODES = ("prompt", "exec")
ARG_TOKEN = "$ARGUMENTS"

# 内置命令（前端 SLASH_COMMANDS 的权威副本）：自定义命令不得占用这些名字
BUILTIN_NAMES = ("add-dir", "crazy", "help", "技能化", "诊断")


class CommandError(Exception):
    """单个命令文件的解析/校验错误。调用方应跳过该文件并记下原因，不影响其余命令。"""


@dataclass
class Command:
    name: str                       # 命令名（不含前导 /）
    description: str = ""
    mode: str = "prompt"
    body: str = ""                  # prompt 模式的提示词模板 / exec 模式的后续指示（可空）
    command: str = ""               # exec 模式要跑的命令行模板
    skill: str = ""                 # 可选：绑定的技能名
    argument_hint: str = ""         # 补全菜单里显示的参数提示
    source: str = "project"         # builtin / global / config / project
    path: Path | None = None

    @property
    def slash(self) -> str:
        return "/" + self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name, "slash": self.slash, "description": self.description,
            "mode": self.mode, "body": self.body, "command": self.command,
            "skill": self.skill, "argument_hint": self.argument_hint,
            "source": self.source, "path": str(self.path) if self.path else "",
        }


def validate_name(name: str) -> str:
    """校验命令名，返回归一化后的名字（去空白）。不合法抛 CommandError。"""
    n = (name or "").strip()
    if not n:
        raise CommandError("命令名不能为空")
    if len(n) > MAX_NAME_LEN:
        raise CommandError(f"命令名超过 {MAX_NAME_LEN} 字符")
    if not _NAME_RE.match(n):
        raise CommandError(f"命令名 {n!r} 含非法字符（只允许中英文、数字、- 和 _）")
    return n


def parse_command_md(text: str, *, name: str, source: str = "project",
                     path: Path | None = None) -> Command:
    """解析一份命令文件为 Command（纯函数）。

    frontmatter 可以整个省略——那时整份文件就是提示词模板，description 留空由 UI 兜底显示。
    """
    name = validate_name(name)
    raw = (text or "").lstrip("﻿")
    try:
        fm_text, body = split_frontmatter(raw)
    except Exception:
        fm_text, body = "", raw          # 没有 frontmatter：整份文件都是模板，合法

    meta: dict = {}
    if fm_text.strip():
        try:
            loaded = yaml.safe_load(fm_text)
        except yaml.YAMLError as e:
            raise CommandError(f"frontmatter 不是合法 YAML：{e}") from e
        if loaded is not None and not isinstance(loaded, dict):
            raise CommandError("frontmatter 必须是键值对")
        meta = loaded or {}

    def s(key: str) -> str:
        v = meta.get(key)
        if v is None:
            return ""
        if not isinstance(v, str):
            raise CommandError(f"{key} 必须是字符串")
        return v.strip()

    mode = (s("mode") or "prompt").lower()
    if mode not in MODES:
        raise CommandError(f"mode 只能是 {' / '.join(MODES)}，收到 {mode!r}")

    desc = s("description")
    if len(desc) > MAX_DESC_LEN:
        desc = desc[:MAX_DESC_LEN - 1].rstrip() + "…"

    command = s("command")
    body = body.strip()
    if len(body) > MAX_BODY_CHARS:
        raise CommandError(f"正文超过 {MAX_BODY_CHARS} 字符")

    if mode == "exec" and not command:
        raise CommandError("exec 模式必须在 frontmatter 里写 `command:`（要执行的命令行）")
    if mode == "prompt" and not body:
        raise CommandError("prompt 模式的正文不能为空（正文就是提示词模板）")

    return Command(
        name=name, description=desc, mode=mode, body=body, command=command,
        skill=s("skill"), argument_hint=s("argument-hint") or s("argument_hint"),
        source=source, path=path,
    )


def expand_arguments(template: str, arg: str) -> str:
    """把模板里的 $ARGUMENTS 换成用户给的参数。

    模板没写 $ARGUMENTS 但用户给了参数时，**参数追加到末尾**而不是被丢掉——
    丢掉参数是最难察觉的失败（用户以为传了、模型压根没看见）。
    """
    tpl = template or ""
    a = (arg or "").strip()
    if ARG_TOKEN in tpl:
        return tpl.replace(ARG_TOKEN, a)
    return f"{tpl}\n\n{a}".rstrip() if a else tpl


def render_prompt(cmd: Command, arg: str) -> str:
    """prompt 模式：展开成最终要发出去的用户输入。绑定了技能就在开头点名，省得模型漏掉。"""
    text = expand_arguments(cmd.body, arg)
    if cmd.skill:
        text = f"（使用 `{cmd.skill}` 技能）\n{text}"
    return text


def render_exec(cmd: Command, arg: str) -> str:
    """exec 模式：展开成最终要执行的命令行。"""
    return expand_arguments(cmd.command, arg).strip()


def build_command_md(spec: dict) -> str:
    """把管理面填的表单拼成命令文件内容（纯函数）。

    只写填了的字段——空字段留在 frontmatter 里是噪音，用户手改文件时也容易误以为有意义。
    """
    spec = spec or {}
    mode = (spec.get("mode") or "prompt").strip().lower()
    lines = ["---"]

    def put(key: str, value: str) -> None:
        v = (value or "").strip()
        if v:
            # 值里有可能出现 : # 等 YAML 元字符，一律用双引号包并转义，避免生成出坏 YAML
            lines.append(f'{key}: "{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"')

    put("description", spec.get("description", ""))
    if mode == "exec":
        lines.append("mode: exec")
        put("command", spec.get("command", ""))
    put("skill", spec.get("skill", ""))
    put("argument-hint", spec.get("argument_hint", "") or spec.get("argument-hint", ""))
    lines.append("---")
    body = (spec.get("body") or "").strip()
    return "\n".join(lines) + "\n" + (body + "\n" if body else "")


def command_dirs(workspace: Path, app_dir: Path, extra: list[str] | None = None) -> list[tuple[Path, str]]:
    """命令根目录查找顺序：用户全局 → 额外配置 → 项目（**靠后优先**，同名覆盖前面的）。

    与技能不同，这里没有"内置目录"——内置命令写在代码里（BUILTIN_NAMES），不从磁盘加载。
    """
    dirs: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(p: Path, source: str) -> None:
        rp = Path(p)
        if rp not in seen:
            seen.add(rp)
            dirs.append((rp, source))

    add(app_dir / "commands", "global")
    for e in extra or []:
        if str(e).strip():
            add(Path(e).expanduser(), "config")
    add(workspace / ".hermes" / "commands", "project")
    return dirs


def discover_commands(dirs: list[tuple[Path, str]], *, limit: int = MAX_COMMANDS,
                      builtin_names: tuple[str, ...] = BUILTIN_NAMES,
                      ) -> tuple[list[Command], list[str]]:
    """扫目录收集命令。返回 (命令列表, 错误说明列表)。

    - 靠后的目录同名覆盖靠前的（项目级 > 全局）；
    - 撞内置命令名一律跳过并记错——不静默覆盖危险入口；
    - 坏文件只记错跳过，不拖垮其余（对标坏技能包隔离）。
    """
    found: dict[str, Command] = {}
    errors: list[str] = []
    for root, source in dirs:
        try:
            if not root.is_dir():
                continue
            entries = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".md")
        except OSError as e:
            errors.append(f"{root}：读取失败（{e}）")
            continue
        for p in entries:
            if len(found) >= limit and p.stem not in found:
                errors.append(f"命令数量超过上限 {limit}，其余已忽略")
                return list(found.values()), errors
            try:
                name = validate_name(p.stem)
                if name in builtin_names:
                    raise CommandError(f"与内置命令 /{name} 同名，已跳过（内置命令不可覆盖）")
                text = p.read_text(encoding="utf-8", errors="replace")
                found[name] = parse_command_md(text, name=name, source=source, path=p)
            except (CommandError, OSError) as e:
                errors.append(f"{p}：{e}")
    return list(found.values()), errors
