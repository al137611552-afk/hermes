"""Git 集成（FR-10.1）：git 工具与面板「改动」区 git 模式的底层封装。

走 git CLI（subprocess、cwd=工作区），不引入 GitPython 等新依赖。git 未安装 /
非 git 仓库 / 命令失败时抛 GitError（信息可读，回灌模型或前端都不崩）。
纯解析（parse_porcelain）与受控 IO（run_git）分离，便于单测。

面板 git 模式语义：列**全部未提交改动**（暂存/未暂存/未跟踪，跨重启、含用户手改），
diff 对 HEAD；回退＝丢弃未提交改动（tracked 用 checkout HEAD --，新增/未跟踪删文件）。
仓库礼仪走"引导不硬拦"：commit 在默认分支（main/master）上直接提交时附 ⚠ 提醒。
"""
from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from .changes import MAX_DIFF_LINES

GIT_TIMEOUT = 30                        # 单条 git 命令超时（秒）
DEFAULT_BRANCHES = ("main", "master")   # "默认分支"启发式（礼仪提醒用）
MAX_BRANCH_LINES = 20                   # 分支列表展示上限


class GitError(Exception):
    """git 不可用/执行失败等可预期错误，信息直接给模型或前端。"""


def run_git(workspace: Path, args: list[str], timeout: int = GIT_TIMEOUT) -> str:
    """在工作区执行一条 git 命令，返回 stdout；失败抛 GitError（信息可读）。"""
    argv = ["git", "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(
            argv, cwd=str(workspace), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        raise GitError("未安装 git 或不在 PATH（Windows 请安装 Git for Windows）。")
    except subprocess.TimeoutExpired:
        raise GitError(f"git 命令超时（>{timeout}s）：git {' '.join(args)}")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GitError(f"git {args[0]} 失败：{msg or f'exit code {proc.returncode}'}")
    return proc.stdout


def is_git_workspace(workspace: Path) -> bool:
    """工作区根是否 git 仓库（面板模式判定用）。

    只认根目录有 .git——工作区是某个更大仓库的子目录时不算（面板走内存台账兜底，
    避免把仓库根之外的路径语义搅混；git 工具不受此限，模型仍可直接用）。
    """
    try:
        return (Path(workspace) / ".git").exists()
    except OSError:
        return False


def has_head(workspace: Path) -> bool:
    """仓库是否已有提交（全新 git init 的仓库 HEAD 尚未出生）。"""
    try:
        run_git(workspace, ["rev-parse", "--verify", "-q", "HEAD"])
        return True
    except GitError:
        return False


def current_branch(workspace: Path) -> str:
    """当前分支名；未出生分支也能取到名字，游离 HEAD 给短 sha。"""
    try:
        name = run_git(workspace, ["symbolic-ref", "--short", "-q", "HEAD"]).strip()
        if name:
            return name
    except GitError:
        pass
    try:
        return run_git(workspace, ["rev-parse", "--short", "HEAD"]).strip() + "（游离 HEAD）"
    except GitError:
        return "(未知)"


# ---- 状态/改动 --------------------------------------------------------------

def parse_porcelain(out: str) -> list[dict]:
    """解析 `git status --porcelain -uall` 为 [{path, status}]（纯函数）。

    状态归并为面板/台账同一套词汇：added / modified / deleted；重命名取新路径记 modified。
    """
    items: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) < 4:
            continue
        x, y, rest = line[0], line[1], line[3:]
        if " -> " in rest and x in ("R", "C"):
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"').replace("\\", "/")
        if x in ("?", "A"):
            status = "added"
        elif "D" in (x, y):
            status = "deleted"
        else:
            status = "modified"  # M/R/C/U 等一律视作有改动
        items[path] = status
    return [{"path": p, "status": s} for p, s in sorted(items.items())]


def changes(workspace: Path) -> list[dict]:
    """未提交改动列表（面板 git 模式用），路径相对仓库根（=工作区根）。"""
    return parse_porcelain(run_git(workspace, ["status", "--porcelain", "-uall"]))


