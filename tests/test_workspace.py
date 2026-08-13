"""工作区文件预览自测（纯函数，临时目录，无 GUI）。

运行：python tests/test_workspace.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.workspace import (  # noqa: E402
    build_tree, open_in_default_app, open_plan, read_conventions, read_file,
    resolve_within,
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


# ---- 「在浏览器打开」（右侧预览面板）--------------------------------------
# 真机 bug（2026-08-13）：打开**已有项目**后，预览里点「在浏览器打开」没反应、也没报错。
# 两处缺陷叠加：① 后端忽略 webbrowser.open() 的布尔返回值，打不开也回 ok:True；
# ② 前端完全不看返回值。根因是 file:// URI 的百分号编码——见 open_plan 注释。

def test_open_plan_prefers_native_path_over_percent_encoded_uri():
    """Windows 首选原生路径：as_uri() 会把中文/空格编码，ShellExecute 解不可靠 → 静默失败。"""
    raw = r"C:\Users\张三\我的项目\index.html"
    steps = open_plan("win32", raw)
    assert steps[0] == ("startfile", raw)      # 原样传给 startfile，未经任何编码
    assert "%" not in steps[0][1]
    # 记录问题所在：同一路径走 URI 就成了这副样子（真机 WindowsPath 上才是完整形态）
    from pathlib import PureWindowsPath
    assert "%E5%BC%A0" in PureWindowsPath(raw).as_uri()


def test_open_plan_never_raises_on_unexpressible_uri():
    """as_uri() 对相对路径会抛 ValueError——兜底手段不能把整条打开路径炸掉。"""
    steps = open_plan("linux", "relative/a.html")     # 不抛异常即通过
    assert steps and steps[0][0] == "run"


def test_open_plan_per_platform():
    assert open_plan("darwin", "/a/b.html")[0] == ("run", ["open", "/a/b.html"])
    assert open_plan("linux", "/a/b.html")[0] == ("run", ["xdg-open", "/a/b.html"])
    for plat in ("win32", "darwin", "linux"):
        assert open_plan(plat, "/a/b.html")[-1][0] == "browser"   # 每个平台都有兜底


def test_open_in_default_app_reports_failure_honestly(tmp: Path):
    """全部手段失败 → 必须回 (False, 原因)，不能像老实现那样谎报成功。"""
    f = tmp / "a.html"
    f.write_text("<h1>x</h1>")
    ok, err = open_in_default_app(f, platform="linux",
                                  run=lambda argv: False,        # xdg-open 打不开
                                  browser=lambda uri: False)     # webbrowser 也返回 False
    assert ok is False
    assert err and "xdg-open" in err


def test_open_in_default_app_falls_back_to_browser(tmp: Path):
    """首选手段不可用时降级到下一种，而不是直接失败。"""
    f = tmp / "a.html"
    f.write_text("<h1>x</h1>")
    seen = []
    ok, err = open_in_default_app(
        f, platform="linux",
        run=lambda argv: (_ for _ in ()).throw(FileNotFoundError("xdg-open")),  # 没装 xdg-open
        browser=lambda uri: seen.append(uri) or True)
    assert ok is True and err == ""
    assert seen and seen[0].startswith("file://")


def test_open_in_default_app_windows_uses_startfile(tmp: Path):
    """Windows 路径上真正被调用的是 startfile(原生路径)，不是 browser(URI)。"""
    f = tmp / "a.html"
    f.write_text("<h1>x</h1>")
    called = []
    ok, err = open_in_default_app(f, platform="win32",
                                  startfile=lambda p: called.append(p),
                                  browser=lambda uri: called.append(uri) or True)
    assert ok is True
    assert called == [str(f)]                 # 只调了 startfile，且传的是原生路径
    assert "%" not in called[0]


def test_open_in_default_app_survives_exceptions(tmp: Path):
    """某一步抛异常不能把整条路堵死，要接着试下一种。"""
    f = tmp / "a.html"
    f.write_text("<h1>x</h1>")
    ok, err = open_in_default_app(
        f, platform="win32",
        startfile=lambda p: (_ for _ in ()).throw(OSError("拒绝访问")),
        browser=lambda uri: True)
    assert ok is True and err == ""


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
