"""上下文 token 预算与压缩（P6.2 / FR-6.2）。

随着会话变长（尤其 P6.1 持久化后跨重启累积的长历史），整段 history 直接喂给
模型会撑爆上下文窗口、也抬高成本。本模块在「喂给模型之前」按 token 预算裁剪：
保留最近若干个对话回合，把更早的内容压成一段摘要塞进 system。

设计约束：
- **无新依赖**：token 用启发式估算（非精确分词），离线、provider 无关。够用来
  做预算决策；宁可略高估也不低估，避免裁剪后仍超限。
- **不破坏 tool-use 配对**：assistant 的 tool_use 块必须与其后 user 的 tool_result
  成对出现，否则 Anthropic API 报错。因此裁剪点只落在「真实用户回合」的边界上
  （一条 role==user 且不含 tool_result 的消息），绝不从一次工具往返中间切断。
- **不改持久化**：DB 里始终是完整历史；压缩只作用于本次喂给模型的副本。
"""
from __future__ import annotations

from dataclasses import dataclass

from .providers import Message

# 单张图片的粗略 token 估值（Anthropic 中等尺寸图量级）；图不参与文本估算。
_IMAGE_TOKENS = 1600
# 每条消息的固定结构开销（role 包裹等）。
_MSG_OVERHEAD = 4
# 摘要里每条被省略消息保留的文本字数上限。
_SUMMARY_CHARS_PER_MSG = 200
# 瘦身（FR-9.4b）：旧回合里超过该字数的 tool_result 截短保留头部
_SLIM_TOOL_RESULT_CHARS = 600


@dataclass
class CompressResult:
    messages: list[Message]
    system: str | None
    compressed: bool
    dropped: int          # 被省略的消息条数
    before_tokens: int
    after_tokens: int
    budget: int
    slimmed: int = 0      # 被瘦身（截短）的旧 tool_result 块数（FR-9.4b）


def estimate_tokens_text(s: str) -> int:
    """启发式：ASCII 约 4 字符/token，非 ASCII（如中文）约 1 字符/token。

    略偏高估，作为预算决策足够；不追求与某家分词器精确一致。
    """
    if not s:
        return 0
    ascii_n = sum(1 for c in s if ord(c) < 128)
    other = len(s) - ascii_n
    return ascii_n // 4 + other + 1


def _block_tokens(block: dict) -> int:
    t = block.get("type")
    if t == "text":
        return estimate_tokens_text(block.get("text", ""))
    if t == "image":
        return _IMAGE_TOKENS
    if t == "document":
        # 归一后的文档块文本在 source/text 或 text 字段；尽量取到文本
        return estimate_tokens_text(block.get("text", "") or str(block.get("source", "")))
    if t == "tool_use":
        return estimate_tokens_text(block.get("name", "")) + estimate_tokens_text(str(block.get("input", "")))
    if t == "tool_result":
        c = block.get("content", "")
        return estimate_tokens_text(c if isinstance(c, str) else str(c))
    # 未知块：按其字符串长度兜底估算
    return estimate_tokens_text(str(block))


def estimate_message_tokens(msg: Message) -> int:
    content = msg.content
    if isinstance(content, str):
        return estimate_tokens_text(content) + _MSG_OVERHEAD
    if isinstance(content, list):
        return sum(_block_tokens(b) for b in content if isinstance(b, dict)) + _MSG_OVERHEAD
    return estimate_tokens_text(str(content)) + _MSG_OVERHEAD


def estimate_tokens(messages: list[Message], system: str | None = None) -> int:
    total = estimate_tokens_text(system or "")
    for m in messages:
        total += estimate_message_tokens(m)
    return total


def _is_user_turn(msg: Message) -> bool:
    """是否为「真实用户回合」的起点（可作为安全裁剪边界）。

    tool_result 回灌消息也是 role==user，但 content 是 tool_result 块列表，
    它属于上一条 assistant 工具往返的一部分，不能作为切点。
    """
    if msg.role != "user":
        return False
    content = msg.content
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return True


def _turn_starts(messages: list[Message]) -> list[int]:
    return [i for i, m in enumerate(messages) if _is_user_turn(m)]


