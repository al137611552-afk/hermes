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


def test_elicitation_text_names_the_command_and_the_way_out():
    """审批被拒必须**说得清、能照做**：静默拒绝的表现就是用户看到的
    "工作区被以只读方式挂载、写操作被拒"，查不到原因（2026-08-20 真机）。"""
    from agentcore.mcp_client.events import render_elicitation
    out = render_elicitation({"message": "Allow Codex to run ...?",
                              "codex_command": ["/bin/bash", "-lc", "printf 'hi' > c.txt"]})
    assert "printf 'hi' > c.txt" in out            # 到底要干什么
    assert "approval-policy" in out and "never" in out and "sandbox" in out   # 出路
    # 没有 codex_command 时退回 message，不能变成空白
    assert "Allow Codex" in render_elicitation({"message": "Allow Codex to run x?"})
    assert render_elicitation(None).startswith("⛔")


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


def test_stream_tap_forwards_everything_and_sniffs_events():
    """老 SDK（1.x）**连口子都没有**：不认识的通知 log 一句就丢，`message_handler` 也拿不到
    （mcp 1.27.2 实测确认）。所以在**流上**加旁路：只看不改、原样转交。

    这条测的是"转交"必须一条不落——旁路要是吞了消息，整个连接就废了。
    """
    import anyio

    from agentcore.config import MCPConfig
    from agentcore.mcp_client.manager import McpManager

    class _Root:
        def __init__(self, method, params):
            self.method, self.params = method, params

    class _Msg:
        def __init__(self, method, params=None):
            self.message = type("M", (), {"root": _Root(method, params)})()

    got = []
    m = McpManager(MCPConfig(enabled=True))
    m._ev_pending["codex"] = [lambda kind, text: got.append(text)]

    async def main():
        src_send, src_recv = anyio.create_memory_object_stream(16)
        out_send, out_recv = anyio.create_memory_object_stream(16)
        msgs = [
            _Msg("codex/event", {"_meta": {"requestId": 1},
                                 "msg": {"type": "agent_message_content_delta", "delta": "起"}}),
            _Msg("notifications/progress", {"progress": 1}),      # 别的通知照样转交
            _Msg("codex/event", {"_meta": {"requestId": 1},
                                 "msg": {"type": "task_complete"}}),
        ]
        for x in msgs:
            await src_send.send(x)
        await src_send.aclose()
        async with anyio.create_task_group() as tg:
            tg.start_soon(m._tap, "codex", src_recv, out_send)
            forwarded = []
            async for msg in out_recv:
                forwarded.append(msg)
            return forwarded

    forwarded = anyio.run(main)
    assert len(forwarded) == 3, "旁路必须一条不落地转交"
    assert got == ["起", "✓ 完成\n"], got


def test_raw_event_recovered_from_validation_error():
    """老 SDK 把不认识的通知**当校验失败**丢过来（用户真机见到的 `Field required`）。
    原始报文只能从 ValidationError 的 `input` 里回捞——捞不到就安静放弃。"""
    from agentcore.mcp_client.manager import _raw_event_params

    class _Exc(Exception):
        def __init__(self, errs):
            self._e = errs

        def errors(self):
            return self._e

    raw = {"method": "codex/event", "params": {"_meta": {"requestId": 3},
                                               "msg": {"type": "agent_message_content_delta",
                                                       "delta": "起"}}}
    got = _raw_event_params(_Exc([{"input": raw}]))
    assert got == raw["params"]
    # 不是我们要的通知 / 结构不对 / errors() 自己炸了——一律 None，别抛
    assert _raw_event_params(_Exc([{"input": {"method": "别的"}}])) is None
    assert _raw_event_params(_Exc([{"nope": 1}])) is None
    assert _raw_event_params(ValueError("没有 errors()")) is None


def test_sdk_capabilities_are_reported_for_diagnosis():
    """"没有边跑边出字"要能一眼分清是**配置问题还是环境问题**：
    老 SDK 两条通道都没有的话，接得再对也不会有输出。"""
    from agentcore.mcp_client.diag import sdk_capabilities
    caps = sdk_capabilities()
    assert set(caps) == {"version", "events", "message_handler"}
    assert isinstance(caps["events"], bool) and isinstance(caps["message_handler"], bool)


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
