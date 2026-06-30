"""P5 截屏工具自测：工具开关 / 图片并列块注入 / 权限 gate / 优雅降级（无 GUI、无网络）。

运行：python tests/test_p5_screenshot.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.gate import DENY, PermissionGate  # noqa: E402
from agentcore.agent.loop import AgentLoop  # noqa: E402
from agentcore.providers.base import Message, StreamEvent, ToolCall  # noqa: E402
from agentcore.tools import ToolRegistry, build_registry  # noqa: E402
from agentcore.tools.base import Tool, ToolOutput  # noqa: E402
from agentcore.tools.screenshot import ScreenshotTool  # noqa: E402

_IMG_BLOCK = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}


class _FakeShot(Tool):
    name = "take_screenshot"
    description = "fake"
    input_schema = {"type": "object", "properties": {}}
    dangerous = False

    def run(self, params):
        return ToolOutput(text="已截屏", blocks=[dict(_IMG_BLOCK)])


class _DangerShot(_FakeShot):
    dangerous = True


class _ShotProvider:
    """第一轮调用 take_screenshot，第二轮纯文本结束。"""
    def __init__(self):
        self.round = 0

    def stream_chat(self, messages, system=None, tools=None):
        self.round += 1
        if self.round == 1:
            yield StreamEvent("tool_use", meta={"call": ToolCall("c1", "take_screenshot", {})})
            yield StreamEvent("done", meta={"stop_reason": "tool_use"})
        else:
            yield StreamEvent("text", "我看到屏幕了。")
            yield StreamEvent("done", meta={"stop_reason": "end_turn"})


def _tool_turn_msg(msgs):
    """返回回灌 tool_result 的那条 user 消息（content 为 list）。"""
    for m in msgs:
        if m.role == "user" and isinstance(m.content, list):
            if any(b.get("type") == "tool_result" for b in m.content):
                return m
    return None


def test_registry_screenshot_toggle(tmp: Path):
    on = build_registry(tmp, shell="bash", screenshot=True)
    assert "take_screenshot" in on.names()
    assert on.is_dangerous("take_screenshot")  # 截屏属危险操作，过权限 gate
    off = build_registry(tmp, shell="bash", screenshot=False)
    assert "take_screenshot" not in off.names()  # kill-switch 生效


def test_image_injected_as_sibling(tmp: Path):
    """截屏图片必须作为并列块进 tool_result 所在的同一条 user 消息（变体1）。"""
    reg = ToolRegistry([_FakeShot(tmp)])
    gate = PermissionGate(lambda req: None)
    loop = AgentLoop(_ShotProvider(), reg, gate, max_steps=5)

    events = []
    msgs = loop.run([Message("user", "看屏")], None, lambda e, d: events.append((e, d)))

    turn = _tool_turn_msg(msgs)
    assert turn is not None
    types = [b.get("type") for b in turn.content]
    assert "tool_result" in types and "image" in types  # 二者并列同消息
    # tool_result 事件带缩略图 data url
    tr = next(d for e, d in events if e == "tool_result")
    assert tr["image"].startswith("data:image/png;base64,")


def test_gate_allow_then_image(tmp: Path):
    """危险截屏工具：用户授权后才注入图片。"""
    reg = ToolRegistry([_DangerShot(tmp)])
    gate = PermissionGate(lambda req: None)
    threading.Thread(target=lambda: (time.sleep(0.05), gate.resolve(1, "allow"))).start()
    loop = AgentLoop(_ShotProvider(), reg, gate, max_steps=5)
    events = []
    msgs = loop.run([Message("user", "看屏")], None, lambda e, d: events.append((e, d)))
    turn = _tool_turn_msg(msgs)
    assert any(b.get("type") == "image" for b in turn.content)
    tr = next(d for e, d in events if e == "tool_result")
    assert tr["ok"] and "image" in tr


def test_gate_denied_no_image(tmp: Path):
    """被拒绝 -> 不截屏、无图片块、结果标记失败。"""
    reg = ToolRegistry([_DangerShot(tmp)])
    gate = PermissionGate(lambda req: None)
    threading.Thread(target=lambda: (time.sleep(0.05), gate.resolve(1, DENY))).start()
    loop = AgentLoop(_ShotProvider(), reg, gate, max_steps=5)
    events = []
    msgs = loop.run([Message("user", "看屏")], None, lambda e, d: events.append((e, d)))
    turn = _tool_turn_msg(msgs)
    assert not any(b.get("type") == "image" for b in turn.content)  # 无图片
    tr = next(d for e, d in events if e == "tool_result")
    assert not tr["ok"] and "image" not in tr


def test_screenshot_tool_graceful(tmp: Path):
    """真实 ScreenshotTool：有显示则返回 ToolOutput+image；无显示(开发机)优雅抛 ToolError。"""
    from agentcore.tools.base import ToolError
    try:
        out = ScreenshotTool(tmp).run({})
    except ToolError:
        return  # headless 环境：优雅降级，符合预期
    assert isinstance(out, ToolOutput)
    assert out.blocks and out.blocks[0]["type"] == "image"


def _run_all():
    import inspect
    import tempfile
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
