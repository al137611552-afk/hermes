"""P6.2 上下文 token 预算与压缩自测（无 GUI、无网络）。

运行：python tests/test_p6_context.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.context import (  # noqa: E402
    compress,
    estimate_tokens,
    estimate_tokens_text,
    _is_user_turn,
)
from agentcore.providers import Message  # noqa: E402

_FILLER = "这是一段用于撑大上下文的中文内容反复出现以触发预算压缩。" * 20  # ~600 字


def _turns(n: int) -> list[Message]:
    """构造 n 个普通对话回合（user 文本 + assistant 文本）。"""
    msgs: list[Message] = []
    for i in range(n):
        msgs.append(Message("user", f"第{i}轮提问 {_FILLER}"))
        msgs.append(Message("assistant", f"第{i}轮回答 {_FILLER}"))
    return msgs


def test_estimate_text():
    assert estimate_tokens_text("") == 0
    # 纯 ASCII 约 4 字符/token；中文约 1 字符/token，后者明显更高
    assert estimate_tokens_text("你好世界") > estimate_tokens_text("hello world")


def test_no_compress_under_budget():
    msgs = _turns(2)
    res = compress(msgs, "sys", budget=10_000_000, keep_recent_turns=6)
    assert res.compressed is False
    assert res.messages is msgs and res.system == "sys"
    assert res.dropped == 0


def test_compress_drops_old_keeps_recent():
    msgs = _turns(8)
    before = estimate_tokens(msgs, "sys")
    res = compress(msgs, "sys", budget=before // 3, keep_recent_turns=2)
    assert res.compressed is True
    assert res.dropped > 0
    assert res.after_tokens < res.before_tokens
    # 至少保留 keep_recent_turns 个真实用户回合
    kept_turns = sum(1 for m in res.messages if _is_user_turn(m))
    assert kept_turns >= 2
    # 摘要进入 system，且标明省略条数
    assert "此前对话摘要" in res.system
    assert "第0轮提问" in res.system  # 最早的内容被摘要保留了线索


def test_kept_starts_on_clean_boundary():
    """裁剪点必须落在真实用户回合，绝不能以 tool_result 开头（破坏 tool 配对）。"""
    msgs: list[Message] = []
    for i in range(6):
        msgs.append(Message("user", f"提问{i} {_FILLER}"))
        # assistant 发起工具调用
        msgs.append(Message("assistant", [
            {"type": "text", "text": f"我来处理{i}"},
            {"type": "tool_use", "id": f"c{i}", "name": "read_file", "input": {"path": "x"}},
        ]))
        # tool_result 回灌（role==user，但不是真实回合）
        msgs.append(Message("user", [
            {"type": "tool_result", "tool_use_id": f"c{i}", "content": _FILLER},
        ]))
        msgs.append(Message("assistant", f"完成{i}"))

    before = estimate_tokens(msgs, "sys")
    res = compress(msgs, "sys", budget=before // 3, keep_recent_turns=2)
    assert res.compressed is True
    # 保留段首条必须是真实用户回合
    assert _is_user_turn(res.messages[0])
    # 保留段里每个 tool_result 都能找到它前面的 tool_use（配对完整）
    open_ids: set[str] = set()
    for m in res.messages:
        if isinstance(m.content, list):
            for b in m.content:
                if b.get("type") == "tool_use":
                    open_ids.add(b["id"])
                elif b.get("type") == "tool_result":
                    assert b["tool_use_id"] in open_ids, "tool_result 缺失配对的 tool_use"


def test_single_huge_turn_not_truncated():
    """只有一个回合却超预算：无安全切点，原样返回（交给模型/上层处理）。"""
    msgs = [Message("user", _FILLER * 50), Message("assistant", "ok")]
    res = compress(msgs, "sys", budget=10, keep_recent_turns=6)
    assert res.compressed is False
    assert res.messages is msgs


# ---- FR-9.4b：旧回合大 tool_result 瘦身（优先于整回合丢弃） -----------------

def _tool_turn(i: int, big: str) -> list[Message]:
    """一个含超大 tool_result 的完整工具回合。"""
    return [
        Message("user", f"提问{i}"),
        Message("assistant", [
            {"type": "text", "text": f"读{i}"},
            {"type": "tool_use", "id": f"c{i}", "name": "read_file", "input": {"path": "x"}},
        ]),
        Message("user", [{"type": "tool_result", "tool_use_id": f"c{i}", "content": big}]),
        Message("assistant", f"完成{i}"),
    ]


def test_slim_old_tool_results_first():
    """超预算时先瘦身旧回合的大 tool_result；够了就不丢回合（dropped=0）。"""
    big = _FILLER * 10  # 每个 tool_result ~6000 字
    msgs: list[Message] = []
    for i in range(5):
        msgs += _tool_turn(i, big)
    before = estimate_tokens(msgs, "sys")
    # 预算设为"丢掉大头 tool_result 即可达标"的量级
    res = compress(msgs, "sys", budget=int(before * 0.55), keep_recent_turns=2)
    assert res.compressed is True
    assert res.slimmed > 0
    assert res.dropped == 0                       # 瘦身已达标，没丢回合
    assert len(res.messages) == len(msgs)          # 消息条数不变（只缩内容）
    # 旧回合的 tool_result 被截短并带标记；最近 keep_recent_turns 回合不动
    old_tr = res.messages[2].content[0]
    assert "已截短" in old_tr["content"]
    recent_tr = res.messages[-2].content[0]
    assert recent_tr["content"] == big             # 最近回合保持原样
    # 原列表未被改动（不可变性）
    assert msgs[2].content[0]["content"] == big
    # tool 配对完整
    assert _is_user_turn(res.messages[0])


def test_slim_not_enough_then_drop_turns():
    """瘦身不够时仍走整回合丢弃，且摘要照常生成。"""
    big = _FILLER * 10
    msgs: list[Message] = []
    for i in range(8):
        msgs += _tool_turn(i, big)
    before = estimate_tokens(msgs, "sys")
    res = compress(msgs, "sys", budget=before // 20, keep_recent_turns=2)
    assert res.compressed is True
    assert res.dropped > 0                         # 瘦身后仍超 → 丢了回合
    assert "此前对话摘要" in (res.system or "")
    assert _is_user_turn(res.messages[0])


def test_model_summary_injected_and_cut_passed():
    """FR-10.4a：注入 summarize 时用其产出替代启发式摘要，且拿到完整被丢段。"""
    msgs = _turns(10)
    before = estimate_tokens(msgs, "sys")
    seen = {}

    def fake_summarize(dropped):
        seen["n"] = len(dropped)
        return f"[模型摘要] 覆盖{len(dropped)}条"

    res = compress(msgs, "sys", budget=before // 4, keep_recent_turns=2,
                   summarize=fake_summarize)
    assert res.compressed and res.dropped == seen["n"] > 0
    assert "[模型摘要]" in res.system and "此前对话摘要（" not in res.system


def test_model_summary_failure_falls_back():
    """summarize 返回 None / 抛异常：回退启发式摘要，压缩本身不受影响。"""
    msgs = _turns(10)
    before = estimate_tokens(msgs, "sys")
    for bad in (lambda d: None, lambda d: (_ for _ in ()).throw(RuntimeError("x"))):
        res = compress(msgs, "sys", budget=before // 4, keep_recent_turns=2, summarize=bad)
        assert res.compressed and res.dropped > 0
        assert "此前对话摘要（" in res.system          # 启发式兜底


def test_build_summary_request_shapes():
    from agentcore.context import build_summary_request, build_transcript
    msgs = _turns(2)
    t = build_transcript(msgs)
    assert "第0轮提问" in t and t.count("用户:") == 2
    system, req = build_summary_request(msgs)
    assert "压缩" in system and len(req) == 1 and "需要并入摘要" in req[0].content
    system2, req2 = build_summary_request(msgs, prev_summary="旧摘要内容")
    assert "已有摘要" in req2[0].content and "旧摘要内容" in req2[0].content


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
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
