"""大库代码检索（按需、零依赖、离线）：对自然语言查询，返回全库**最相关**的代码块。

定位：补 grep/symbol 够不到的「按概念/意图找代码」——大型陌生库里，模型读不动全部文件时，
用一次查询把最相关的几段拉进上下文。**诚实说明**：这是**关系排序检索（BM25 + 代码感知分块 +
标识符切词）**，不是神经向量语义检索——但代码满是精确标识符，词项检索对代码很有效，且符合
hermes「无新依赖、离线、按需扫不持久化」的一贯取舍（同 codeindex）。需要真·向量语义时另接嵌入模型。

纯逻辑（tokenize / chunk_source / Bm25）与受控 IO（search_code 遍历读盘）分离，便于单测。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .codeindex import (
    MAX_FILE_BYTES, _SKIP_DIRS, extract_file, is_indexable,
)

MAX_FILES = 800           # 检索扫描的文件数上限（大库护栏）
WINDOW_LINES = 40         # 无符号文件的滑窗块大小
MAX_CHUNK_LINES = 220     # 单个符号块过大时截断参与评分的行数

# 代码里几乎无判别力的高频词（停用），降低噪音
_STOP = frozenset({
    "the", "and", "for", "def", "class", "return", "self", "import", "from", "this",
    "var", "let", "const", "function", "if", "else", "true", "false", "none", "null",
    "in", "is", "to", "of", "with", "type", "value", "data", "list", "dict", "str", "int",
})


_CJK = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> list[str]:
    """标识符感知 + 中文切词：拆 snake_case/camelCase、转小写、去停用词；中文出二元组（纯逻辑）。

    `parseHTMLDoc`→parse/html/doc；`get_user_id`→get/user/id；中文「分级折扣」→分级/级折/折扣
    （二元组让中文查询与中文注释/docstring 也可检索，否则非 ASCII 全被丢、中文用户查不到）。
    """
    out: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", text or ""):
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", word) or [word]
        for p in parts:
            p = p.lower()
            if len(p) >= 2 and p not in _STOP:
                out.append(p)
    for run in _CJK.findall(text or ""):       # 中文：相邻二字成元组（单字成 1 元组）
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


@dataclass
class Chunk:
    file: str
    start: int                 # 起始行号（1-based）
    name: str                  # 符号名（无则空）
    text: str
    tokens: list[str] = field(default_factory=list)


def chunk_source(relpath: str, source: str) -> list[Chunk]:
    """把一个文件切成代码感知的块（纯逻辑）：按顶层符号体切；无符号则按行滑窗。"""
    lines = source.splitlines()
    if not lines:
        return []
    syms = [s for s in extract_file(Path(relpath), source) if not s.parent]  # 顶层符号作切点
    chunks: list[Chunk] = []
    if syms:
        starts = sorted({s.line for s in syms})
        name_at = {s.line: s.name for s in syms}
        # 文件头（第一个符号前的 import/模块说明）也作一块
        if starts[0] > 1:
            head = "\n".join(lines[: starts[0] - 1])
            if head.strip():
                chunks.append(Chunk(relpath, 1, "", head))
        bounds = starts + [len(lines) + 1]
        for i, st in enumerate(starts):
            body = lines[st - 1: min(bounds[i + 1] - 1, st - 1 + MAX_CHUNK_LINES)]
            text = "\n".join(body)
            if text.strip():
                chunks.append(Chunk(relpath, st, name_at.get(st, ""), text))
    else:  # 无可抽取符号：滑窗
        for st in range(0, len(lines), WINDOW_LINES):
            text = "\n".join(lines[st: st + WINDOW_LINES])
            if text.strip():
                chunks.append(Chunk(relpath, st + 1, "", text))
    for c in chunks:
        # 块文本 + 文件名 + 符号名都进词袋（路径/符号名是强信号）
        c.tokens = tokenize(c.text) + tokenize(relpath) + tokenize(c.name) * 3
    return chunks


class Bm25:
    """极简 BM25（纯逻辑、无依赖）。文档=各代码块的词袋。"""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs) or 1
        self.dl = [len(d) for d in docs]
        self.avgdl = (sum(self.dl) / self.N) or 1.0
        self.tf: list[dict] = []
        df: dict[str, int] = {}
        for d in docs:
            counts: dict[str, int] = {}
            for t in d:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, query: list[str], i: int) -> float:
        counts, dl = self.tf[i], self.dl[i]
        s = 0.0
        for t in query:
            f = counts.get(t)
            if not f:
                continue
            idf = self.idf.get(t, 0.0)
            s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s


def rank_chunks(query: str, chunks: list[Chunk], limit: int = 8) -> list[tuple[Chunk, float]]:
    """对查询给所有块打分排序，返回前 limit 个 (块, 分)（纯逻辑，分>0 才算命中）。"""
    q = tokenize(query)
    if not q or not chunks:
        return []
    bm = Bm25([c.tokens for c in chunks])
    scored = [(c, bm.score(q, i)) for i, c in enumerate(chunks)]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def _snippet(text: str, max_lines: int = 8) -> str:
    lines = text.splitlines()
    out = lines[:max_lines]
    if len(lines) > max_lines:
        out.append(f"…（本块共 {len(lines)} 行）")
    return "\n".join(out)


def search_code(workspace: Path, query: str, limit: int = 8) -> str:
    """遍历工作区代码、按相关性返回最相关的若干块（受控 IO）。"""
    workspace = Path(workspace).resolve()
    chunks: list[Chunk] = []
    n_files = 0
    for p in sorted(workspace.rglob("*")):
        if n_files >= MAX_FILES:
            break
        if not p.is_file() or not is_indexable(p):
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(workspace).parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            source = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n_files += 1
        chunks.extend(chunk_source(p.relative_to(workspace).as_posix(), source))
    ranked = rank_chunks(query, chunks, limit)
    if not ranked:
        return (f"未找到与「{query}」相关的代码（扫描 {n_files} 个文件 / {len(chunks)} 块）。"
                "换几个关键词，或用 grep_search 精确匹配。")
    out = [f"🔎 与「{query}」最相关的代码（关系排序检索，扫 {n_files} 文件/{len(chunks)} 块，按需无缓存）："]
    for c, score in ranked:
        loc = f"{c.file}:{c.start}" + (f"  ({c.name})" if c.name else "")
        out.append(f"\n— {loc}  [score {score:.1f}] —\n{_snippet(c.text)}")
    out.append("\n（这是相关性检索、非精确匹配；要看全文用 read_file，要精确串用 grep_search。）")
    return "\n".join(out)
