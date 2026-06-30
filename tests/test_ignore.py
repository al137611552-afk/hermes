"""轻量 .gitignore 过滤自测：matcher + 文件树 / grep / glob 过滤。

运行：python tests/test_ignore.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.ignore import make_gitignore_matcher  # noqa: E402
from agentcore.tools import build_registry  # noqa: E402
from agentcore.workspace import build_tree  # noqa: E402


def test_gitignore_matcher(tmp: Path):
    (tmp / ".gitignore").write_text("*.log\ncoverage/\nsecrets.txt\n", encoding="utf-8")
    gi = make_gitignore_matcher(tmp)
    assert gi("app.log", "app.log")                  # *.ext
    assert gi("a/b/x.log", "x.log")                  # *.ext 任意层级
    assert gi("coverage", "coverage")                # dir/
    assert gi("coverage/r.html", "r.html")           # dir/ 子项
    assert gi("secrets.txt", "secrets.txt")          # 具体名
    assert not gi("app.py", "app.py")                # 不命中


def test_no_gitignore_matches_nothing(tmp: Path):
    gi = make_gitignore_matcher(tmp)                 # 无 .gitignore
    assert not gi("x.log", "x.log")


def test_build_tree_respects_gitignore(tmp: Path):
    (tmp / ".gitignore").write_text("*.log\nsecret/\n", encoding="utf-8")
    (tmp / "app.py").write_text("x")
    (tmp / "debug.log").write_text("x")
    (tmp / "secret").mkdir()
    (tmp / "secret" / "k.txt").write_text("x")
    names = [c["name"] for c in build_tree(tmp)["children"]]
    assert "app.py" in names
    assert "debug.log" not in names                  # 被 *.log 滤掉
    assert "secret" not in names                     # 被 secret/ 滤掉


def test_grep_glob_respect_gitignore(tmp: Path):
    (tmp / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp / "a.py").write_text("TARGET here")
    (tmp / "b.log").write_text("TARGET here")
    reg = build_registry(tmp, shell="bash")
    g = reg.get("grep_search").run({"pattern": "TARGET"})
    assert "a.py" in g and "b.log" not in g          # grep 跳 *.log
    gl = reg.get("glob_search").run({"pattern": "**/*"})
    assert "a.py" in gl and "b.log" not in gl        # glob 跳 *.log


def test_gitignore_handles_bom(tmp: Path):
    """Windows 记事本存的 .gitignore 常带 BOM，第一行模式不能因此失效。"""
    (tmp / ".gitignore").write_bytes(b"\xef\xbb\xbf*.log\ncoverage/\n")  # BOM + 第一行 *.log
    gi = make_gitignore_matcher(tmp)
    assert gi("debug.log", "debug.log")     # 曾因 BOM 粘在 *.log 上而漏过
    assert gi("coverage", "coverage")


def _run_all():
    import inspect
    import tempfile
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
