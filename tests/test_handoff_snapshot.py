"""换手落盘自测（不起窗口、不连模型）。

运行：python tests/test_handoff_snapshot.py

换手是最容易"人去操作 → 中途出岔子 → 最后重启"的节点，而换手请求本身只活在内存里、
重启即蒸发。同源的"回合内容不丢"由 test_durable_turn.py 覆盖。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

def test_handoff_snapshot_writes_evidence_to_workspace():
    """换手时把已拿到的东西落**工作区文件**（不是 notes——重启后常落在新会话里，notes 就看不见了）。"""
    from agentcore.bridge.conversation import Conversation
    from agentcore.providers import Message

    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)

        class _Fake:
            workspace = ws
            history = [
                Message("assistant", [{"type": "tool_use", "name": "web_search",
                                       "input": {"query": "uv vs pip"}}]),
                Message("user", [{"type": "tool_result", "content": "1. uv 比 pip 快 10 倍 …"}]),
            ]
            _snapshot_handoff = Conversation._snapshot_handoff

        path = _Fake()._snapshot_handoff(
            {"target": "https://zhihu.com/q/1", "reason": "要登录", "verify": "重开目标页看是否已登录"})
        assert path, "落盘失败"
        text = Path(path).read_text(encoding="utf-8")
        assert "https://zhihu.com/q/1" in text
        assert "重开目标页看是否已登录" in text
        assert "web_search" in text and "uv 比 pip 快 10 倍" in text, "已拿到的结果没记下来"
        assert Path(path).parent.name == "handoff"


def test_handoff_snapshot_never_blocks_handoff():
    """记录失败绝不能反过来挡住换手本身——工作区不可写时要安静地返回空串。"""
    from agentcore.bridge.conversation import Conversation

    class _Broken:
        workspace = Path("/proc/nonexistent-dir/x")   # mkdir 必失败
        history = []
        _snapshot_handoff = Conversation._snapshot_handoff

    assert _Broken()._snapshot_handoff({"target": "x"}) == ""


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
