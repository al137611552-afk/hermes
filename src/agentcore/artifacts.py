"""工具产物：大输出落盘 + 摘要 + 句柄（ADR 0021）。

问题不在"工具输出有上限"（上限是对的，防灌爆上下文），而在**截掉的部分永久消失**——
模型想再看只能重跑同一条命令，而重跑既贵又未必幂等（网页变了、构建产物变了、
后台进程的早期日志已被环形缓冲冲掉，根本重跑不出来）。

本模块让大输出落盘成「产物」，工具只回**摘要 + 句柄**；处理产物复用现成的
`grep_search` / `read_file(offset=)` / shell，**不新增读产物的专用工具**（见 ADR 0021 §4）。

分层：
- 纯逻辑（可脱环境单测）：`should_artifact` 判据、`summarize_for_context` 摘要、
  `format_with_handle` 拼装、`prune_plan` 清理计划。
- IO：`ArtifactStore` 落盘 + JSON 台账 + prune；`TeeWriter` 供后台进程读线程边收边写（§7）。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_THRESHOLD = 20_000     # 防抖下限：原始量不到这个数就算截断了也不落产物（避免碎片产物）
DEFAULT_MAX_TOTAL_MB = 200     # 单工作区产物总量上限
DEFAULT_KEEP_DAYS = 7          # 产物保留天数
HEAD_LINES = 60                # 摘要保留的头部行数
TAIL_LINES = 40                # 摘要保留的尾部行数（结论通常在尾部：失败汇总、退出码）
MAX_SUMMARY_LINE = 2000        # 摘要里单行截断（同 fs/grep 的约定，防单行 40 万字符的 JSON）
MAX_SUMMARY_CHARS = 8000       # 摘要总字符上限（兜底：头尾行本身也可能很大）

_DIR_NAME = ".hermes"
_ARTIFACT_SUBDIR = "artifacts"
_LEDGER_NAME = "artifacts.json"   # 台账放产物目录**外**（同技能安装台账：不往内容目录塞 sidecar）


@dataclass
class Artifact:
    """一个产物 = 一个文件 + 一条台账元数据。"""
    id: str
    rel: str            # 相对工作区的路径（给模型看的地址）
    tool: str
    origin: str         # 产生它的命令 / URL，便于人和模型辨认
    chars: int
    lines: int
    created_at: float
    session_id: int | None = None

    def to_meta(self) -> dict:
        return asdict(self)


# ---- 纯逻辑 -----------------------------------------------------------------


def should_artifact(raw_chars: int, returned_chars: int,
                    threshold: int = DEFAULT_THRESHOLD) -> bool:
    """判据 = 「这次输出发生了截断」且「原始量 ≥ threshold」。

    判"有没有截断"而不是"输出够不够大"（ADR 0021 §2 评审改动）：
    - `web_fetch` 的 cap 默认正好也是 20,000，量返回长度会永远卡在阈值边界；真正该存的是
      **抓到了却被 cap 掉的原文**，所以必须量原始数据。
    - 没截断的大输出存了也没用——模型已经全看到了，落盘是纯开销。
    """
    if raw_chars <= returned_chars:      # 没截断
        return False
    return raw_chars >= max(0, threshold)


def _clip_line(line: str, limit: int = MAX_SUMMARY_LINE) -> str:
    return line if len(line) <= limit else line[:limit] + f" …(行过长，已截断至 {limit} 字符)"


def _clip_line_tail(line: str, limit: int = MAX_SUMMARY_LINE) -> str:
    """尾部行保**右**边：一行超长时结论在行尾（如单行 JSON、进度条刷出来的一整行），
    按头部那样从左截会把要看的东西正好切掉。"""
    return line if len(line) <= limit else f"(行过长，只留末尾 {limit} 字符)… " + line[-limit:]


def summarize_for_context(text: str, head_lines: int = HEAD_LINES,
                          tail_lines: int = TAIL_LINES,
                          max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """取头 N 行 + 尾 M 行、中间标省略计数，作为回给模型的摘要。

    取头尾而非只取头：命令行输出的结论通常在**尾部**（失败汇总、退出码），只取头会把最有用的丢掉。
    """
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines:
        body = "\n".join(_clip_line(ln) for ln in lines)
    else:
        omitted = len(lines) - head_lines - tail_lines
        body = "\n".join([
            *(_clip_line(ln) for ln in lines[:head_lines]),
            f"…（中间省略 {omitted:,} 行）",
            *(_clip_line_tail(ln) for ln in lines[-tail_lines:]),
        ])
    if len(body) > max_chars:            # 兜底：头尾行本身就很大时再砍一刀
        keep = max_chars // 2
        body = (body[:keep]
                + f"\n…（摘要过长，中间省略 {len(body) - 2 * keep:,} 字符）\n"
                + body[-keep:])
    return body


def head_tail_of_file(path: Path, *, head_lines: int = HEAD_LINES,
                      tail_lines: int = TAIL_LINES, total_lines: int | None = None,
                      chunk: int = 200_000) -> str:
    """从产物文件里取「头 N 行 + 尾 M 行」摘要，不把整个文件读进内存（尾部用 seek）。

    尾部是这里的关键：命令行输出的结论几乎都在最后（失败汇总、退出码），而 20 万字符的硬截断
    恰恰**只留头部、把结论丢掉**——一次 40 万字符的 pytest 输出，模型看到的是前半截噪音、
    看不到最后的失败摘要。有了产物就能把两头都给它。
    """
    try:
        size = path.stat().st_size
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(chunk)
            if size > chunk:
                fh.seek(max(0, size - chunk))
                tail = fh.read()
            else:
                tail = ""
    except OSError:
        return ""
    head_part = head.splitlines()[:head_lines]
    tail_src = (tail or head).splitlines()
    tail_part = tail_src[-tail_lines:] if len(tail_src) > tail_lines else (
        tail_src if tail else [])
    if not tail_part and not tail:
        # 整个文件都读进来了：直接走纯逻辑摘要，省得两头重复
        return summarize_for_context(head, head_lines, tail_lines)
    if total_lines is not None:
        omitted = max(0, total_lines - len(head_part) - len(tail_part))
        mid = f"…（中间省略 {omitted:,} 行）"
    else:
        mid = "…（中间省略）"
    return "\n".join([*(_clip_line(ln) for ln in head_part), mid,
                      *(_clip_line_tail(ln) for ln in tail_part)])


def format_with_handle(summary: str, art: Artifact) -> str:
    """把摘要和句柄拼成工具的最终返回文本。

    提示写明"完整内容在哪、可以怎么处理、不必重跑"——ADR 0021 风险 1 的缓解：
    模型只看摘要就下结论的话，产物就白存了。
    """
    return (
        f"[产物 {art.id} · 原始 {art.chars:,} 字符 / {art.lines:,} 行 · 已落盘 {art.rel}]\n"
        f"{summary}\n"
        f"[提示] 上面是摘要（头 {HEAD_LINES} 行 + 尾 {TAIL_LINES} 行）。完整内容在 {art.rel}，"
        f"需要细节就 grep_search / read_file（带 offset）它，或写脚本处理，**不必重跑**。"
    )


def prune_plan(entries: list[dict], *, max_total_bytes: int, keep_days: float,
               now: float | None = None) -> list[str]:
    """算出该删哪些产物（返回 id 列表，按删除顺序）。双上限：过期优先，再按最旧优先压总量。

    `max_total_bytes <= 0` 或 `keep_days <= 0` 表示对应维度不限。
    """
    now = time.time() if now is None else now
    rows = sorted(entries, key=lambda e: e.get("created_at") or 0)
    doomed: list[str] = []
    if keep_days > 0:
        cutoff = now - keep_days * 86400
        for e in rows:
            if (e.get("created_at") or 0) < cutoff:
                doomed.append(e["id"])
    if max_total_bytes > 0:
        alive = [e for e in rows if e["id"] not in doomed]
        total = sum(int(e.get("bytes") or 0) for e in alive)
        for e in alive:                       # 已按时间升序：最旧优先删
            if total <= max_total_bytes:
                break
            doomed.append(e["id"])
            total -= int(e.get("bytes") or 0)
    return doomed


# ---- IO：存储与台账 ----------------------------------------------------------


class TeeWriter:
    """边产生边落盘的产物写入器（后台进程读线程用，ADR 0021 §7）。

    后台进程的环形缓冲是"一边收一边丢最旧"，等工具返回时再落盘早就晚了，
    所以这里在读线程写缓冲的同时追加落盘。写失败一律吞掉（落盘是增值能力，
    不能让它把正在跑的进程搞挂）。
    """

    def __init__(self, store: "ArtifactStore", art: Artifact, fh, *, min_chars: int = 0) -> None:
        self._store = store
        self._fh = fh
        # 必须可重入：write() 落盘出错时会在持锁状态下调 close() 收摊，普通 Lock 会当场死锁。
        self._lock = threading.RLock()
        self._min_chars = min_chars   # 收尾时不够大就自我销毁（前台 shell 用；后台进程传 0＝始终保留）
        self._kept = True
        self.artifact = art

    def write(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(chunk)
                self._fh.flush()
                self.artifact.chars += len(chunk)
                self.artifact.lines += chunk.count("\n")
            except OSError:
                self.close()

    def close(self) -> bool:
        """收尾。返回产物是否保留（不够 min_chars 则销毁，避免碎片产物）。"""
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is None:
            return self._kept
        try:
            fh.close()
        except OSError:
            pass
        if self.artifact.chars < self._min_chars:
            self._store.drop(self.artifact.id)
            self._kept = False
        else:
            self._store.update(self.artifact)
            self._kept = True
        return self._kept


class ArtifactSink:
    """注入给工具的产物入口：绑定会话、内含判据、失败即降级（同 browser_reader 的注入模式）。

    工具不该自己管存储、会话号和"该不该落盘"，那会让每个接入点重复一遍逻辑。工具只问两句：
    `maybe_put(原文, 实际回了多少)` 拿句柄，或 `open_tee()` 拿个边跑边写的写入器；
    拿到 None 就当没这回事照常返回（产物是增值能力，绝不能让它把工具搞挂）。
    """

    def __init__(self, store: ArtifactStore, *, session_id_fn=None,
                 enabled: bool = True, threshold: int | None = None) -> None:
        self._store = store
        self._session_id_fn = session_id_fn
        self.enabled = enabled
        self.threshold = store.threshold if threshold is None else threshold

    @property
    def store(self) -> ArtifactStore:
        return self._store

    def _session_id(self) -> "int | None":
        if self._session_id_fn is None:
            return None
        try:
            return self._session_id_fn()
        except Exception:  # noqa: BLE001 — 取不到会话号不影响落盘
            return None

    def maybe_put(self, raw: str, returned_chars: int, *, tool: str,
                  origin: str = "") -> "Artifact | None":
        """按判据落产物：没截断 / 不够大 / 关了 / 落盘失败，一律返回 None。"""
        if not self.enabled or not should_artifact(len(raw), returned_chars, self.threshold):
            return None
        try:
            return self._store.put(raw, tool=tool, origin=origin, session_id=self._session_id())
        except OSError:
            return None

    def open_tee(self, *, tool: str, origin: str = "",
                 min_chars: int = 0) -> "TeeWriter | None":
        """开一个边产生边落盘的产物（输出会被丢弃的场景：前台 shell 超上限、后台环形缓冲）。"""
        if not self.enabled:
            return None
        return self._store.open_tee(tool=tool, origin=origin, min_chars=min_chars,
                                    session_id=self._session_id())


class ArtifactStore:
    """产物落盘 + JSON 台账 + 清理。跟着工作区走（换项目即换产物集）。"""

    def __init__(self, workspace: Path, *, threshold: int = DEFAULT_THRESHOLD,
                 max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
                 keep_days: float = DEFAULT_KEEP_DAYS) -> None:
        self.workspace = Path(workspace).resolve()
        self.threshold = threshold
        self.max_total_bytes = int(max_total_mb) * 1024 * 1024
        self.keep_days = keep_days
        self._lock = threading.Lock()   # 台账读改写 + 发号；同轮并行工具可能同时落产物

    # -- 路径 --
    @property
    def root(self) -> Path:
        return self.workspace / _DIR_NAME / _ARTIFACT_SUBDIR

    @property
    def ledger_path(self) -> Path:
        return self.workspace / _DIR_NAME / _LEDGER_NAME

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # 自我忽略：绑定真实项目时 .hermes/ 会冒进 git_status 和「改动」面板。
        # 只写自己这层的 .gitignore，**不动用户仓库根的 .gitignore**（那是用户的文件）。
        gi = self.workspace / _DIR_NAME / ".gitignore"
        if not gi.exists():
            try:
                gi.write_text("*\n", encoding="utf-8")
            except OSError:
                pass

    # -- 台账 --
    def _read_ledger(self) -> dict:
        try:
            d = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_ledger(self, data: dict) -> None:
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self.ledger_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        except OSError:
            pass

    def _next_id(self, data: dict) -> str:
        n = 0
        for k in data:
            try:
                n = max(n, int(str(k).rsplit("_", 1)[-1]))
            except ValueError:
                continue
        return f"art_{n + 1:04d}"

    def _record(self, art: Artifact, path: Path) -> None:
        data = self._read_ledger()
        meta = art.to_meta()
        try:
            meta["bytes"] = path.stat().st_size
        except OSError:
            meta["bytes"] = art.chars
        data[art.id] = meta
        self._write_ledger(data)

    # -- 写入 --
    def put(self, text: str, *, tool: str, origin: str = "",
            session_id: int | None = None) -> Artifact:
        """把一段完整输出落成产物，返回句柄。落盘失败抛 OSError 由调用方降级。"""
        with self._lock:
            self._ensure_root()
            data = self._read_ledger()
            art_id = self._next_id(data)
            path = self.root / f"{art_id}.txt"
            # newline=""：**产物要无损**。文本模式在 Windows 上会把 "\n" 翻成 "\r\n"，
            # 于是落盘字节数 ≠ chars（台账的 bytes 取 st_size，配额也按它算），
            # 且工具原样输出的换行被我们悄悄改写了——产物是现场证据，不该被翻译。
            path.write_text(text, encoding="utf-8", errors="replace", newline="")
            art = Artifact(id=art_id, rel=self.rel_of(path), tool=tool, origin=origin,
                           chars=len(text), lines=(text.count("\n") + 1) if text else 0,
                           created_at=time.time(), session_id=session_id)
            self._record(art, path)
        self.prune()
        return art

    def drop(self, art_id: str) -> None:
        """删掉一条产物（文件 + 台账）。tee 收尾发现不够大时用。"""
        with self._lock:
            data = self._read_ledger()
            meta = data.pop(art_id, None)
            if meta is None:
                return
            self._write_ledger(data)
        try:
            (self.workspace / meta.get("rel", "")).unlink(missing_ok=True)
        except OSError:
            pass

    def open_tee(self, *, tool: str, origin: str = "", min_chars: int = 0,
                 session_id: int | None = None) -> "TeeWriter | None":
        """开一个 append-only 产物给后台进程边跑边写。开不了返回 None（调用方照常跑）。"""
        try:
            with self._lock:
                self._ensure_root()
                data = self._read_ledger()
                art_id = self._next_id(data)
                path = self.root / f"{art_id}.log"
                fh = open(path, "a", encoding="utf-8", errors="replace", newline="")
                art = Artifact(id=art_id, rel=self.rel_of(path), tool=tool, origin=origin,
                               chars=0, lines=0, created_at=time.time(), session_id=session_id)
                self._record(art, path)
            return TeeWriter(self, art, fh, min_chars=min_chars)
        except OSError:
            return None

    def update(self, art: Artifact) -> None:
        """更新台账里的一条（tee 写完后回填最终大小）。"""
        with self._lock:
            self._record(art, self.root / Path(art.rel).name)

    # -- 读取与清理 --
    def rel_of(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace)).replace("\\", "/")
        except ValueError:
            return str(path)

    def list(self, session_id: int | None = None) -> list[dict]:
        """列产物（新的在前）。给了 session_id 就只列本会话的。

        默认 `per_session_workspace=true` 时工作区本就按会话隔离，这里的过滤只在
        用 📂 绑定真实项目、多会话共用同一目录时才起作用（ADR 0021 决议 1）。
        """
        rows = sorted(self._read_ledger().values(),
                      key=lambda e: e.get("created_at") or 0, reverse=True)
        if session_id is None:
            return rows
        return [r for r in rows if r.get("session_id") == session_id]

    def others_count(self, session_id: int | None) -> int:
        """同工作区里**不属于**本会话的产物条数（列表末尾提示用）。"""
        if session_id is None:
            return 0
        return sum(1 for r in self._read_ledger().values() if r.get("session_id") != session_id)

    def prune(self) -> list[str]:
        """按双上限清理，同时 prune 掉用户手删文件后残留的台账条目。返回删掉的 id。"""
        with self._lock:
            data = self._read_ledger()
            if not data:
                return []
            changed = False
            for art_id, meta in list(data.items()):     # 文件被手删 -> 台账同步清掉
                if not (self.workspace / meta.get("rel", "")).exists():
                    data.pop(art_id, None)
                    changed = True
            doomed = prune_plan(list(data.values()),
                                max_total_bytes=self.max_total_bytes,
                                keep_days=self.keep_days)
            for art_id in doomed:
                meta = data.pop(art_id, None)
                changed = True
                if not meta:
                    continue
                try:
                    (self.workspace / meta.get("rel", "")).unlink(missing_ok=True)
                except OSError:
                    pass
            if changed:
                self._write_ledger(data)
            return doomed
