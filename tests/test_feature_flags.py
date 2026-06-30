"""功能开关持久化 + 合并 + bridge 即时生效自测（GUI「功能开关」面板后端）。

运行：python tests/test_feature_flags.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import (  # noqa: E402
    merge_feature_flags, read_feature_flags, set_feature_flags,
)


# ---- 持久化 read/set（受控 IO）----

def test_set_and_read_roundtrip(tmp: Path):
    p = tmp / "ff.json"
    out = set_feature_flags({"auto_affected_test": True, "auto_review": True}, path=p)
    assert out == {"auto_affected_test": True, "auto_review": True}
    assert read_feature_flags(p) == {"auto_affected_test": True, "auto_review": True}


def test_set_merges_not_overwrites(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"auto_affected_test": True}, path=p)
    set_feature_flags({"auto_review": True}, path=p)  # 不该清掉前一个
    d = read_feature_flags(p)
    assert d == {"auto_affected_test": True, "auto_review": True}


def test_set_rejects_unknown_keys(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"auto_affected_test": True, "evil_flag": True, "system_prompt": "x"}, path=p)
    d = read_feature_flags(p)
    assert "evil_flag" not in d and "system_prompt" not in d
    assert d == {"auto_affected_test": True}


def test_set_test_command_string(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"auto_test": True, "test_command": "pytest -q"}, path=p)
    d = read_feature_flags(p)
    assert d == {"auto_test": True, "test_command": "pytest -q"}


def test_set_delegate_max_revisions_int(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"delegate_max_revisions": 2}, path=p)
    assert read_feature_flags(p) == {"delegate_max_revisions": 2}
    data = merge_feature_flags({"agent": {"delegate_max_revisions": 0}}, path=p)
    assert data["agent"]["delegate_max_revisions"] == 2


def test_read_missing_or_bad(tmp: Path):
    assert read_feature_flags(tmp / "nope.json") == {}
    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_feature_flags(bad) == {}


# ---- merge 到 config data（纯逻辑，注入 path）----

def test_merge_overrides_agent_defaults(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"auto_affected_test": True, "test_command": "npm test"}, path=p)
    data = {"agent": {"auto_affected_test": False, "shell": "powershell"}}
    merged = merge_feature_flags(data, path=p)
    assert merged["agent"]["auto_affected_test"] is True       # 覆盖默认
    assert merged["agent"]["test_command"] == "npm test"       # 新增
    assert merged["agent"]["shell"] == "powershell"            # 其它字段不动


def test_merge_no_flags_is_noop(tmp: Path):
    data = {"agent": {"auto_affected_test": False}}
    out = merge_feature_flags(data, path=tmp / "nope.json")
    assert out["agent"]["auto_affected_test"] is False


def test_merge_creates_agent_if_absent(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"auto_review": True}, path=p)
    out = merge_feature_flags({}, path=p)
    assert out["agent"]["auto_review"] is True


# ---- 持久化文件就是合法 JSON ----

def test_persisted_is_valid_json(tmp: Path):
    p = tmp / "ff.json"
    set_feature_flags({"auto_test": True, "test_command": "中文命令"}, path=p)
    json.loads(p.read_text(encoding="utf-8"))  # 不抛即合法（含非 ASCII）


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