def status_summary(workspace: Path) -> str:
    """git_status 工具输出：分支/领先落后 + 改动短格式 + 本地分支列表。"""
    out = run_git(workspace, ["status", "--short", "--branch", "-uall"]).rstrip()
    lines = out.splitlines()
    parts = ["\n".join(lines) if len(lines) > 1 else f"{out}\n(工作区干净，无未提交改动)"]
    try:
        br = run_git(workspace, ["branch", "-v"]).rstrip()
    except GitError:
        br = ""
    if br:
        blines = br.splitlines()
        if len(blines) > MAX_BRANCH_LINES:
            blines = blines[:MAX_BRANCH_LINES] + [f"...（共 {len(br.splitlines())} 个分支，已截断）"]
        parts.append("[本地分支]\n" + "\n".join(blines))
    return "\n\n".join(parts)


# ---- diff -------------------------------------------------------------------

def _truncate(diff: str) -> str:
    lines = diff.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... (diff 过长，已截断到 {MAX_DIFF_LINES} 行)"]
    return "\n".join(lines)


def _synth_added_diff(workspace: Path, rel: str) -> "str | None":
    """未跟踪/无 HEAD 文件合成"新增"统一 diff（git diff HEAD 不含 untracked）。"""
    p = Path(workspace) / rel
    try:
        cur = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = list(difflib.unified_diff(
        [], cur.splitlines(), fromfile="/dev/null", tofile=f"b/{rel}", lineterm="",
    ))
    return _truncate("\n".join(lines)) if lines else None


def file_diff(workspace: Path, rel: str) -> "str | None":
    """单文件对 HEAD 的统一 diff（面板用）；不在改动列表或无差异返回 None。"""
    rel = (rel or "").replace("\\", "/").strip()
    if not rel:
        return None
    st = {c["path"]: c["status"] for c in changes(workspace)}
    if rel not in st:
        return None
    if st[rel] == "added" or not has_head(workspace):
        return _synth_added_diff(workspace, rel)
    out = run_git(workspace, ["diff", "HEAD", "--", rel]).rstrip()
    return _truncate(out) if out else None


def diff_text(workspace: Path, path: "str | None" = None) -> str:
    """git_diff 工具输出：对 HEAD 的 diff（可选限定路径）。

    git diff 不含未跟踪文件：限定了具体路径且它是未跟踪文件则合成新增 diff；
    未限定路径时把未跟踪文件列在末尾（不展开内容，防输出爆量）。
    """
    untracked = [c["path"] for c in changes(workspace) if c["status"] == "added"]
    if path:
        rel = path.replace("\\", "/").strip().rstrip("/")
        if rel in untracked:
            return _synth_added_diff(workspace, rel) or "(空文件)"
        args = (["diff", "HEAD"] if has_head(workspace) else ["diff"]) + ["--", rel]
        out = run_git(workspace, args).rstrip()
        return _truncate(out) if out else "(该路径相对 HEAD 无差异)"
    args = ["diff", "HEAD"] if has_head(workspace) else ["diff"]
    out = run_git(workspace, args).rstrip()
    parts = [_truncate(out)] if out else []
    if untracked:
        names = "\n".join(f"  + {p}" for p in untracked[:50])
        parts.append(f"[未跟踪的新文件]（用 git_diff 带 path 或 read_file 查看内容）\n{names}")
    return "\n\n".join(parts) if parts else "(相对 HEAD 无差异，工作区干净)"


def log_text(workspace: Path, limit: int = 20) -> str:
    """git_log 工具输出：最近 limit 条提交（单行格式）。"""
    if not has_head(workspace):
        return "(仓库还没有任何提交)"
    out = run_git(workspace, [
        "log", f"-{max(1, int(limit))}", "--date=short", "--pretty=format:%h %ad %an%d %s",
    ]).rstrip()
    return out or "(无提交)"


# ---- 回退（面板 git 模式） ---------------------------------------------------

def revert_file(workspace: Path, rel: str) -> bool:
    """丢弃单个文件的未提交改动：tracked 恢复到 HEAD；新增/未跟踪＝取消暂存并删除。

    只接受当前改动列表里的路径（路径来源即 git，天然限制在仓库内）。
    """
    rel = (rel or "").replace("\\", "/").strip()
    st = {c["path"]: c["status"] for c in changes(workspace)}
    if rel not in st:
        return False
    p = Path(workspace) / rel
    try:
        if st[rel] == "added":
            try:  # 已暂存的新增先取消暂存；纯未跟踪时本命令报错，忽略
                run_git(workspace, ["rm", "-f", "-q", "--cached", "--", rel])
            except GitError:
                pass
            if p.is_file():
                p.unlink()
        else:
            run_git(workspace, ["checkout", "HEAD", "--", rel])
        return True
    except (OSError, GitError):
        return False


