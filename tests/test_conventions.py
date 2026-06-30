"""自动生成项目规范 hermes.md 的纯逻辑自测（无网络、无模型）。

运行：python tests/test_conventions.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.conventions import (  # noqa: E402
    build_generate_request, build_project_digest, clean_output,
)


def test_digest_has_tree_and_key_files(tmp: Path):
    (tmp / "src").mkdir()
    (tmp / "src" / "app.py").write_text("print('hi')")
    (tmp / "README.md").write_text("# DemoProj\n一个示例项目")
    (tmp / "pyproject.toml").write_text("[project]\nname='demo'")
    d = build_project_digest(tmp)
    assert "目录结构" in d
    assert "app.py" in d and "src" in d           # 树含结构
    assert "DemoProj" in d                          # README 内容纳入
    assert "pyproject.toml" in d and "name='demo'" in d


def test_digest_bounded(tmp: Path):
    (tmp / "README.md").write_text("x" * 50000)
    d = build_project_digest(tmp)
    assert len(d) <= 8000                           # 总量受限


def test_generate_request_shape():
    system, msgs = build_generate_request("【目录结构】\n📄 a.py", "全局标准：要简洁")
    assert "项目规范" in system and "无关" in system  # 强调只写本项目
    assert len(msgs) == 1 and msgs[0].role == "user"
    assert "a.py" in msgs[0].content and "全局标准" in msgs[0].content


def test_generate_request_no_global():
    system, msgs = build_generate_request("摘要X", None)
    assert "摘要X" in msgs[0].content                # 没有全局标准也能用


def test_clean_output_strips_fence():
    assert clean_output("```markdown\n# 标题\n内容\n```") == "# 标题\n内容"
    assert clean_output("```\n# 纯\n```") == "# 纯"
    assert clean_output("# 没有围栏\n直接正文") == "# 没有围栏\n直接正文"
    assert clean_output("  # 去空白  ") == "# 去空白"


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
