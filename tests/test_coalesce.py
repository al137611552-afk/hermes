"""FR-12.3 流式事件合并：顺序与完整性是硬约束（不触网、不起窗口）。

运行：python tests/test_coalesce.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.bridge.coalesce import ChunkCoalescer  # noqa: E402


def test_chunks_within_window_are_merged():
    c = ChunkCoalescer(window_s=0.08)
    assert c.feed("chunk", "你", 1, 0.00) == []      # 攒着
    assert c.feed("chunk", "好", 1, 0.02) == []
    assert c.feed("chunk", "吗", 1, 0.09) == [("chunk", "你好吗", 1)]   # 到点：一次发出，顺序不乱


def test_other_events_flush_pending_first_and_keep_order():
    """要害：攒着的文本必须排在打断它的事件**前面**——否则界面上工具块会跑到那段话上面去。"""
    c = ChunkCoalescer(window_s=10)                  # 窗口极长，靠打断才冲
    c.feed("chunk", "我去查一下", 1, 0.0)
    out = c.feed("tool_use", {"name": "web_search"}, 1, 0.01)
    assert out == [("chunk", "我去查一下", 1), ("tool_use", {"name": "web_search"}, 1)]
    assert not c.pending()


def test_streams_of_different_conversations_do_not_mix():
    c = ChunkCoalescer(window_s=10)
    c.feed("chunk", "A1", 1, 0.0)
    c.feed("chunk", "B1", 2, 0.0)
    c.feed("chunk", "A2", 1, 0.0)
    # 冲对话 1 不该动到对话 2
    assert c.feed("done", {}, 1, 0.01) == [("chunk", "A1A2", 1), ("done", {}, 1)]
    assert c.flush_cid(2) == [("chunk", "B1", 2)]


def test_thinking_and_chunk_are_separate_streams():
    c = ChunkCoalescer(window_s=10)
    c.feed("thinking", "想…", 1, 0.0)
    c.feed("chunk", "答", 1, 0.0)
    got = dict((e, d) for e, d, _ in c.flush_cid(1))
    assert got == {"thinking": "想…", "chunk": "答"}   # 两条流各攒各的，不会串成一段


def test_due_flushes_the_tail_when_stream_goes_quiet():
    """出字一停就没人再喂了；没有定时器这一路，最后一截会永远压着不显示。"""
    c = ChunkCoalescer(window_s=0.08)
    c.feed("chunk", "尾巴", 1, 0.0)
    assert c.due(0.05) == []                          # 还没到点
    assert c.due(0.09) == [("chunk", "尾巴", 1)]
    assert not c.pending()


def test_big_payload_flushes_early():
    """攒太大反而卡：一次 json.dumps + evaluate_js 的大 payload 会把 UI 线程按住。"""
    c = ChunkCoalescer(window_s=100, max_chars=10)
    assert c.feed("chunk", "x" * 4, 1, 0.0) == []
    assert c.feed("chunk", "y" * 8, 1, 0.0) == [("chunk", "x" * 4 + "y" * 8, 1)]


def test_non_string_payload_is_never_coalesced():
    """event 名对但 data 不是字符串（历史/异常数据）：直通，别拼出个 TypeError。"""
    c = ChunkCoalescer(window_s=10)
    assert c.feed("chunk", {"weird": 1}, 1, 0.0) == [("chunk", {"weird": 1}, 1)]


def test_nothing_is_lost_across_a_whole_turn():
    """不变量：一整轮里 push 进去的所有文本，拼起来必须和发出去的完全一致、顺序一致。"""
    c = ChunkCoalescer(window_s=0.08)
    src, sent, t = "", [], 0.0
    for i in range(300):
        tok = f"t{i % 7}"
        src += tok
        t += 0.01                                     # 100 token/s
        sent += c.feed("chunk", tok, 9, t)
        if i == 150:                                  # 中途插一个工具事件打断
            sent += c.feed("tool_use", {"name": "read_file"}, 9, t)
    sent += c.flush_all()
    assert "".join(d for e, d, _ in sent if e == "chunk") == src
    assert [e for e, _, _ in sent].count("tool_use") == 1


def test_merging_actually_reduces_calls():
    c = ChunkCoalescer(window_s=0.08)
    calls, t = 0, 0.0
    for _ in range(400):                              # 400 token @ 25ms = 40 token/s
        t += 0.025
        calls += len(c.feed("chunk", "x", 1, t))
    calls += len(c.flush_all())
    assert calls < 400 / 3, f"合并后仍有 {calls} 次调用，没起到作用"


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
