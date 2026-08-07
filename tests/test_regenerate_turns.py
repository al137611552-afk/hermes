"""重新生成 / 编辑重发的轮次定位自测（无 GUI、无网络）。

回归点：工具结果作为 role==user 消息回灌（loop.py），不算独立用户轮次；
`_nth_user_index` 只数真实用户轮次，须与前端 userTurns 编号 1:1，
否则有工具调用的对话里「重新生成」会命中错误的消息（见 bug 修复）。

运行：python tests/test_regenerate_turns.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.bridge.conversation import Conversation  # noqa: E402
from agentcore.providers.base import Message  # noqa: E402


def _conv(history):
    """绕过重型 __init__，只装配 _nth_user_index 需要的 history。"""
    c = Conversation.__new__(Conversation)
    c.history = history
    return c


def _tool_use():
    return [{"type": "text", "text": "让我查一下"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]


def _tool_result():
    return [{"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]


# ---- 静态判定：区分真实用户轮次 vs 工具结果回灌 -----------------------
def test_is_real_user_turn():
    assert Conversation._is_real_user_turn(Message("user", "hi")) is True
    # 文本+图片的真实用户消息
    assert Conversation._is_real_user_turn(
        Message("user", [{"type": "text", "text": "看图"},
                         {"type": "image", "source": {}}])) is True
    # tool_result 回灌：不是用户轮次
    assert Conversation._is_real_user_turn(Message("user", _tool_result())) is False
    # 即便工具结果消息里并列了注入的文本提示，仍不算用户轮次
    assert Conversation._is_real_user_turn(
        Message("user", _tool_result() + [{"type": "text", "text": "收尾"}])) is False
    # assistant 永远不是用户轮次
    assert Conversation._is_real_user_turn(Message("assistant", "ans")) is False


# ---- 计数：有工具往返的多轮对话，轮次编号跳过工具结果消息 -------------
def test_nth_user_index_skips_tool_results():
    history = [
        Message("user", "A"),            # 0  真实用户轮次 0
        Message("assistant", _tool_use()),
        Message("user", _tool_result()),  # 工具结果回灌（不算轮次）
        Message("assistant", "ans1"),
        Message("user", "B"),            # 4  真实用户轮次 1
        Message("assistant", "ans2"),
    ]
    c = _conv(history)
    assert c._nth_user_index(0) == 0    # 命中 "A"（而非把工具结果算进去）
    assert c._nth_user_index(1) == 4    # 命中 "B"
    assert c._nth_user_index(2) is None  # 越界


def test_nth_user_index_plain_conversation():
    history = [
        Message("user", "A"),      # 0
        Message("assistant", "a"),
        Message("user", "B"),      # 2
        Message("assistant", "b"),
    ]
    c = _conv(history)
    assert c._nth_user_index(0) == 0
    assert c._nth_user_index(1) == 2
    assert c._nth_user_index(2) is None


# ---- 极简 runner（不依赖 pytest） --------------------------------------
def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