def _block_summary(block: dict) -> str:
    t = block.get("type")
    if t == "text":
        return block.get("text", "")
    if t == "image":
        return "[图片]"
    if t == "document":
        return "[文档]"
    if t == "tool_use":
        return f"[调用工具 {block.get('name', '?')}]"
    if t == "tool_result":
        c = block.get("content", "")
        return f"[工具结果] {(c if isinstance(c, str) else str(c))[:_SUMMARY_CHARS_PER_MSG]}"
    return ""


def _msg_summary(msg: Message, cap: int = _SUMMARY_CHARS_PER_MSG) -> str:
    content = msg.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(s for s in (_block_summary(b) for b in content if isinstance(b, dict)) if s)
    else:
        text = str(content)
    text = text.strip().replace("\n", " ")
    if len(text) > cap:
        text = text[:cap] + "…"
    role = "用户" if msg.role == "user" else "助手"
    return f"{role}: {text}" if text else ""


def _summarize(dropped: list[Message]) -> str:
    lines = [s for s in (_msg_summary(m) for m in dropped) if s]
    body = "\n".join(lines)
    return f"[此前对话摘要（{len(dropped)} 条较早消息因上下文预算已省略）]\n{body}"


# ---- 模型生成压缩摘要（FR-10.4a，对标 /compact）------------------------------

# 给摘要模型的转录：每条上限放宽（信息更全）、总量设上限（摘要调用本身别爆）
_TRANSCRIPT_CHARS_PER_MSG = 800
_TRANSCRIPT_MAX_CHARS = 24_000

_SUMMARY_SYSTEM = (
    "你是对话压缩器。把给出的早期对话记录压成一段忠实、信息密集的摘要，"
    "供同一个助手在后续对话中当作记忆使用。要求：保留用户的目标与明确要求、"
    "已完成的工作（涉及的文件/关键决定/结论）、未完成事项与重要约束；"
    "不要寒暄、不要点评；第三人称陈述；500 字以内。只输出摘要正文。"
)


def build_transcript(msgs: list[Message], max_chars: int = _TRANSCRIPT_MAX_CHARS) -> str:
    """把被丢弃的消息渲染成给摘要模型看的转录（每条/总量有上限）。"""
    lines: list[str] = []
    total = 0
    for m in msgs:
        s = _msg_summary(m, cap=_TRANSCRIPT_CHARS_PER_MSG)
        if not s:
            continue
        if total + len(s) > max_chars:
            lines.append(f"…（其余 {len(msgs)} 条中靠后的部分因长度上限省略）")
            break
        lines.append(s)
        total += len(s)
    return "\n".join(lines)


def build_summary_request(
    dropped: list[Message], prev_summary: str | None = None
) -> tuple[str, list[Message]]:
    """构造摘要调用的 (system, messages)。prev_summary 给了则做增量合并。"""
    parts: list[str] = []
    if prev_summary:
        parts.append(f"[已有摘要（覆盖更早的对话）]\n{prev_summary}")
    parts.append(f"[需要并入摘要的对话记录]\n{build_transcript(dropped)}")
    parts.append("请输出合并后的完整摘要。" if prev_summary else "请输出摘要。")
    return _SUMMARY_SYSTEM, [Message("user", "\n\n".join(parts))]


def _read_sources(messages: list[Message]) -> dict[str, str]:
    """建 tool_use_id -> read_file 路径 的映射（FR-11.3b 可重读引用）。

    assistant 的 tool_use(name=read_file) 的 id，对应下一条 user 里同 id 的 tool_result。
    截短该结果时据此标注"可用 read_file 重读 <路径>"。
    """
    out: dict[str, str] = {}
    for m in messages:
        if m.role != "assistant" or not isinstance(m.content, list):
            continue
        for b in m.content:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "read_file"):
                path = (b.get("input") or {}).get("path")
                if b.get("id") and path:
                    out[b["id"]] = path
    return out


