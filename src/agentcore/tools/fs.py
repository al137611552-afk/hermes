"""文件系统工具：读 / 写 / 编辑 / 多处编辑 / 列目录。路径均限制在工作区内。

FR-10.2 读写精度（对标 Claude Code 的 Read/Edit/MultiEdit 惯例）：
- read_file 输出带行号（`行号→制表符→内容`），offset/limit 局部读、按行流式，
  大文件不再一刀切静默截断——没读完会明确提示"继续读用 offset=N"。
- edit_file 支持 replace_all；失败信息可操作（行号前缀带入/空白缩进不一致/确实不存在）。
- multi_edit 同文件多处替换按序在内存应用、**原子落盘**（任意一处失败整体不写）。
匹配诊断与多处应用是纯函数（diagnose_not_found / apply_edits），便于单测。
"""
from __future__ import annotations

import os
import re

from .base import Tool, ToolError, make_diff_block, output_with_diff

MAX_READ_CHARS = 200_000  # 单次输出字符上限（防灌爆上下文；可用 offset 续读）
MAX_READ_LINES = 2000     # 单次最多行数（默认与上限）
MAX_LINE_CHARS = 2000     # 超长单行截断
MAX_DIR_ENTRIES = 1000    # list_dir 单次最多列出条目（防大目录灌爆上下文）


def _read_text_or_die(p, path_label: str) -> str:
    """严格 UTF-8 读文本；遇二进制/非 UTF-8 抛可操作 ToolError（而非裸 UnicodeDecodeError 灌给模型）。
    编辑类工具用：按文本 count/replace 二进制内容会产出乱码替换字符、把文件写坏，故直接拒绝并指路。"""
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{path_label} 疑似二进制或非 UTF-8 文本，无法按文本编辑；"
                        "如确要改，请用 write_file 整体覆盖，或用 shell 处理。")


def _safe_read_for_diff(p) -> str:
    """读旧内容仅用于生成 diff 展示；不可解码（二进制）时返回 ''，绝不让 diff 这种“锦上添花”功能
    把一次合法的覆盖写整个搞崩（原 write_file 覆盖二进制文件即因此裸崩 UnicodeDecodeError）。"""
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except (UnicodeDecodeError, OSError):
        return ""


