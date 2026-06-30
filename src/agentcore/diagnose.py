"""报错定位（FR-13.B，P5 调试能力工程化第一波）。

工具/命令输出里出现 Python traceback 时，解析它、挑出**工作区内最深一帧**（真正崩在用户代码
的那行），从磁盘摘出该行 ± 上下文，附加到工具结果里回灌——模型一眼看到「错在哪个文件第几行、
那段代码长什么样」，省掉再开文件找位置的一轮。

纯逻辑（parse_traceback / pick_workspace_frame / format_location）与受控 IO（enrich_traceback
读盘取上下文）分离，便于单测。只读、零行为改动：没 traceback 或不指向工作区文件就返回 None。
"""
from __future__ import annotations

import re
from pathlib import Path

# 形如：  File "/path/to/x.py", line 12, in func
_FRAME_RE = re.compile(r'^\s*File "(?P<file>.+?)", line (?P<line>\d+), in (?P<func>.+?)\s*$')
# 末行异常：ValueError: boom / mypkg.MyError: ... （行首非空白、形如 Name: msg）
_EXC_RE = re.compile(r'^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|StopIteration|Failed)?)'
                     r'(?::\s?(?P<msg>.*))?$')


class Frame:
    __slots__ = ("file", "line", "func")

    def __init__(self, file: str, line: int, func: str) -> None:
        self.file, self.line, self.func = file, line, func

    def __eq__(self, other) -> bool:
        return (isinstance(other, Frame) and self.file == other.file
                and self.line == other.line and self.func == other.func)

    def __repr__(self) -> str:
        return f"Frame({self.file!r}, {self.line}, {self.func!r})"


def parse_traceback(text: str) -> "list[Frame]":
    """从文本里解析出所有 traceback 帧（纯逻辑，外层→内层顺序）。无则空列表。"""
    frames: list[Frame] = []
    for line in (text or "").splitlines():
        m = _FRAME_RE.match(line)
        if m:
            frames.append(Frame(m.group("file"), int(m.group("line")), m.group("func").strip()))
    return frames


def parse_exception_line(text: str) -> "str | None":
    """取 traceback 末尾的异常行（如 `ValueError: boom`）；找不到返回 None（纯逻辑）。"""
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln[0].isspace() or ln.startswith("File "):
            continue
        if _EXC_RE.match(ln) and (":" in ln or ln.endswith("Error") or ln.endswith("Exception")):
            return ln.strip()
    return None


def _resolve_in_workspace(file: str, workspace: Path) -> "Path | None":
    """traceback 里的文件路径若指向工作区内的真实文件，返回其绝对 Path；否则 None（纯逻辑判定）。"""
    ws = workspace.resolve()
    raw = file.strip().strip('"')
    cands = [Path(raw)]
    if not Path(raw).is_absolute():
        cands.append(ws / raw)
    for p in cands:
        try:
            rp = p.resolve()
        except (OSError, ValueError):
            continue
        if rp == ws or ws in rp.parents:
            if rp.is_file():
                return rp
    return None


def pick_workspace_frame(frames: "list[Frame]", workspace: Path) -> "tuple[Frame, Path] | None":
    """挑出最深一帧落在工作区内的（=用户代码真正崩的位置）；无则 None。"""
    for fr in reversed(frames):  # 最内层优先
        rp = _resolve_in_workspace(fr.file, workspace)
        if rp is not None:
            return fr, rp
    return None


def format_location(relpath: str, line: int, func: str, src_lines: "list[str]",
                    start: int, hot: int, exc: "str | None") -> str:
    """组织「报错定位」块（纯逻辑）：file:line + 带箭头的源码上下文 + 异常行。"""
    out = [f"📍 报错定位（FR-13.B）：{relpath}:{line}（in {func}）"]
    for i, code in enumerate(src_lines):
        ln = start + i
        mark = "→" if ln == hot else " "
        out.append(f"  {mark} {ln:>4} | {code}")
    if exc:
        out.append(f"  ⮑ {exc}")
    return "\n".join(out)


def enrich_traceback(text: str, workspace: Path, context: int = 2) -> "str | None":
    """文本含指向工作区文件的 traceback → 返回定位块（读盘取上下文）；否则 None（受控 IO）。"""
    try:
        frames = parse_traceback(text)
        if not frames:
            return None
        picked = pick_workspace_frame(frames, workspace)
        if picked is None:
            return None
        fr, abspath = picked
        try:
            all_lines = abspath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        if not (1 <= fr.line <= len(all_lines)):
            return None
        start = max(1, fr.line - context)
        end = min(len(all_lines), fr.line + context)
        src = all_lines[start - 1:end]
        try:
            rel = abspath.relative_to(workspace.resolve()).as_posix()
        except ValueError:
            rel = abspath.name
        return format_location(rel, fr.line, fr.func, src, start, fr.line,
                               parse_exception_line(text))
    except Exception:  # noqa: BLE001 —— 定位是锦上添花，绝不因它把工具结果搞挂
        return None


def with_location(text: str, workspace: Path) -> str:
    """便捷封装：把定位块附加到原文本之后（无定位则原样返回）。"""
    loc = enrich_traceback(text, workspace)
    return f"{text}\n\n{loc}" if loc else text
