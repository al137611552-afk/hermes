"""FR-10.2 读写精度：read_file 行号/offset/limit、edit_file 诊断/replace_all、
multi_edit 原子多处替换（无网络）。

运行：python tests/test_fs_rw.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.fs import (  # noqa: E402
    WriteFileTool, apply_edits, diagnose_not_found,
)


def _reg(tmp: Path, tracker=None):
    return build_registry(tmp, change_tracker=tracker)


# ---- read_file：行号 / offset / limit / 截断 ---------------------------------

def test_read_line_numbers(tmp: Path):
    (tmp / "a.txt").write_text("foo\nbar\nbaz\n", encoding="utf-8")
    out = _reg(tmp).get("read_file").run({"path": "a.txt"})
    assert out == "1\tfoo\n2\tbar\n3\tbaz"
    (tmp / "e.txt").write_text("", encoding="utf-8")
    assert _reg(tmp).get("read_file").run({"path": "e.txt"}) == "(空文件)"


def test_read_offset_limit_and_continue_hint(tmp: Path):
    (tmp / "a.txt").write_text("".join(f"L{i}\n" for i in range(1, 11)), encoding="utf-8")
    out = _reg(tmp).get("read_file").run({"path": "a.txt", "offset": 4, "limit": 3})
    assert out.splitlines()[0] == "4\tL4" and "6\tL6" in out
    assert "offset=7" in out                      # 未到末尾的续读提示
    out2 = _reg(tmp).get("read_file").run({"path": "a.txt", "offset": 8})
    assert "10\tL10" in out2 and "offset=" not in out2  # 读到末尾无提示


def test_read_offset_beyond_eof(tmp: Path):
    (tmp / "a.txt").write_text("x\ny\n", encoding="utf-8")
    try:
        _reg(tmp).get("read_file").run({"path": "a.txt", "offset": 99})
        assert False, "应报超出末尾"
    except ToolError as e:
        assert "共 2 行" in str(e)


def test_read_long_line_truncated(tmp: Path):
    (tmp / "a.txt").write_text("short\n" + "x" * 5000 + "\n", encoding="utf-8")
    out = _reg(tmp).get("read_file").run({"path": "a.txt"})
    assert "行过长" in out
    long_line = out.splitlines()[1]
    assert len(long_line) < 5000 and long_line.startswith("2\t" + "x" * 10)


def test_read_char_cap_hints_continue(tmp: Path):
    # 150 行 × 2000 字符 ≈ 30 万字符 > 20 万上限：应中途停下并给续读 offset
    (tmp / "big.txt").write_text("".join("y" * 2000 + "\n" for _ in range(150)), encoding="utf-8")
    out = _reg(tmp).get("read_file").run({"path": "big.txt"})
    assert "未到文件末尾" in out and "offset=" in out
    shown = len(out.splitlines()) - 1             # 去掉提示行
    assert 50 < shown < 150                       # 没全读，也没只读几行


# ---- edit_file：可操作诊断 + replace_all --------------------------------------

def test_edit_not_found_hints(tmp: Path):
    text = "def foo():\n    return 1\n"
    (tmp / "a.py").write_text(text, encoding="utf-8")
    e = _reg(tmp).get("edit_file")
    # ① 把行号前缀带进来了
    try:
        e.run({"path": "a.py", "old_string": "1\tdef foo():", "new_string": "x"})
        assert False
    except ToolError as err:
        assert "行号前缀" in str(err)
    # ② 空白/缩进不一致
    try:
        e.run({"path": "a.py", "old_string": "def foo():\n  return 1", "new_string": "x"})
        assert False
    except ToolError as err:
        assert "空白/缩进" in str(err)
    # ③ 确实不存在
    try:
        e.run({"path": "a.py", "old_string": "def bar():", "new_string": "x"})
        assert False
    except ToolError as err:
        assert "read_file 核对" in str(err)
    assert (tmp / "a.py").read_text(encoding="utf-8") == text  # 全程未改动


def test_edit_duplicate_and_replace_all(tmp: Path):
    (tmp / "a.txt").write_text("v=1\nv=1\nv=1\n", encoding="utf-8")
    e = _reg(tmp).get("edit_file")
    try:
        e.run({"path": "a.txt", "old_string": "v=1", "new_string": "v=2"})
        assert False
    except ToolError as err:
        assert "3 次" in str(err) and "replace_all" in str(err)
    out = e.run({"path": "a.txt", "old_string": "v=1", "new_string": "v=2", "replace_all": True})
    assert "3 处" in out
    assert (tmp / "a.txt").read_text(encoding="utf-8") == "v=2\nv=2\nv=2\n"


# ---- multi_edit：原子性 / 按序 / 定位报错 -------------------------------------

def test_multi_edit_applies_in_order(tmp: Path):
    (tmp / "a.txt").write_text("alpha beta\n", encoding="utf-8")
    m = _reg(tmp).get("multi_edit")
    out = m.run({"path": "a.txt", "edits": [
        {"old_string": "alpha", "new_string": "ALPHA"},
        {"old_string": "ALPHA beta", "new_string": "ALPHA BETA"},  # 依赖第一处的结果
    ]})
    assert "2 处编辑" in out
    assert (tmp / "a.txt").read_text(encoding="utf-8") == "ALPHA BETA\n"


def test_multi_edit_atomic_on_failure(tmp: Path):
    text = "one\ntwo\nthree\n"
    (tmp / "a.txt").write_text(text, encoding="utf-8")
    m = _reg(tmp).get("multi_edit")
    try:
        m.run({"path": "a.txt", "edits": [
            {"old_string": "one", "new_string": "1"},
            {"old_string": "missing", "new_string": "x"},
        ]})
        assert False, "第 2 处应失败"
    except ToolError as e:
        assert "第 2/2 处" in str(e) and "未改动" in str(e)
    assert (tmp / "a.txt").read_text(encoding="utf-8") == text  # 第 1 处也没落盘


def test_multi_edit_validation_and_replace_all(tmp: Path):
    (tmp / "a.txt").write_text("k=0; k=0\n", encoding="utf-8")
    m = _reg(tmp).get("multi_edit")
    for bad, key in (([], "不能为空"),
                     ([{"old_string": "k=0", "new_string": "k=0"}], "相同"),
                     ([{"old_string": "", "new_string": "x"}], "不能为空")):
        try:
            m.run({"path": "a.txt", "edits": bad})
            assert False, key
        except ToolError as e:
            assert key in str(e)
    out = m.run({"path": "a.txt",
                 "edits": [{"old_string": "k=0", "new_string": "k=9", "replace_all": True}]})
    assert "共替换 2 处" in out
    assert (tmp / "a.txt").read_text(encoding="utf-8") == "k=9; k=9\n"


def test_multi_edit_registered_dangerous_and_tracked(tmp: Path):
    snaps = []
    reg = _reg(tmp, tracker=snaps.append)
    assert reg.is_dangerous("multi_edit")
    (tmp / "a.txt").write_text("v1", encoding="utf-8")
    reg.get("multi_edit").run({"path": "a.txt",
                               "edits": [{"old_string": "v1", "new_string": "v2"}]})
    assert snaps == ["a.txt"]                      # 落盘前进改动台账


# ---- 纯函数细节 ---------------------------------------------------------------

def test_pure_helpers():
    assert "行号前缀" in diagnose_not_found("abc\n", "1\tabc")
    assert "空白/缩进" in diagnose_not_found("    abc\n", "abc ")
    new, n = apply_edits("aa", [{"old_string": "a", "new_string": "b", "replace_all": True}])
    assert new == "bb" and n == 2
    try:
        apply_edits("x", "不是数组")
        assert False
    except ToolError:
        pass


# ---- 压测发现的短板回归：二进制安全 / 原子写 / 大目录截断 ----------------------

def test_write_file_can_overwrite_binary(tmp: Path):
    # 压测发现：write_file 覆盖非 UTF-8 文件曾裸崩 UnicodeDecodeError（因 diff 基线用严格读）。
    # 现应能正常覆盖（diff 读不了就跳过 diff，不阻断合法覆盖写）。
    (tmp / "b.bin").write_bytes(b"\xff\xfe\x00hello\x80")
    out = _reg(tmp).get("write_file").run({"path": "b.bin", "content": "now text"})
    assert "已写入" in str(out)
    assert (tmp / "b.bin").read_text(encoding="utf-8") == "now text"


def test_edit_binary_gives_actionable_error_not_raw_decode(tmp: Path):
    # 压测发现：edit/multi_edit 编辑二进制曾把裸 UnicodeDecodeError 回灌模型。现应是可操作 ToolError。
    (tmp / "b.bin").write_bytes(b"\xff\xfehello\x80")
    for tool, params in [
        ("edit_file", {"path": "b.bin", "old_string": "hello", "new_string": "HI"}),
        ("multi_edit", {"path": "b.bin", "edits": [{"old_string": "hello", "new_string": "HI"}]}),
    ]:
        raised = None
        try:
            _reg(tmp).get(tool).run(params)
        except ToolError as e:
            raised = str(e)
        assert raised is not None and "二进制" in raised, f"{tool} 应抛可操作 ToolError，实际：{raised!r}"


def test_write_is_atomic_no_partial_temp_left(tmp: Path):
    # 原子写：落盘后目录里不应残留 .hermes.tmp 临时文件。
    _reg(tmp).get("write_file").run({"path": "sub/a.txt", "content": "x" * 10000})
    assert (tmp / "sub" / "a.txt").read_text(encoding="utf-8") == "x" * 10000
    assert not list((tmp / "sub").glob("*.hermes.tmp")), "原子写不应残留临时文件"


def test_list_dir_caps_huge_directory(tmp: Path):
    from agentcore.tools.fs import MAX_DIR_ENTRIES
    big = tmp / "big"; big.mkdir()
    for i in range(MAX_DIR_ENTRIES + 50):
        (big / f"f{i:05d}.txt").touch()
    out = str(_reg(tmp).get("list_dir").run({"path": "big"}))
    assert out.count("\n") <= MAX_DIR_ENTRIES + 1, "大目录应被截断"
    assert f"共 {MAX_DIR_ENTRIES + 50} 项" in out, "应提示总数与已截断"


def test_write_file_append_chunks(tmp: Path):
    """分块写大文件：第一块正常写，后续 append 追加——单次输出装不下整份内容时的正解。"""
    w = WriteFileTool(tmp)
    w.run({"path": "proto.html", "content": "<html>\n"})
    out = w.run({"path": "proto.html", "content": "<body>hi</body>\n", "append": True})
    w.run({"path": "proto.html", "content": "</html>\n", "append": True})
    assert (tmp / "proto.html").read_text(encoding="utf-8") == "<html>\n<body>hi</body>\n</html>\n"
    assert "已追加" in str(out) and "现 " in str(out)      # 回报累计长度，模型知道写到哪了


def test_write_file_append_creates_when_missing(tmp: Path):
    """文件还不存在时 append 等同新建——别让分块写的第一块因为"文件没有"而失败。"""
    WriteFileTool(tmp).run({"path": "new.txt", "content": "第一块", "append": True})
    assert (tmp / "new.txt").read_text(encoding="utf-8") == "第一块"


def test_write_file_append_skips_syntax_check(tmp: Path):
    """分块写的中间态必然语法不完整（半个函数），这时跑校验只会每块喷一次假报错、把模型带偏。"""
    calls = []

    def verifier(rel):
        calls.append(rel)
        return "⚠ 语法错误：unexpected EOF"

    w = WriteFileTool(tmp, verifier=verifier)
    out_append = w.run({"path": "a.py", "content": "def f(", "append": True})
    assert "语法错误" not in str(out_append) and calls == []      # 追加时不校验
    assert "跳过语法校验" in str(out_append)                       # 但明确告知要自己收口
    out_full = w.run({"path": "a.py", "content": "def f(): pass"})
    assert "语法错误" in str(out_full) and calls == ["a.py"]       # 整体覆盖仍照常校验


def test_write_file_append_diff_shows_only_added(tmp: Path):
    """改动面板/内联 diff 看到的是本次追加的部分，不是整份文件重写。"""
    w = WriteFileTool(tmp)
    w.run({"path": "x.txt", "content": "line1\n"})
    out = w.run({"path": "x.txt", "content": "line2\n", "append": True})
    diff = getattr(out, "blocks", [{}])[0].get("diff", "")
    assert "+line2" in diff and "-line1" not in diff


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
