"""FR-10.1 Git 集成：gitsupport 封装 + git 工具（本地临时仓库，无网络）。

运行：python tests/test_git.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore import gitsupport as g  # noqa: E402
from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402


def _git(tmp: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=tmp, check=True, capture_output=True)


def _repo(tmp: Path) -> None:
    _git(tmp, "init", "-q", "-b", "main")
    _git(tmp, "config", "user.email", "t@example.com")
    _git(tmp, "config", "user.name", "tester")


def _seed(tmp: Path) -> None:
    """基线提交：a.txt + b.txt。"""
    (tmp / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
    (tmp / "b.txt").write_text("bye\n", encoding="utf-8")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-q", "-m", "base")


# ---- 纯解析 -----------------------------------------------------------------

def test_parse_porcelain(tmp: Path):
    out = (
        " M src/a.py\n"
        "A  new_staged.txt\n"
        "?? untracked.txt\n"
        " D gone.txt\n"
        "R  old.txt -> renamed.txt\n"
        '?? "中 文.txt"\n'
    )
    chg = {c["path"]: c["status"] for c in g.parse_porcelain(out)}
    assert chg == {
        "src/a.py": "modified", "new_staged.txt": "added", "untracked.txt": "added",
        "gone.txt": "deleted", "renamed.txt": "modified", "中 文.txt": "added",
    }


# ---- 状态 / 改动 / diff ------------------------------------------------------

def test_changes_and_status(tmp: Path):
    _repo(tmp); _seed(tmp)
    (tmp / "a.txt").write_text("line1\nline2 changed\n", encoding="utf-8")
    (tmp / "b.txt").unlink()
    (tmp / "c.txt").write_text("new\n", encoding="utf-8")
    chg = {c["path"]: c["status"] for c in g.changes(tmp)}
    assert chg == {"a.txt": "modified", "b.txt": "deleted", "c.txt": "added"}
    s = g.status_summary(tmp)
    assert "main" in s and "a.txt" in s and "[本地分支]" in s
    assert g.current_branch(tmp) == "main"


def test_file_diff(tmp: Path):
    _repo(tmp); _seed(tmp)
    (tmp / "a.txt").write_text("line1\nline2 changed\n", encoding="utf-8")
    d = g.file_diff(tmp, "a.txt")
    assert "-line2" in d and "+line2 changed" in d
    (tmp / "c.txt").write_text("hello\n", encoding="utf-8")
    d2 = g.file_diff(tmp, "c.txt")  # 未跟踪：合成新增 diff
    assert "+hello" in d2 and "b/c.txt" in d2
    assert g.file_diff(tmp, "不存在.txt") is None
    # 工具级 diff：不带 path 时未跟踪只列名；带 path 时能看内容
    all_d = g.diff_text(tmp)
    assert "+line2 changed" in all_d and "未跟踪的新文件" in all_d and "c.txt" in all_d
    assert "+hello" in g.diff_text(tmp, "c.txt")


def test_revert(tmp: Path):
    _repo(tmp); _seed(tmp)
    (tmp / "a.txt").write_text("changed", encoding="utf-8")
    (tmp / "b.txt").unlink()
    (tmp / "c.txt").write_text("new\n", encoding="utf-8")
    assert g.revert_file(tmp, "a.txt") is True
    assert (tmp / "a.txt").read_text(encoding="utf-8") == "line1\nline2\n"
    assert g.revert_file(tmp, "c.txt") is True       # 新增=删除
    assert not (tmp / "c.txt").exists()
    assert g.revert_file(tmp, "不存在.txt") is False
    assert g.revert_all(tmp) == 1                     # 还剩 b.txt（删除→恢复）
    assert (tmp / "b.txt").exists() and g.changes(tmp) == []


def test_revert_all_many_batched(tmp: Path):
    """改动上百时全部回退须批量执行（Windows 逐文件 spawn 实测卡 UI）：正确性 + 分批边界。"""
    _repo(tmp)
    for i in range(115):  # >100，跨 _chunks 分批边界
        (tmp / f"f{i:03}.txt").write_text("v1\n", encoding="utf-8")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-q", "-m", "base")
    for i in range(105):
        (tmp / f"f{i:03}.txt").write_text("v2\n", encoding="utf-8")   # modified
    for i in range(105, 115):
        (tmp / f"f{i:03}.txt").unlink()                                # deleted
    for i in range(30):
        (tmp / f"new{i:02}.txt").write_text("n\n", encoding="utf-8")   # untracked
    assert g.revert_all(tmp) == 145
    assert g.changes(tmp) == []
    assert (tmp / "f000.txt").read_text(encoding="utf-8") == "v1\n"   # 改回原值
    assert (tmp / "f110.txt").exists()                                # 删除被恢复
    assert not (tmp / "new00.txt").exists()                           # 未跟踪被删


def test_empty_repo_revert_all(tmp: Path):
    """HEAD 未出生的全新仓库：暂存+未跟踪混合也能全部回退（reset 失败退化 rm --cached）。"""
    _repo(tmp)
    for i in range(3):
        (tmp / f"s{i}.txt").write_text("x", encoding="utf-8")
    _git(tmp, "add", "-A")                                            # 暂存（'A '）
    (tmp / "u.txt").write_text("y", encoding="utf-8")                 # 未跟踪（'??'）
    assert g.revert_all(tmp) == 4
    assert g.changes(tmp) == [] and not (tmp / "u.txt").exists()


# ---- commit / log / branch（礼仪：引导不硬拦） --------------------------------

def test_commit_default_branch_warn(tmp: Path):
    _repo(tmp); _seed(tmp)
    (tmp / "a.txt").write_text("v2", encoding="utf-8")
    note = g.commit(tmp, "feat: 改 a")
    assert "main" in note and "⚠" in note            # 默认分支直接提交带提醒，但不拒绝
    assert g.changes(tmp) == []
    assert "feat: 改 a" in g.log_text(tmp, 5)


def test_branch_and_commit_no_warn(tmp: Path):
    _repo(tmp); _seed(tmp)
    assert "feature/x" in g.branch(tmp, "create", "feature/x")
    assert g.current_branch(tmp) == "feature/x"
    (tmp / "a.txt").write_text("v2", encoding="utf-8")
    note = g.commit(tmp, "feat: on branch")
    assert "feature/x" in note and "⚠" not in note
    assert "main" in g.branch(tmp, "switch", "main")
    try:
        g.branch(tmp, "drop", "x")
        assert False, "应拒绝未知 op"
    except g.GitError as e:
        assert "op" in str(e)


def test_commit_paths_only(tmp: Path):
    _repo(tmp); _seed(tmp)
    (tmp / "a.txt").write_text("v2", encoding="utf-8")
    (tmp / "c.txt").write_text("new\n", encoding="utf-8")
    g.commit(tmp, "只提交 a", paths=["a.txt"])
    chg = {c["path"]: c["status"] for c in g.changes(tmp)}
    assert chg == {"c.txt": "added"}                  # c 未被卷进提交
    try:
        g.commit(tmp, "空提交", paths=["a.txt"])      # a 已干净：没有可提交内容
        assert False, "应报没有可提交的改动"
    except g.GitError as e:
        assert "没有可提交" in str(e)


def test_commit_identity_hint(tmp: Path):
    """没配 git 身份时 commit 给中文可操作提示（Windows 真机反馈：原生报错不直观）。"""
    import os
    _git(tmp, "init", "-q", "-b", "main")  # 故意不配 user.name/email
    # 开发机 git 会用 用户名@主机名 自动推断身份；关掉自动推断以复现 Windows 的失败
    _git(tmp, "config", "user.useConfigOnly", "true")
    (tmp / "a.txt").write_text("x", encoding="utf-8")
    saved = {k: os.environ.get(k) for k in
             ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_AUTHOR_NAME",
              "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL")}
    try:  # 屏蔽全局/系统配置与环境身份，确保命中"未配置身份"分支
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
        for k in saved:
            if k.startswith(("GIT_AUTHOR", "GIT_COMMITTER")) or k == "EMAIL":
                os.environ.pop(k, None)
        try:
            g.commit(tmp, "test")
            assert False, "应报未配置身份"
        except g.GitError as e:
            assert "提交身份" in str(e) and "user.name" in str(e)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_empty_repo(tmp: Path):
    _repo(tmp)  # 无提交（HEAD 未出生）
    assert g.has_head(tmp) is False
    (tmp / "x.txt").write_text("first\n", encoding="utf-8")
    assert g.changes(tmp) == [{"path": "x.txt", "status": "added"}]
    assert "+first" in g.file_diff(tmp, "x.txt")      # 合成新增 diff
    assert "还没有任何提交" in g.log_text(tmp)
    note = g.commit(tmp, "首次提交")                  # 首次提交可用
    assert "main" in note and g.has_head(tmp) is True


# ---- 非 git 仓库 / 工具层 -----------------------------------------------------

def test_not_a_repo(tmp: Path):
    assert g.is_git_workspace(tmp) is False
    try:
        g.changes(tmp)
        assert False, "非仓库应报错"
    except g.GitError as e:
        assert "git" in str(e)
    reg = build_registry(tmp)
    try:
        reg.get("git_status").run({})
        assert False, "工具应转成 ToolError"
    except ToolError:
        pass


def test_tools(tmp: Path):
    _repo(tmp); _seed(tmp)
    reg = build_registry(tmp)
    for name in ("git_status", "git_diff", "git_log", "git_commit", "git_branch"):
        assert name in reg.names()
    assert not reg.is_dangerous("git_status") and not reg.is_dangerous("git_diff")
    assert not reg.is_dangerous("git_log")
    assert reg.is_dangerous("git_commit") and reg.is_dangerous("git_branch")
    (tmp / "c.txt").write_text("hello\n", encoding="utf-8")
    assert "+hello" in reg.get("git_diff").run({"path": "c.txt"})
    assert "base" in reg.get("git_log").run({"limit": 1})
    note = reg.get("git_commit").run({"message": "tool 提交", "paths": ["c.txt"]})
    assert "tool 提交" in note
    try:
        reg.get("git_commit").run({"message": "  "})
        assert False, "空 message 应报错"
    except ToolError as e:
        assert "message" in str(e)


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
