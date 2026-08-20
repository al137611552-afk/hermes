"""agent 型 MCP server 的**自定义通知**渲染（纯逻辑，无 IO、无 SDK 依赖）。

标准 MCP 只有 `notifications/progress` 与 `notifications/message`，而 Codex 一条标准通知
**都不发**（2026-08-20 探针实测：全程只有 `codex/event`）。所以要拿到过程，只能接它的
自定义通知——SDK 的 `NotificationBinding` 就是干这个的，不接则 pydantic 校验失败、
消息连进都进不来（用户真机见到的 `Field required [type=missing]` 就是这个）。

本模块只做「一条事件 → 给人看的一段文本」，形状取自**真机与探针抓到的原始载荷**，
不是照文档猜的。不认识的事件一律返回空串——**宁可不显示，也不要刷屏**。
"""
from __future__ import annotations

CODEX_EVENT = "codex/event"

# 逐字输出：这才是"看着它写"的那条流，不加换行、原样拼接
_DELTA_TYPES = ("agent_message_content_delta", "agent_message_delta", "agent_reasoning_delta")
# 纯噪声：回显我们自己发的提示、原始协议帧、token 计数……显示了只会盖住有用信息
_MUTED_TYPES = ("raw_response_item", "user_message", "session_configured",
                "mcp_startup_complete", "token_count", "turn_diff")


def _cmd_text(msg: dict) -> str:
    cmd = msg.get("command")
    if isinstance(cmd, list):
        # PowerShell 那种 ["powershell.exe","-Command","..."] 只看最后一段才有信息量
        return str(cmd[-1] if cmd else "")
    return str(cmd or "")


def render_event(msg) -> str:
    """一条 `codex/event` 的 `msg` → 要追加到工具块的文本。空串＝不显示。

    返回值**已含所需换行**：逐字增量不带换行（要拼成连续文本），状态行自带换行。
    """
    if not isinstance(msg, dict):
        return ""
    t = str(msg.get("type") or "")
    if t in _DELTA_TYPES:
        return str(msg.get("delta") or "")
    if t in _MUTED_TYPES:
        return ""
    if t == "task_started":
        return "▸ 开始\n"
    if t == "exec_command_begin":
        return f"$ {_cmd_text(msg)[:200]}\n"
    if t == "exec_command_end":
        code = msg.get("exit_code")
        return f"  ↳ 退出码 {code}\n" if code is not None else ""
    if t == "exec_approval_request":
        # **这条最要紧**：Codex 在等审批，而 hermes 还接不了这个通道——不说出来就是干等到超时
        return (f"⏳ Codex 请求批准执行：{_cmd_text(msg)[:160]}\n"
                "   （hermes 暂不能代答；调用时给 approval-policy=\"never\" 可避免卡住）\n")
    if t == "apply_patch_approval_request":
        return ("⏳ Codex 请求批准改文件\n"
                "   （hermes 暂不能代答；调用时给 approval-policy=\"never\" 可避免卡住）\n")
    if t in ("error", "stream_error"):
        return f"⚠ {str(msg.get('message') or '')[:300]}\n"
    if t == "warning":
        return f"⚠ {str(msg.get('message') or '')[:300]}\n"
    if t in ("item_started", "item_completed"):
        item = msg.get("item") if isinstance(msg.get("item"), dict) else {}
        kind = str(item.get("type") or "")
        if not kind or kind == "UserMessage":     # 回显我们自己的提示词，没信息量
            return ""
        return f"{'▸' if t == 'item_started' else '✓'} {kind}\n"
    if t == "task_complete":
        return "✓ 完成\n"
    return ""


def event_request_id(params) -> "int | None":
    """事件属于哪一次调用（`_meta.requestId`）。取不到返回 None。

    归属靠它而不是"当前那次"：子 Agent 可能并发调同一个 server，靠猜必然串台。
    """
    meta = None
    if isinstance(params, dict):
        meta = params.get("_meta") or params.get("meta")
    else:
        meta = getattr(params, "meta", None) or getattr(params, "_meta", None)
    if isinstance(meta, dict):
        rid = meta.get("requestId")
        if isinstance(rid, int):
            return rid
    return None
