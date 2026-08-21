"""agent 型调用的客观改动清单（纯逻辑）。

**为什么不信 agent 的自述**：Codex 回来的是一段自然语言（"我修好了 X"），
它到底动了哪些文件只有 git 说了算。同一条纪律在评测里叫「判分优先程序化」。

运行：python tests/test_mcp_gitwatch.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.mcp_client.gitwatch import (  # noqa: E402
    MAX_LINES, diff_status, render_changes, status_lines,
)


def test_only_new_changes_are_attributed():
    """工作区本来就可能是脏的。事后一把梭会把**用户自己**没提交的改动算到 agent 头上——
    那种"自信的错数"比没有更糟。"""
    before = [" M user_edited.py"]
    after = [" M user_edited.py", "?? codex_new.py", " M codex_touched.py"]
    assert diff_status(before, after) == ["?? codex_new.py", " M codex_touched.py"]


def test_unmeasurable_is_not_the_same_as_clean():
    """None＝测不了（不是 git 仓库/没装 git/超时），[]＝干净。两者混淆就会显示假结论。"""
    assert diff_status(None, ["?? x"]) == []
    assert diff_status([" M a"], None) == []
    assert diff_status([], []) == []


def test_render_states_no_change_but_stays_silent_when_unmeasurable():
    """**"没有改动"要说出来**：agent 自述"已创建 xxx"而工作区毫无改动，
    是最值得当场看见的矛盾（2026-08-21 真机就是这么漏过去的）。
    但测不了（不是 git 仓库/没装 git）要保持安静——别把"没测"说成"没改"。"""
    assert "工作区无改动" in render_changes([], measurable=True)
    assert render_changes([], measurable=False) == ""
    out = render_changes(["?? a.py", " M b.py"])
    assert "2 处" in out and "?? a.py" in out


def test_render_caps_long_lists():
    out = render_changes([f"?? f{i}.py" for i in range(MAX_LINES + 5)])
    assert f"…还有 5 处" in out
    assert out.count("??") == MAX_LINES


def test_status_lines_on_a_real_repo_and_on_a_plain_dir():
    """真跑一次 git：非仓库目录必须返回 None（测不了），而不是空列表（干净）。"""
    plain = tempfile.mkdtemp(prefix="nogit_")
    assert status_lines(plain) is None
    assert status_lines("") is None

    repo = tempfile.mkdtemp(prefix="gitrepo_")
    if subprocess.run(["git", "init", "-q", repo], capture_output=True).returncode != 0:
        print("  （跳过：本机没有 git）")
        return
    assert status_lines(repo) == []                       # 空仓库＝干净
    Path(repo, "new.txt").write_text("x", encoding="utf-8")
    lines = status_lines(repo)
    assert lines and any("new.txt" in l for l in lines)


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