def _slim_old_tool_results(
    messages: list[Message], keep_from: int, max_chars: int = _SLIM_TOOL_RESULT_CHARS
) -> tuple[list[Message], int]:
    """瘦身（FR-9.4b）：把 keep_from 之前旧回合里超长的 tool_result 文本截短保留头部。

    不改原消息对象（仅复制受影响的消息/块）；保持 tool_use/tool_result 配对结构不变，
    只缩内容——比整回合丢弃更细粒度、少丢信息。来自 read_file 的结果在截短标记里标注来源
    文件与"可重读"（FR-11.3b），模型需要细节时能精准重取。返回 (新消息列表, 截短块数)。
    """
    sources = _read_sources(messages)
    out: list[Message] = []
    slimmed = 0
    for i, m in enumerate(messages):
        if i >= keep_from or m.role != "user" or not isinstance(m.content, list):
            out.append(m)
            continue
        new_blocks = None
        for j, b in enumerate(m.content):
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            c = b.get("content", "")
            if not (isinstance(c, str) and len(c) > max_chars):
                continue
            if new_blocks is None:
                new_blocks = list(m.content)
            src = sources.get(b.get("tool_use_id"))
            hint = (f"，可用 read_file 重读 {src}" if src else "")
            new_blocks[j] = {
                **b,
                "content": c[:max_chars] + f"\n…[工具结果过长，已截短（原 {len(c)} 字符）{hint}]",
            }
            slimmed += 1
        out.append(Message(m.role, new_blocks) if new_blocks is not None else m)
    return out, slimmed


def compress(
    messages: list[Message],
    system: str | None,
    *,
    budget: int,
    keep_recent_turns: int = 6,
    summarize=None,
) -> CompressResult:
    """按 token 预算裁剪喂给模型的消息。

    - 不超预算：原样返回（compressed=False）。
    - 超预算：先**瘦身**最近 keep_recent_turns 个回合之前的超长 tool_result（截短保留头部，
      FR-9.4b）；瘦身后已达标则到此为止。
    - 仍超预算：从最早的回合开始整段丢弃，直到剩余 <= budget；但至少保留最近
      keep_recent_turns 个回合。被丢弃内容压成摘要追加到 system。
    - 即使保留最近 keep_recent_turns 回合仍超预算：尽力裁到该边界（不再继续切，
      以免破坏 tool 配对或丢光上下文），compressed=True 但 after 可能仍 > budget。

    summarize（FR-10.4a）：可选 `(dropped)->str|None`，产出更高质量的摘要块文本
    （如模型生成）；返回 None / 抛异常则回退内置启发式截断。
    """
    before = estimate_tokens(messages, system)
    if before <= budget:
        return CompressResult(messages, system, False, 0, before, before, budget)

    # 先瘦身旧回合的大 tool_result（不破坏配对、少丢信息）
    starts0 = _turn_starts(messages)
    slimmed = 0
    if len(starts0) > 1:
        max_keep0 = max(1, keep_recent_turns)
        keep_from = starts0[-max_keep0] if len(starts0) >= max_keep0 else starts0[0]
        messages, slimmed = _slim_old_tool_results(messages, keep_from)
        if slimmed:
            after_slim = estimate_tokens(messages, system)
            if after_slim <= budget:
                return CompressResult(
                    messages, system, True, 0, before, after_slim, budget, slimmed=slimmed
                )

    starts = _turn_starts(messages)
    if len(starts) <= 1:
        # 没有可切的回合边界（如单个超大回合）：无法再裁剪（瘦身若发生则保留瘦身结果）。
        after = estimate_tokens(messages, system)
        return CompressResult(
            messages, system, slimmed > 0, 0, before, after, budget, slimmed=slimmed
        )

    # 最早允许的「保留窗口」起点：保证至少留 keep_recent_turns 个回合。
    max_keep = max(1, keep_recent_turns)
    floor_cut = starts[-max_keep] if len(starts) >= max_keep else starts[0]

    # 在 [starts[0] .. floor_cut] 中选最小的切点，使保留部分 <= budget（保留最多上下文）。
    cut = floor_cut
    for s in starts:
        if s > floor_cut:
            break
        if estimate_tokens(messages[s:], system) <= budget:
            cut = s
            break

    if cut <= starts[0]:
        # 连第一个候选都没省下东西：不再裁剪（瘦身若发生则保留瘦身结果）。
        after = estimate_tokens(messages, system)
        return CompressResult(
            messages, system, slimmed > 0, 0, before, after, budget, slimmed=slimmed
        )

    dropped = messages[:cut]
    kept = messages[cut:]
    summary = None
    if summarize is not None:
        try:
            summary = summarize(dropped)
        except Exception:  # noqa: BLE001 — 摘要升级失败绝不挡压缩本身
            summary = None
    if not summary:
        summary = _summarize(dropped)
    new_system = f"{system}\n\n{summary}" if system else summary
    after = estimate_tokens(kept, new_system)
    return CompressResult(
        kept, new_system, True, len(dropped), before, after, budget, slimmed=slimmed
    )
