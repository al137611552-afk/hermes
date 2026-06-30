"""块H3：搜索/调研结果的**模型裁判**（语义 + 多模态相关性），见 ADR 0018。

H1 的正则只判可证伪的硬约束（预算）。语义（"夏季"≠厚秋冬款）、来源权威性、时效、
以及"配图对不对题"这类，正则判不了——交模型裁判。裁判是决策层的一道**质量闸**：
它会发模型调用（有 IO），故**不是**纯逻辑 Evaluator，单独住这里。

设计：
- provider 经 `judge_fn(prompt, images) -> str` **注入**——便于单测用"假裁判"，也便于换便宜模型。
- **多模态**：`images` 非空时连图一起喂裁判（Hermes 默认模型原生视觉），这才能抓"配图是冬季"。
- **通用**：prompt 用"用户目标"参数化——购物判品类/季节/价/图，资料查询判相关/权威/时效，
  竞品调研判覆盖/对标/遗漏。同一道闸，换判据。
- **纪律**（同块E/H2）：裁判也会判错 → **喂事实不硬拦**（产 issues→提示重搜，不删结果）；
  裁判故障/解析失败 → 一律按"对题"放行（**不拦**），绝不因裁判出错而误触发重搜或卡死。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass
class Verdict:
    on_target: bool                 # 结果整体是否对题
    off: list = field(default_factory=list)   # 不对题的条目+理由，如 ["真丝厚睡衣：秋冬款，不符夏季"]
    suggestion: str = ""            # 裁判给的重搜/改进建议
    raw: str = ""                   # 模型原始输出（排障用）
    use: list = field(default_factory=list)   # 块H3c：可萃取采用的相关条目（污染结果里的有效少数）

    @property
    def salvageable(self) -> bool:
        """块H3c：整体不对题，但**有**可萃取的相关条目（部分污染）→ 该挑出来用，别整批丢。"""
        return (not self.on_target) and bool(self.use)


def build_judge_prompt(goal: str, results_text: str, has_images: bool = False) -> str:
    """构造裁判 prompt。要求模型只回紧凑 JSON，便于稳定解析。"""
    img_line = "另附若干结果配图，请一并据图判断（如季节、款式、品类是否对得上）。\n" if has_images else ""
    return (
        "你是严格的检索结果相关性裁判。下面是用户的目标，以及一次搜索/调研返回的结果。\n"
        "判断这些结果**整体上是否对题**——是否满足用户目标里的关键限定（如季节、品类、性别、"
        "预算、时效、权威性、是否真正对标）。挑出明显不对题的条目并给一句话理由。\n"
        "**关键**：即使整体不对题，也要把其中**确实相关、可用**的条目挑进 use（哪怕只有一两条）。"
        "**绝不要因为掺了垃圾就把相关的也一起丢弃**；也**绝不要**让人凭训练记忆硬编来替代这些有效内容。\n\n"
        f"【用户目标】{goal}\n\n"
        f"【搜索结果】\n{results_text}\n\n"
        f"{img_line}"
        "只输出一个紧凑 JSON，不要任何多余文字：\n"
        '{"on_target": true/false, "use": ["可采用的相关条目（原文标题/要点）", ...], '
        '"off": ["条目：不对题的理由", ...], "suggestion": "如何换词/换源/筛选重搜"}\n'
        "判据：多数结果不满足关键限定 → on_target=false（但仍把相关少数放进 use）。"
        "结果基本对题 → on_target=true、off 留空。use 为空只在**真的一条都不相关**时。"
    )


def parse_verdict(raw: str) -> Verdict:
    """稳健解析裁判输出。解析失败 → 按"对题"放行（不拦），符合"裁判出错不误触发"纪律。"""
    text = raw or ""
    m = _JSON_RE.search(text)
    if not m:
        return Verdict(True, [], "", text)
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return Verdict(True, [], "", text)
    on = d.get("on_target")
    on = True if on is None else bool(on)

    def _strlist(v):
        if not v:
            return []
        if not isinstance(v, list):
            v = [str(v)]
        return [str(x) for x in v if str(x).strip()]

    off = _strlist(d.get("off"))
    use = _strlist(d.get("use"))
    return Verdict(on, off, str(d.get("suggestion") or ""), text, use)


def judge_research(goal: str, results_text: str, judge_fn, images=None) -> Verdict:
    """跑一次裁判。goal 或 内容 缺失 → 直接放行（无可判）。judge_fn 故障 → 放行不拦。"""
    if not (goal and goal.strip()):
        return Verdict(True, [], "", "")
    if not ((results_text and results_text.strip()) or images):
        return Verdict(True, [], "", "")
    prompt = build_judge_prompt(goal, results_text or "", has_images=bool(images))
    try:
        raw = judge_fn(prompt, images) or ""
    except Exception:  # noqa: BLE001 — 裁判故障绝不影响主循环
        return Verdict(True, [], "", "")
    return parse_verdict(raw)
