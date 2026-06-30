"""工作区文件预览自测（纯函数，临时目录，无 GUI）。

运行：python tests/test_workspace.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.workspace import (  # noqa: E402
    build_tree, read_conventions, read_file, resolve_within,
)


def test_resolve_within_ok(tmp: Path):
    (tmp / "a.txt").write_text("x")
    assert resolve_within(tmp, "a.txt") == (tmp / "a.txt").resolve()
    assert resolve_within(tmp, "") == tmp.resolve()


def test_resolve_within_rejects_escape(tmp: Path):
    for bad in ["../secret", "../../etc/passwd", "sub/../../out"]:
        try:
            resolve_within(tmp, bad)
            assert False, f"应拒绝越界路径 {bad}"
        except ValueError:
            pass


def test_build_tree_skips_noise(tmp: Path):
    (tmp / "src").mkdir()
    (tmp / "src" / "app.py").write_text("print(1)")
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "x.pyc").write_text("junk")
    (tmp / ".git").mkdir()
    (tmp / ".git" / "config").write_text("junk")
    (tmp / "readme.md").write_text("# hi")

    tree = build_tree(tmp)
    names = {c["name"] for c in tree["children"]}
    assert "src" in names and "readme.md" in names
    assert "__pycache__" not in names and ".git" not in names  # 噪音目录被跳过
    # 目录在前、文件在后
    types = [c["type"] for c in tree["children"]]
    assert types == sorted(types, key=lambda t: t != "dir")
    src = next(c for c in tree["children"] if c["name"] == "src")
    assert src["children"][0]["name"] == "app.py" and src["children"][0]["path"] == "src/app.py"


def test_read_text_and_html(tmp: Path):
    (tmp / "a.py").write_text("print('hi')", encoding="utf-8")
    (tmp / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")
    r = read_file(tmp, "a.py")
    assert r["kind"] == "text" and "print" in r["text"] and r["truncated"] is False
    h = read_file(tmp, "page.html")
    assert h["kind"] == "html" and "<h1>" in h["text"]


def test_read_image_and_svg(tmp: Path):
    # 1x1 PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
    )
    (tmp / "p.png").write_bytes(png)
    (tmp / "i.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    r = read_file(tmp, "p.png")
    assert r["kind"] == "image" and r["dataUrl"].startswith("data:image/png;base64,")
    s = read_file(tmp, "i.svg")
    assert s["kind"] == "image" and "svg+xml" in s["dataUrl"]


def test_read_binary_and_truncate(tmp: Path):
    (tmp / "b.bin").write_bytes(b"\x00\x01\x02\x03" * 10)
    assert read_file(tmp, "b.bin")["kind"] == "binary"
    big = "a" * 600_000
    (tmp / "big.txt").write_text(big)
    r = read_file(tmp, "big.txt")
    assert r["kind"] == "text" and r["truncated"] is True and len(r["text"]) <= 500_000


def test_read_missing(tmp: Path):
    assert read_file(tmp, "nope.txt")["kind"] == "error"


def test_read_conventions(tmp: Path):
    assert read_conventions(tmp, "hermes.md") == ""        # 不存在 -> ""
    assert read_conventions(tmp, "") == ""                  # 关闭 -> ""
    (tmp / "hermes.md").write_text("  # 规范\n- 先读后改  ", encoding="utf-8")
    out = read_conventions(tmp, "hermes.md")
    assert "先读后改" in out and out == out.strip()         # 读到并去空白
    # 越界文件名不读
    (tmp.parent / "outside.md").write_text("SECRET")
    assert read_conventions(tmp, "../outside.md") == ""
    # 超长截断
    (tmp / "big.md").write_text("x" * 30000)
    assert len(read_conventions(tmp, "big.md")) <= 20000


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                if "tmp" in inspect.signature(fn).parameters:
                    fn(Path(d))
                else:
                    fn()
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
