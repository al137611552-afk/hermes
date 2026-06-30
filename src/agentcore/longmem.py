"""长期记忆的纯逻辑（P6.3 / FR-6.3）：无副作用、可单测。

存储在 store/memory.py（MemoryStore）；这里只放可单测的纯函数：
- build_memory_block：把记忆列表拼成注入 system 的文本（带条数/字符预算）。
- build_transcript：把一段对话展平成纯文本，供抽取模型阅读。
- build_extract_request：构造抽取用的 (system, messages)。
- parse_memories：从模型输出里解析出记忆条目（容忍代码围栏 / 多余文本）。
"""
from __future__ import annotations

import json
import re

from .providers import Message
from .store.memory import normalize_kind

_KIND_LABEL = {"user": "用户", "preference": "偏好", "skill": "能力", "fact": "事实", "principle": "原则"}
_MAX_TRANSCRIPT_CHARS = 12000


# ---- 注入 system 的记忆块 ------------------------------------------------

def build_memory_block(
    memories: list[dict], *, max_items: int = 30, max_chars: int = 2000
) -> str:
    """把记忆拼成注入 system 的文本块；超条数 / 字符预算则截断。空则返回 ""。"""
    lines: list[str] = []
    used = 0
    for m in memories[:max_items]:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        label = _KIND_LABEL.get(normalize_kind(m.get("kind")), "事实")
        line = f"- [{label}] {content}"
        if lines and used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return (
        "[长期记忆] 以下是你在过往会话里记下的、关于用户与项目的持久事实，"
        "回答时应纳入考虑（若与本次对话明显冲突，以本次为准）：\n" + "\n".join(lines)
    )


# ---- 记忆固化：碎片 -> 框架原则（类人记忆，离线归纳）----------------------

def build_consolidate_request(fragments: list[str], existing: list[str]):
    """构造'固化'请求：把零散碎片提炼成框架性原则（参考已有原则去重）。返回 (system, [Message])。"""
    frag_text = "\n".join(f"- {f}" for f in fragments if f and f.strip())
    prin_text = "\n".join(f"- {p}" for p in existing if p and p.strip()) or "（暂无）"
    system = (
        "你在做长期记忆的『固化』：先把零散经验碎片**按主题归类**（主题由你根据内容自拟，如"
        "『记忆系统』『自主模式』『权限安全』等），再把每个主题的碎片提炼成一条框架性、规律性的原则——"
        "每条一句、高度概括，是能据此回忆起该主题一类细节的『框架』，不要复述碎片。"
    )
    user = (
        f"【已有原则】（避免重复，可合并优化）：\n{prin_text}\n\n"
        f"【新经验碎片】：\n{frag_text}\n\n"
        '请**按主题**输出框架原则（一个主题一条，通常 3~8 条）。**只输出一个 JSON 对象** {"memories": [...]}，'
        '其中每项形如 {"content": "【主题】原则一句话", "kind": "principle"}——content 开头用【】标注所属主题。'
    )
    return system, [Message("user", user)]


# ---- 对话转录（供抽取模型阅读） ------------------------------------------

def _block_text(b: dict) -> str:
    t = b.get("type")
    if t == "text":
        return b.get("text", "")
    if t == "image":
        return "[图片]"
    if t == "document":
        return b.get("text", "") or "[文档]"
    if t == "tool_use":
        args = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
        return f"[调用工具 {b.get('name', '?')} {args}]"
    if t == "tool_result":
        c = b.get("content", "")
        return f"[工具结果 {(c if isinstance(c, str) else str(c))[:200]}]"
    return ""


def _msg_text(msg: Message) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(s for s in (_block_text(b) for b in c if isinstance(b, dict)) if s)
    return str(c)


def build_transcript(messages: list[Message], *, max_chars: int = _MAX_TRANSCRIPT_CHARS) -> str:
    parts: list[str] = []
    for m in messages:
        text = _msg_text(m).strip()
        if not text:
            continue
        role = "用户" if m.role == "user" else "助手"
        parts.append(f"{role}: {text}")
    transcript = "\n".join(parts)
    if len(transcript) > max_chars:  # 保留尾部（更近的对话）
        transcript = "…\n" + transcript[-max_chars:]
    return transcript


# ---- 抽取请求 + 解析 -----------------------------------------------------

_EXTRACT_SYSTEM = (
    "你是一个为「跨项目长期记忆」做信息抽取的助手。只提取**跨项目通用、换个项目也仍然有用**"
    "的事实，例如：用户的身份/称呼、长期偏好与工作习惯、反复强调的要求、用户的技能/能力倾向。\n"
    "**绝不要记录任何项目专属内容**（某个项目做了什么、其目标/架构/技术栈/目录/决定/约定等）"
    "——那些属于该项目自己的 hermes.md，记进全局记忆会造成跨项目互相干扰。\n"
    "也忽略一次性、寒暄、过程细节。每条写成一句话，具体、自包含（不要用「它/这个」等指代）。\n"
    "下面会给出「已有记忆」，不要重复或仅改写已有内容。\n"
    "只输出 JSON，格式：{\"memories\":[{\"content\":\"...\",\"kind\":\"user|preference|skill|fact\"}]}。"
    "没有值得记的（包括只聊了某个具体项目时）就输出 {\"memories\":[]}。不要输出 JSON 以外的任何文字。"
)


def build_extract_request(transcript: str, existing: list[str]) -> tuple[str, list[Message]]:
    existing_block = "\n".join(f"- {e}" for e in existing[:100]) or "（暂无）"
    user = (
        f"已有记忆：\n{existing_block}\n\n"
        f"待抽取的对话：\n{transcript}\n\n"
        "请按要求输出 JSON。"
    )
    return _EXTRACT_SYSTEM, [Message("user", user)]


def parse_memories(text: str) -> list[dict]:
    """从模型输出解析记忆条目；容忍代码围栏与前后多余文本。失败返回 []。

    返回 [{"content": str, "kind": str}]，kind 已归一到合法值。
    """
    if not text:
        return []
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    obj = _loads(s)
    if obj is None:  # 退而求其次：截取第一个 { 到最后一个 }
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            obj = _loads(s[i : j + 1])
    if not isinstance(obj, dict):
        return []
    items = obj.get("memories")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if isinstance(it, str):
            content, kind = it, "fact"
        elif isinstance(it, dict):
            content, kind = it.get("content", ""), it.get("kind", "fact")
        else:
            continue
        content = (content or "").strip()
        if content:
            out.append({"content": content, "kind": normalize_kind(kind)})
    return out


def _loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None
