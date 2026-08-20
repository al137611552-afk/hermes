"""agent 型 MCP server 的自定义事件渲染与归属（纯逻辑，无网络、无 SDK）。

**语料全部来自真机与探针抓到的原始载荷**（2026-08-20），不是照文档猜的形状：
Codex 一条标准 MCP 通知都不发，过程全在 `codex/event` 里；不接就是黑箱，
接错字段就是刷屏。

运行：python tests/test_mcp_events.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.mcp_client.events import (  # noqa: E402
    event_request_id, render_event,
)


def test_agent_delta_is_the_live_text_stream():
    """逐字增量＝"看着它写"那条流：**原样拼接、不加换行**，否则每个字一行。"""
    assert render_event({"type": "agent_message_content_delta", "delta": "起"}) == "起"
    assert render_event({"type": "agent_message_delta", "delta": "承"}) == "承"
    joined = "".join(render_event({"type": "agent_message_content_delta", "delta": d})
                     for d in ("先", "读", "代码"))
    assert joined == "先读代码"


def test_exec_command_shows_only_the_informative_tail():
    """PowerShell 那种 ["powershell.exe","-Command","真正的命令"]，只有最后一段有信息量。"""
    out = render_event({"type": "exec_command_begin",
                        "command": ["C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe",
                                    "-Command", "Get-Content -Raw README.md"]})
    assert out == "$ Get-Content -Raw README.md\n"
    assert render_event({"type": "exec_command_end", "exit_code": 1}) == "  ↳ 退出码 1\n"


def test_approval_request_says_hermes_cannot_answer():
    """真机踩到的死等：Codex 在等审批，hermes 接不了这条通道 → 不说出来就是干等到超时。
    这条文案必须给出**能照做的出路**。"""
    out = render_event({"type": "exec_approval_request",
                        "command": ["powershell.exe", "-Command", "Get-Content README.md"]})
    assert "请求批准" in out and "approval-policy" in out and "never" in out


def test_noise_is_muted():
    """回显我们自己的提示词、原始协议帧、token 计数——显示了只会盖住有用信息。"""
    for t in ("raw_response_item", "user_message", "session_configured",
              "mcp_startup_complete", "token_count"):
        assert render_event({"type": t, "message": "x"}) == "", t
    # item_* 里回显 UserMessage 同样是噪声（真机载荷：item.type == "UserMessage"）
    assert render_event({"type": "item_started",
                         "item": {"type": "UserMessage", "content": []}}) == ""
    assert render_event({"type": "item_started", "item": {"type": "CommandExecution"}}) \
        == "▸ CommandExecution\n"


def test_errors_and_warnings_surface():
    real = ("Falling back from WebSockets to HTTPS transport. unexpected status 401 "
            "Unauthorized: Missing bearer or basic authentication in header")
    assert render_event({"type": "warning", "message": real}).startswith("⚠ ")
    assert render_event({"type": "error", "message": "boom"}) == "⚠ boom\n"


def test_unknown_and_junk_never_crash_or_spam():
    """上游随时会加新事件类型：**不认识就不显示**，绝不刷屏、绝不抛。"""
    assert render_event({"type": "some_future_event", "x": 1}) == ""
    assert render_event({}) == ""
    assert render_event(None) == ""
    assert render_event("字符串") == ""


def test_request_id_is_the_attribution_key():
    """归属靠 `_meta.requestId`——子 Agent 可能并发调同一个 server，靠"当前那次"猜必然串台。"""
    assert event_request_id({"_meta": {"requestId": 4, "threadId": "T"}, "msg": {}}) == 4
    assert event_request_id({"meta": {"requestId": 7}}) == 7
    assert event_request_id({"_meta": {"threadId": "T"}}) is None
    assert event_request_id({}) is None
    assert event_request_id(None) is None


def test_dispatch_binds_by_request_id_and_refuses_to_guess():
    """归属规则：首条事件到达时若**只有一次调用未绑定**才认领；并发同时起跑时宁可不显示。

    显示错地方比不显示更糟——那会让人以为 A 调用干了 B 的事。
    """
    from agentcore.config import MCPConfig
    from agentcore.mcp_client.manager import McpManager

    m = McpManager(MCPConfig(enabled=True))
    got_a, got_b = [], []
    cb_a = lambda k, t: got_a.append(t)      # noqa: E731
    cb_b = lambda k, t: got_b.append(t)      # noqa: E731

    m._ev_pending["codex"] = [cb_a]
    m._dispatch_event("codex", 3, "第一段")          # 唯一在飞 → 认领并绑定
    m._dispatch_event("codex", 3, "第二段")          # 已绑定 → 继续给它
    assert got_a == ["第一段", "第二段"]

    m._ev_pending["codex"] = [cb_a, cb_b]            # 两次在飞、都没绑定
    m._dispatch_event("codex", 9, "认不出是谁的")
    assert got_a == ["第一段", "第二段"] and got_b == []

    m._dispatch_event("codex", None, "没有 requestId")  # 认不出 → 丢弃
    assert got_b == []


def test_dispatch_never_breaks_the_call():
    """推流回调自己炸了，也不能影响工具调用——过程展示是增值项。"""
    from agentcore.config import MCPConfig
    from agentcore.mcp_client.manager import McpManager

    def boom(kind, text):
        raise RuntimeError("前端没了")

    m = McpManager(MCPConfig(enabled=True))
    m._ev_pending["codex"] = [boom]
    m._dispatch_event("codex", 1, "x")       # 不抛即通过


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