def _chunks(paths: list[str], size: int = 100):
    """分批，避免命令行过长（Windows 上限约 32k 字符）。"""
    for i in range(0, len(paths), size):
        yield paths[i:i + size]


def revert_all(workspace: Path) -> int:
    """丢弃全部未提交改动，返回回退条数。

    批量执行（reset / checkout 各分批一次、未跟踪文件在 Python 内删除），不逐文件起
    git 子进程——逐文件每条要 2 次进程创建，Windows 真机实测改动上百时会卡住 UI。
    """
    before = changes(workspace)
    if not before:
        return 0
    added = [c["path"] for c in before if c["status"] == "added"]
    tracked = [c["path"] for c in before if c["status"] != "added"]
    if added:
        for batch in _chunks(added):
            try:  # 取消暂存（纯未跟踪路径 reset 安静跳过；无 HEAD 的全新仓库退化为 rm --cached）
                run_git(workspace, ["reset", "-q", "--", *batch])
            except GitError:
                try:
                    run_git(workspace, ["rm", "-r", "-f", "-q", "--cached",
                                        "--ignore-unmatch", "--", *batch])
                except GitError:
                    pass
        for rel in added:
            p = Path(workspace) / rel
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
    if tracked:
        for batch in _chunks(tracked):
            try:
                run_git(workspace, ["checkout", "HEAD", "--", *batch])
            except GitError:
                for rel in batch:  # 个别路径失败不拖垮整批：退回逐个
                    revert_file(workspace, rel)
    return len(before) - len(changes(workspace))


# ---- 写操作（工具用，过权限 gate） -------------------------------------------

def commit(workspace: Path, message: str, paths: "list[str] | None" = None) -> str:
    """暂存并提交：paths 给了只提交这些路径，否则提交全部改动。返回带分支提醒的摘要。"""
    message = (message or "").strip()
    if not message:
        raise GitError("提交说明（message）不能为空。")
    if paths:
        run_git(workspace, ["add", "--", *paths])
    else:
        run_git(workspace, ["add", "-A"])
    staged = run_git(workspace, ["status", "--porcelain"])
    if not any(line and line[0] not in " ?" for line in staged.splitlines()):
        raise GitError("没有可提交的改动（工作区干净或路径不匹配）。")
    try:
        run_git(workspace, ["commit", "-m", message])
    except GitError as e:
        s = str(e).lower()
        if any(k in s for k in ("tell me who you are", "auto-detect", "empty ident",
                                "no email was given", "no name was given")):
            raise GitError(
                "git 还没配置提交身份，无法提交。请先运行（配置一次即可）：\n"
                '  git config --global user.name "你的名字"\n'
                '  git config --global user.email "你的邮箱"\n'
                "（也可以经用户同意后用 run_powershell 代为配置。）"
            ) from None
        raise
    branch = current_branch(workspace)
    sha = run_git(workspace, ["rev-parse", "--short", "HEAD"]).strip()
    note = f"已提交 {sha} 到分支 {branch}：{message}"
    if branch in DEFAULT_BRANCHES:
        note += "\n⚠ 这是在默认分支上的直接提交（仓库礼仪：通常应先开分支；本次已经用户确认）。"
    return note


def branch(workspace: Path, op: str, name: "str | None" = None) -> str:
    """建/切分支：op = create（switch -c）/ switch。"""
    op = (op or "").strip()
    name = (name or "").strip()
    if op not in ("create", "switch"):
        raise GitError(f"不支持的 op：{op}（可选 create / switch）")
    if not name:
        raise GitError(f"{op} 需要分支名（name）。")
    if op == "create":
        run_git(workspace, ["switch", "-c", name])
        return f"已创建并切换到分支 {name}。"
    run_git(workspace, ["switch", name])
    return f"已切换到分支 {name}。"