def _atomic_write(p, content: str) -> None:
    """原子写：先写同目录临时文件再 os.replace 换名——写到一半崩溃/断电不会留下半截损坏文件
    （原实现直接 write_text 覆盖，大文件中途失败即毁原文件）。临时文件与目标同目录，保证同盘可原子 rename。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".hermes.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

# read_file 行号前缀的说明（edit 工具描述里也要提醒别带入）
_LINE_PREFIX_RE = re.compile(r"(?m)^\s*\d+\t")


def _normalized(s: str) -> str:
    """空白宽松归一：各行 strip、丢空行（诊断"内容对但缩进/空白不一致"用）。"""
    return "\n".join(line.strip() for line in s.splitlines() if line.strip())


def diagnose_not_found(text: str, old: str) -> str:
    """old_string 未命中时的可操作提示（纯函数，edit_file / multi_edit 共用）。"""
    stripped = _LINE_PREFIX_RE.sub("", old)
    if stripped != old and stripped and stripped in text:
        return ("未找到 old_string——它带着 read_file 显示用的行号前缀；"
                "去掉每行开头的「行号+制表符」再试。")
    norm = _normalized(old)
    if norm and norm in _normalized(text):
        return ("未找到 old_string——内容相近但空白/缩进/空行不一致；"
                "请按文件原样（含缩进与空白）提供。")
    return ("未找到 old_string，未做修改。请先用 read_file 核对原文"
            "（精确匹配，含空白与缩进；不要包含行号前缀）。")


def _dup_msg(count: int) -> str:
    return (f"old_string 出现 {count} 次（需唯一）。"
            "补足上下文使其唯一，或传 replace_all:true 全部替换。")


def _with_verify(msg: str, verifier, relpath: str) -> str:
    """落盘成功后附加零成本语法校验结果（FR-11.2a）：通过则原样，失败则把警告并入返回。"""
    if verifier is None:
        return msg
    problem = verifier(relpath)
    return f"{msg}\n{problem}" if problem else msg


def apply_edits(text: str, edits: list) -> tuple[str, int]:
    """按序在内存应用多处替换；任意一处失败抛 ToolError（带第几处与原因），不产生半成品。

    返回 (新文本, 实际替换总次数)。后面的编辑作用在前面编辑之后的内容上。
    """
    if not isinstance(edits, list) or not edits:
        raise ToolError("edits 不能为空，应为 [{old_string, new_string, replace_all?}, ...] 数组")
    total = 0
    for i, e in enumerate(edits, 1):
        where = f"第 {i}/{len(edits)} 处编辑"
        if not isinstance(e, dict):
            raise ToolError(f"{where}：应为对象（含 old_string / new_string）")
        old = e.get("old_string") or ""
        new = e.get("new_string", "")
        if not old:
            raise ToolError(f"{where}：old_string 不能为空")
        if old == new:
            raise ToolError(f"{where}：old_string 与 new_string 相同，无意义")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"{where}：{diagnose_not_found(text, old)}（整个文件未改动）")
        if e.get("replace_all"):
            text = text.replace(old, new)
            total += count
        else:
            if count > 1:
                raise ToolError(f"{where}：{_dup_msg(count)}（整个文件未改动）")
            text = text.replace(old, new, 1)
            total += 1
    return text, total


class ReadFileTool(Tool):
    name = "read_file"
    parallel_safe = True  # 只读，同轮多个调用并发执行（FR-10.5 扩展到只读工具）
    description = (
        "读取工作区内文本文件，输出带行号（每行格式：行号+制表符+内容）。"
        "大文件用 offset/limit 分段读，没读完会提示下次的 offset。"
        "注意：行号前缀只是显示用，编辑工具的 old_string 里不要包含它。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "offset": {"type": "integer", "description": "起始行号（1 起，默认 1）"},
            "limit": {"type": "integer", "description": f"最多读多少行（默认/上限 {MAX_READ_LINES}）"},
        },
        "required": ["path"],
    }

    def run(self, params: dict) -> str:
        p = self.resolve(params["path"])
        if not p.is_file():
            raise ToolError(f"文件不存在：{params['path']}")
        try:
            offset = max(1, int(params.get("offset") or 1))
        except (TypeError, ValueError):
            offset = 1
        try:
            limit = int(params.get("limit") or MAX_READ_LINES)
        except (TypeError, ValueError):
            limit = MAX_READ_LINES
        limit = max(1, min(limit, MAX_READ_LINES))

        out: list[str] = []
        chars = 0
        finished = True   # 是否读到了文件末尾
        last_line = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                last_line = lineno
                if lineno < offset:
                    continue
                if lineno >= offset + limit or chars >= MAX_READ_CHARS:
                    finished = False
                    break
                line = line.rstrip("\r\n")
                if len(line) > MAX_LINE_CHARS:
                    line = line[:MAX_LINE_CHARS] + f" …(行过长，已截断至 {MAX_LINE_CHARS} 字符)"
                out.append(f"{lineno}\t{line}")
                chars += len(line) + 8

        if not out:
            if last_line == 0 and offset == 1:
                return "(空文件)"
            raise ToolError(f"offset={offset} 超出文件末尾（文件共 {last_line} 行）。")
        body = "\n".join(out)
        if not finished:
            body += f"\n... (未到文件末尾，继续读请用 offset={offset + len(out)})"
        return body


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "创建或覆盖工作区内的文本文件（默认覆盖已有内容）。"
        "**一次写不完的大文件（如高保真 HTML 原型）用 append 分块写**：第一块正常写，"
        "后续每块传 append:true 追加——单次输出有 max_tokens 上限，硬要一次写完会被截断、这一轮白跑。"
    )
    dangerous = True
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "content": {"type": "string", "description": "要写入的内容（append:true 时是要追加的那一块）"},
            "append": {"type": "boolean",
                       "description": "追加到文件末尾而不是覆盖（默认 false）。分块写大文件时用："
                                      "先写第一块，之后每块 append:true。文件不存在时等同于新建。"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace, tracker=None, verifier=None) -> None:
        super().__init__(workspace)
        self._tracker = tracker      # 改动台账回调（FR-9.4a）：写入前快照基线
        self._verifier = verifier    # 写入后零成本语法校验（FR-11.2a）

    def run(self, params: dict):
        p = self.resolve(params["path"])
        rel = str(p.relative_to(self.workspace))
        before = _safe_read_for_diff(p)
        if self._tracker:
            self._tracker(rel)
        content = params.get("content", "")
        append = bool(params.get("append"))
        if append:
            # 追加仍走原子写（读旧 + 拼 + 换名），保住"写到一半崩溃不留半截文件"的性质。
            # 文件不存在＝新建，别让分块写的第一块因为"文件还没有"而失败。
            final = (_read_text_or_die(p, params["path"]) if p.is_file() else "") + content
            msg = f"已追加到 {params['path']}（+{len(content)} 字符，现 {len(final)} 字符）"
        else:
            final = content
            msg = f"已写入 {params['path']}（{len(content)} 字符）"
        _atomic_write(p, final)
        if append:
            # 分块写的中间态**必然语法不完整**（半个 <html>、半个函数），这时候跑语法校验只会
            # 每块喷一次假报错、把模型带偏。故追加时跳过校验，但明确告诉它最后要自己收口。
            msg += "\n（分块写入中，已跳过语法校验；写完最后一块后请自行读一遍或跑一次校验确认完整）"
        else:
            msg = _with_verify(msg, self._verifier, rel)
        return output_with_diff(msg, make_diff_block(rel, before, final))


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "对工作区内文件做一处精确字符串替换。old_string 默认须在文件中唯一出现"
        "（要全部替换传 replace_all:true）；按文件原样匹配（含空白缩进），"
        "不要包含 read_file 的行号前缀。同一文件多处修改请用 multi_edit。"
    )
    dangerous = True
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文（默认需唯一）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "替换所有出现处（默认 false）"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, workspace, tracker=None, verifier=None) -> None:
        super().__init__(workspace)
        self._tracker = tracker      # 改动台账回调（FR-9.4a）：编辑前快照基线
        self._verifier = verifier    # 写入后零成本语法校验（FR-11.2a）

    def run(self, params: dict) -> str:
        p = self.resolve(params["path"])
        if not p.is_file():
            raise ToolError(f"文件不存在：{params['path']}")
        text = _read_text_or_die(p, params["path"])
        old = params["old_string"]
        if not old:
            raise ToolError("old_string 不能为空")
        count = text.count(old)
        if count == 0:
            raise ToolError(diagnose_not_found(text, old))
        if count > 1 and not params.get("replace_all"):
            raise ToolError(_dup_msg(count))
        rel = str(p.relative_to(self.workspace))
        if self._tracker:
            self._tracker(rel)
        if params.get("replace_all"):
            after = text.replace(old, params["new_string"])
            msg = f"已编辑 {params['path']}（替换 {count} 处）"
        else:
            after = text.replace(old, params["new_string"], 1)
            msg = f"已编辑 {params['path']}"
        _atomic_write(p, after)
        return output_with_diff(_with_verify(msg, self._verifier, rel),
                                make_diff_block(rel, text, after))


class MultiEditTool(Tool):
    name = "multi_edit"
    description = (
        "对同一文件做多处精确替换：edits 按顺序依次应用（后面的编辑作用在前面编辑后的内容上），"
        "**全部成功才落盘**——任意一处失败则整个文件不改。每处默认 old_string 需唯一，"
        "可单独传 replace_all。比连续多次 edit_file 高效、且不会改一半。"
    )
    dangerous = True
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "edits": {
                "type": "array",
                "description": "按序应用的编辑列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "要被替换的原文（默认需唯一）"},
                        "new_string": {"type": "string", "description": "替换后的新文本"},
                        "replace_all": {"type": "boolean", "description": "替换该处所有出现（默认 false）"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def __init__(self, workspace, tracker=None, verifier=None) -> None:
        super().__init__(workspace)
        self._tracker = tracker      # 改动台账回调（FR-9.4a）：落盘前快照基线
        self._verifier = verifier    # 写入后零成本语法校验（FR-11.2a）

    def run(self, params: dict) -> str:
        p = self.resolve(params["path"])
        if not p.is_file():
            raise ToolError(f"文件不存在：{params['path']}")
        text = _read_text_or_die(p, params["path"])
        new_text, total = apply_edits(text, params.get("edits"))  # 失败在此抛出，不落盘
        rel = str(p.relative_to(self.workspace))
        if self._tracker:
            self._tracker(rel)
        _atomic_write(p, new_text)
        n = len(params["edits"])
        msg = f"已对 {params['path']} 应用 {n} 处编辑（共替换 {total} 处）"
        return output_with_diff(_with_verify(msg, self._verifier, rel),
                                make_diff_block(rel, text, new_text))


class ListDirTool(Tool):
    name = "list_dir"
    parallel_safe = True  # 只读，同轮多个调用并发执行
    description = "列出工作区内某个目录的文件与子目录。"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对工作区的目录，默认根目录"}},
    }

    def run(self, params: dict) -> str:
        p = self.resolve(params.get("path") or ".")
        if not p.is_dir():
            raise ToolError(f"目录不存在：{params.get('path', '.')}")
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        if not entries:
            return "(空目录)"
        total = len(entries)
        shown = entries[:MAX_DIR_ENTRIES]      # 大目录截断，防一次列几千项灌爆上下文
        lines = [f"{'📄' if e.is_file() else '📁'} {e.name}" for e in shown]
        if total > MAX_DIR_ENTRIES:
            lines.append(f"…（共 {total} 项，仅列出前 {MAX_DIR_ENTRIES} 项；"
                         "用更具体的子目录或 grep/glob 缩小范围）")
        return "\n".join(lines)
