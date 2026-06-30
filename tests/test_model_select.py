"""前端模型选择持久化自测：persist_model_selection 按行替换、不破坏 config.yaml 其余内容。

运行：python tests/test_model_select.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import persist_model_selection  # noqa: E402

# 一份带注释、多行 system_prompt、注释掉的 subagent_model 的迷你 config（贴近真实结构）
SAMPLE = """\
active_model: ark-kimi   # 当前主模型

system_prompt: |
  你是 Hermes。
  - 先读后改
  - 最小改动

agent:
  max_steps: 40
  # subagent_model: ark-deepseek  # 委派子任务默认用的模型档案；省略 = 当前对话模型
  auto_review: false

models:
  ark-kimi:
    provider: openai
    model: kimi
  ark-deepseek:
    provider: openai
    model: deepseek
"""


def _write(tmp: Path) -> Path:
    p = tmp / "config.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_persist_active_model_only_changes_that_line(tmp: Path):
    p = _write(tmp)
    persist_model_selection(active="ark-deepseek", path=p)
    out = p.read_text(encoding="utf-8")
    assert "active_model: ark-deepseek" in out
    assert "active_model: ark-kimi" not in out
    # 其余结构原样保留（注释、system_prompt 多行、models 段）
    assert "system_prompt: |" in out and "- 先读后改" in out
    assert "# subagent_model: ark-deepseek" in out  # 没动 subagent 行
    assert out.count("\n") == SAMPLE.count("\n")     # 行数不变（没整文件重排）


def test_persist_subagent_enable_uncomments(tmp: Path):
    p = _write(tmp)
    persist_model_selection(subagent="ark-deepseek", update_subagent=True, path=p)
    out = p.read_text(encoding="utf-8")
    # 注释行被启用（保留 2 空格缩进），不再是注释
    assert "  subagent_model: ark-deepseek" in out
    assert "# subagent_model: ark-deepseek" not in out
    assert "active_model: ark-kimi" in out           # 主模型行没动


def test_persist_subagent_follow_recomments(tmp: Path):
    p = _write(tmp)
    persist_model_selection(subagent="ark-deepseek", update_subagent=True, path=p)
    persist_model_selection(subagent=None, update_subagent=True, path=p)  # 改回跟随主模型
    out = p.read_text(encoding="utf-8")
    assert "subagent_model:" in out
    # 已写回注释形态（跟随主模型）：该行是被注释的
    sub_line = next(l for l in out.splitlines() if "subagent_model" in l)
    assert sub_line.lstrip().startswith("#")


def test_persist_both_at_once(tmp: Path):
    p = _write(tmp)
    persist_model_selection(active="ark-deepseek", subagent="ark-kimi", update_subagent=True, path=p)
    out = p.read_text(encoding="utf-8")
    assert "active_model: ark-deepseek" in out
    assert "  subagent_model: ark-kimi" in out


def test_persist_missing_file_is_noop(tmp: Path):
    persist_model_selection(active="x", path=tmp / "nope.yaml")  # 不存在 -> 不抛错


def _run_all():
    import inspect
    import tempfile
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d)) if "tmp" in inspect.signature(fn).parameters else fn()
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
