"""块H3b：带图答案的多模态裁判（detect_offtarget_answer + loop 终局接线）自检。

用"假裁判"（注入 judge_fn）+ 假 provider/工具替真模型与真截图，纯逻辑、无网络、无模型、无 GUI。
`python tests/test_research_answer_judge.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.gate import PermissionGate  # noqa: E402
from agentcore.agent.loop import AgentLoop, detect_offtarget_answer  # noqa: E402
from agentcore.providers.base import Message, StreamEvent, ToolCall  # noqa: E402
from agentcore.tools import ToolRegistry  # noqa: E402
from agentcore.tools.base import Tool, ToolOutput  # noqa: E402

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
_OFF = '{"on_target": false, "off": ["配图为加厚秋冬款，不符夏季"], "suggestion": "据图改选冰丝短袖款"}'
_ON = '{"on_target": true, "off": []}'


# ============ detector 单元（detect_offtarget_answer）============
def test_offtarget_with_images_nudges():
    msg = detect_offtarget_answer("搜618夏季女士睡衣并附图", "为你推荐这几款真丝睡衣。",
                                  [dict(_IMG)], lambda p, i: _OFF)
    assert msg is not None and "配图与目标不符" in msg and "秋冬" in msg


def test_ontarget_silent():
    msg = detect_offtarget_answer("夏季睡衣", "都是冰丝短袖款。", [dict(_IMG)], lambda p, i: _ON)
    assert msg is None


def test_no_images_no_judge():
    # 无配图 → 无从看图，不触发（也不该空跑裁判）
    called = {"n": 0}
    def fake(p, i):
        called["n"] += 1
        return _OFF
    assert detect_offtarget_answer("夏季睡衣", "纯文字答案", [], fake) is None
    assert called["n"] == 0


def test_no_goal_or_no_answer_passes():
    assert detect_offtarget_answer("", "答案", [dict(_IMG)], lambda p, i: _OFF) is None
    assert detect_offtarget_answer("目标", "  ", [dict(_IMG)], lambda p, i: _OFF) is None


def test_judge_failure_passes():
    def boom(p, i):
        raise RuntimeError("视觉模型超时")
    assert detect_offtarget_answer("夏季睡衣", "答案", [dict(_IMG)], boom) is None


def test_images_passed_to_judge_and_capped():
    seen = {}
    def fake(p, i):
        seen["imgs"] = i
        return _ON
    imgs = [dict(_IMG, **{"id": k}) for k in range(8)]  # 8 张
    detect_offtarget_answer("g", "a", imgs, fake, max_images=6)
    assert len(seen["imgs"]) == 6 and seen["imgs"][0]["id"] == 2  # 只喂最近 6 张


# ============ 端到端 loop（终局多模态裁判 + 再放一轮）============
class _BrowserShot(Tool):
    name = "browser_take_screenshot"   # browser_* → 算"做过研究"，进 H3b 范围
    description = "fake"
    input_schema = {"type": "object", "properties": {}}
    dangerous = False

    def run(self, params):
        return ToolOutput(text="已截图", blocks=[dict(_IMG)])


class _Provider:
    """R1：浏览器截图；R2：纯文本答案（带图轮收尾）；R3：据提示重答。"""
    def __init__(self):
        self.round = 0
        self.texts = []

    def stream_chat(self, messages, system=None, tools=None):
        self.round += 1
        if self.round == 1:
            yield StreamEvent("tool_use", meta={"call": ToolCall("c1", "browser_take_screenshot", {})})
            yield StreamEvent("done", meta={"stop_reason": "tool_use"})
        elif self.round == 2:
            yield StreamEvent("text", "推荐这几款睡衣（见图）。")
            yield StreamEvent("done", meta={"stop_reason": "end_turn"})
        else:
            yield StreamEvent("text", "已据图重选：冰丝短袖夏季款。")
            yield StreamEvent("done", meta={"stop_reason": "end_turn"})


def _mk_loop(provider, judge_fn):
    reg = ToolRegistry([_BrowserShot(Path("."))])
    gate = PermissionGate(lambda req: None)
    return AgentLoop(provider, reg, gate, max_steps=8,
                     research_refine=True, research_judge=judge_fn)


def test_loop_offtarget_answer_triggers_extra_round():
    judged = {"n": 0}
    def judge(p, i):
        judged["n"] += 1
        return _OFF
    prov = _Provider()
    events = []
    msgs = _mk_loop(prov, judge).run(
        [Message("user", "搜618夏季女士睡衣并附图")], None, lambda e, d: events.append((e, d)))
    # 收到带图答案后判不对题 → 注入提示并再放一轮（provider 跑到第 3 轮）
    assert prov.round == 3
    assert any(e == "research_hint" and "配图与目标不符" in d["text"] for e, d in events)
    # 每轮只判一次（answer_refined 封顶，不无限重判）
    assert judged["n"] == 1
    # 最终落库的最后一条 assistant = 重答
    last_assistant = [m for m in msgs if m.role == "assistant" and isinstance(m.content, str)][-1]
    assert "冰丝短袖" in last_assistant.content


def test_loop_ontarget_answer_no_extra_round():
    prov = _Provider()
    events = []
    _mk_loop(prov, lambda p, i: _ON).run(
        [Message("user", "搜夏季睡衣并附图")], None, lambda e, d: events.append((e, d)))
    assert prov.round == 2  # 对题 → 不重答
    assert not any(e == "research_hint" for e, d in events)


def test_loop_no_judge_inert():
    # research_judge=None（H3b 关）→ 带图也不判，存量行为零变化
    prov = _Provider()
    reg = ToolRegistry([_BrowserShot(Path("."))])
    loop = AgentLoop(prov, reg, PermissionGate(lambda req: None), max_steps=8,
                     research_refine=True, research_judge=None)
    loop.run([Message("user", "搜夏季睡衣并附图")], None, lambda e, d: None)
    assert prov.round == 2


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    fns.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
