"""改动台账（FR-9.4a）：追踪 Agent 对工作区文件的修改，支持 diff 评审与回退。

每个对话一本台账（内存级、随运行时存在，重启即清——文件本身不受影响）：
- 工具（write_file/edit_file）改某文件**之前**调用 `snapshot()` 记基线：首次记录该文件
  当时的内容（不存在则记 None=新增）；同一文件后续再改不覆盖基线——diff/回退始终
  相对"本对话第一次动它之前"的状态。
- diff 用标准库 difflib 出统一格式；回退=写回基线（新增文件回退=删除）。

只追踪经由文件工具的修改；run_powershell 等命令改的文件不在台账内（已知限制）。
纯逻辑 + 少量受控 IO（读基线/回退写盘），便于单测。
"""
from __future__ import annotations

import difflib
from pathlib import Path

MAX_BASELINE_BYTES = 2_000_000   # 超大文件不快照（不追踪），避免内存膨胀
MAX_DIFF_LINES = 2000            # diff 输出上限（防撑爆前端/上下文）


class ChangeLedger:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        # 相对路径 -> 基线内容（str；None=改动前不存在，即新增）
        self._baselines: dict[str, "str | None"] = {}

    # ---- 记录（工具改文件前调用） ----------------------------------------

    def snapshot(self, relpath: str) -> None:
        """在文件即将被修改前记基线；同一文件只记第一次。失败静默（不挡写入）。"""
        rel = (relpath or "").replace("\\", "/").strip()
        if not rel or rel in self._baselines:
            return
        p = self.workspace / rel
        try:
            if not p.exists():
                self._baselines[rel] = None  # 新增
            elif p.is_file() and p.stat().st_size <= MAX_BASELINE_BYTES:
                self._baselines[rel] = p.read_text(encoding="utf-8", errors="replace")
            # 超大/非常规文件：不追踪
        except OSError:
            pass

    # ---- 查询 -------------------------------------------------------------

    def _current(self, rel: str) -> "str | None":
        p = self.workspace / rel
        try:
            if not p.is_file():
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def changes(self) -> list[dict]:
        """当前改动列表 [{path, status}]；与基线内容相同的（改了又改回）不算改动。"""
        out: list[dict] = []
        for rel, base in sorted(self._baselines.items()):
            cur = self._current(rel)
            if base is None:
                if cur is None:
                    continue  # 记录为新增但文件已不在：无事发生
                status = "added"
            else:
                if cur == base:
                    continue  # 内容改回去了
                status = "deleted" if cur is None else "modified"
            out.append({"path": rel, "status": status})
        return out

    def diff(self, relpath: str) -> "str | None":
        """该文件相对基线的统一 diff；未在台账或无差异返回 None。"""
        rel = (relpath or "").replace("\\", "/").strip()
        if rel not in self._baselines:
            return None
        base = self._baselines[rel] or ""
        cur = self._current(rel) or ""
        if base == cur:
            return None
        lines = list(difflib.unified_diff(
            base.splitlines(), cur.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
        ))
        if len(lines) > MAX_DIFF_LINES:
            lines = lines[:MAX_DIFF_LINES] + [f"... (diff 过长，已截断到 {MAX_DIFF_LINES} 行)"]
        return "\n".join(lines)

    # ---- 回退 ---------------------------------------------------------------

    def revert(self, relpath: str) -> bool:
        """把文件恢复到基线（新增文件=删除）。成功后从台账移除该项。"""
        rel = (relpath or "").replace("\\", "/").strip()
        if rel not in self._baselines:
            return False
        base = self._baselines[rel]
        p = self.workspace / rel
        try:
            if base is None:
                if p.is_file():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(base, encoding="utf-8")
        except OSError:
            return False
        self._baselines.pop(rel, None)
        return True

    def revert_all(self) -> int:
        """回退所有有差异的改动，返回回退条数。"""
        n = 0
        for c in self.changes():
            if self.revert(c["path"]):
                n += 1
        return n
