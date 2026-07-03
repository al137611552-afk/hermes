"""grep_search 健壮性：ReDoS 防护（长行截断）/ 大文件跳过 / 普通命中仍工作。

压测发现：用户/模型给的正则走回溯引擎，遇长行灾难回溯——`(a+)+$` 对全 a 行呈指数增长
（28 字符行 16s），re.search 不放 GIL → 卡死整个进程。修法＝喂给正则前把每行截到 MAX_GREP_LINE、
跳过超大文件、加墙钟时限。独立 runner（不依赖 pytest）。
"""
from __future__ import annotations

import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools.search import GrepSearchTool, MAX_GREP_LINE  # noqa: E402


def test_redos_pattern_on_long_line_returns_fast():
    # 灾难回溯正则打在 50 万字符全 a 单行上：截断到 MAX_GREP_LINE 后应瞬间返回（否则指数级挂死）。
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "min.js").write_text("a" * 500_000 + "\n", encoding="utf-8")
        t0 = time.time()
        GrepSearchTool(ws).run({"pattern": r"(a+)+$"})
        el = time.time() - t0
        assert el < 3, f"ReDoS 长行应被行截断挡住、快速返回，实测 {el:.1f}s"


def test_long_line_truncated_before_match():
    # 匹配点在第 MAX_GREP_LINE 之后：因行截断不应命中（证明截断生效）。
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "f.txt").write_text("x" * (MAX_GREP_LINE + 500) + "NEEDLE\n", encoding="utf-8")
        out = str(GrepSearchTool(ws).run({"pattern": "NEEDLE"}))
        assert "NEEDLE" not in out, "超过行截断长度的匹配不应被找到"
        # 而截断范围内的匹配仍能命中
        (ws / "g.txt").write_text("head NEEDLE tail\n", encoding="utf-8")
        out2 = str(GrepSearchTool(ws).run({"pattern": "NEEDLE"}))
        assert "g.txt" in out2


def test_oversized_file_skipped():
    # 超过 MAX_GREP_FILE_BYTES 的文件应被跳过（多为数据/压缩产物）。
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "big.dat").write_bytes(b"HITHERE\n" * 1_000_000)  # ~8MB
        out = str(GrepSearchTool(ws).run({"pattern": "HITHERE"}))
        assert "big.dat" not in out, "超大文件应被跳过，不参与扫描"


def test_normal_grep_still_works():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "code.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
        out = str(GrepSearchTool(ws).run({"pattern": "def foo"}))
        assert "code.py" in out and "foo" in out
        assert str(GrepSearchTool(ws).run({"pattern": "zzz_nomatch"})) == "无命中。"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
