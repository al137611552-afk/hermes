"""Architecture Review Mode 引擎（规划模式下的多角色方案评审，ADR 0019）。

把"方案被反复批评-修正-收敛"物化成一条可证伪的流程：
    Proposal（抽出 Decision 列表）→ 两角色 Review（Execution ⟷ Architecture）→ Revise → Consensus → gate 开工

核心纪律（来自 ADR 0014/0018，本模块的硬约束）：
  - **评审单位 = Decision 对象**，不评文档文本——reviewer 针对"当前选择 vs 备选 tradeoff"发言。
  - **共识是四态结构化文档，不是数值**——`Accepted/Rejected/Deferred/NeedUser`，就是 `Decision.status`。
  - **开工 gate 卡可数事实 `未决阻塞==0`，不卡"共识度 80%"**——后者是 expected_gain 同款模糊分，禁用。
  - **停止条件全部可证伪、可数**——轮数 / 零新增 blocking / 连两轮只改措辞，防无限互评（同搜索 loop-until-dry）。

本模块**纯逻辑、无 IO、无网络**：模型调用经注入式 `review_fn(prompt)->str`（同 judge 范式），便于单测/Golden。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ── 四态共识 = Decision.status（ADR 0019 ③①合一）─────────────────────────────
ACCEPTED = "Accepted"      # 采纳
REJECTED = "Rejected"      # 否决（附理由）
DEFERRED = "Deferred"      # 后置（附触发条件）
NEEDUSER = "NeedUser"      # 升级给用户拍板
OPEN = "Open"              # 尚在评审、未定（不是共识态 → 阻塞 gate）

_CONSENSUS_STATES = (ACCEPTED, REJECTED, DEFERRED, NEEDUSER)
_RESOLVED_STATES = (ACCEPTED, REJECTED, DEFERRED)   # 这三态 + 无 open blocking = 不阻塞 gate


@dataclass
class Decision:
    """一个被评审的架构决策——评审/共识/停止条件全部围绕它（ADR 0019）。"""
    id: str
    title: str
    current_choice: str = ""
    alternatives: list = field(default_factory=list)   # [{"choice","tradeoff"}] 或 [str]
    rationale: str = ""
    status: str = OPEN
    blocking: list = field(default_factory=list)        # 未决阻塞问题（str）；空=已澄清

    def signature(self) -> str:
        """架构签名：随"选择/状态"变，**不随 rationale 措辞变**——用于"连两轮只改措辞"停止判定。"""
        return f"{self.id}|{self.current_choice}|{self.status}"


def is_blocking(d: Decision) -> bool:
    """该决策是否阻塞开工 gate：升级待用户、或有未决阻塞、或还没收敛到共识态。"""
    if d.status == NEEDUSER:
        return True                     # 必须用户拍板
    if d.blocking:
        return True                     # 还有未澄清的阻塞问题
    return d.status not in _RESOLVED_STATES   # Open/未知 = 尚未收敛


def count_blocking(decisions) -> int:
    """未决阻塞 Decision 条数——这就是 gate 的可数事实（绝不换算成百分比）。"""
    return sum(1 for d in decisions if is_blocking(d))


def can_start_coding(decisions, user_signed: bool) -> bool:
    """开工 gate：`未决阻塞==0` **且** 用户签字。二者皆满足才解锁"开始编码"。

    诚实地：还有 N 个未决就是 N 个，按钮灰着；不编一个"共识度 73%"。
    """
    return count_blocking(decisions) == 0 and bool(user_signed)


def gate_status(decisions, user_signed: bool) -> dict:
    """给 UI/调用方的诚实门状态：能否开工 + 还差什么（全可数，无分数）。"""
    n = count_blocking(decisions)
    return {
        "can_start": can_start_coding(decisions, user_signed),
        "blocking_count": n,
        "user_signed": bool(user_signed),
        "reason": ("" if (n == 0 and user_signed)
                   else f"还有 {n} 个未决问题" if n
                   else "等待用户签字确认"),
    }


# ── 停止条件（可验证，绝不用百分比）─────────────────────────────────────────
def round_snapshot(decisions) -> dict:
    """把一轮评审快照成可比较的可数结构：阻塞问题集 + 架构签名集。"""
    blocking = set()
    for d in decisions:
        for b in d.blocking:
            blocking.add(f"{d.id}:{b}")
    return {"blocking": blocking, "decisions": {d.signature() for d in decisions}}


def should_stop(rounds, max_rounds: int = 3) -> tuple[bool, str]:
    """评审是否该停。rounds = [round_snapshot(...), ...]（按轮序）。满足任一即停，返回 (stop, 原因)。

    1. 达到最大轮数（防无限互评）。
    2. 连续一轮零新增 blocking（没人再提新阻塞 → 收敛）。
    3. 连续两轮只改措辞、零架构签名变化（边际收益归零，同 loop-until-dry）。
    全部只数条数变化，无任何"共识度"。
    """
    n = len(rounds)
    if n >= max_rounds:
        return True, "max_rounds"
    if n >= 2:
        new_block = set(rounds[-1]["blocking"]) - set(rounds[-2]["blocking"])
        if not new_block:
            return True, "no_new_blocking"
    if n >= 3:
        a, b, c = rounds[-3], rounds[-2], rounds[-1]
        if a["decisions"] == b["decisions"] == c["decisions"]:
            return True, "wording_only"
    return False, ""


# ── Consensus 渲染：按 status 四态分组 = 一份 ADR ─────────────────────────────
_SECTIONS = [
    (ACCEPTED, "Accepted（采纳）"),
    (REJECTED, "Rejected（否决）"),
    (DEFERRED, "Deferred（后置）"),
    (NEEDUSER, "Need User Decision（待你拍板）"),
    (OPEN, "Open（仍在评审）"),
]


def render_consensus(decisions) -> str:
    """把 Decision 按四态（+Open）分组打印成结构化共识文档——评审完即是一份 ADR 草稿。"""
    by_status: dict[str, list] = {}
    for d in decisions:
        by_status.setdefault(d.status, []).append(d)
    lines = ["# Consensus", ""]
    n = count_blocking(decisions)
    lines.append(f"未决阻塞：**{n}**" + ("（可开工待签字）" if n == 0 else "（开工 gate 锁死）"))
    lines.append("")
    for status, label in _SECTIONS:
        items = by_status.get(status)
        if not items:
            continue
        lines.append(f"## {label}")
        for d in items:
            lines.append(f"- **{d.title}**：{d.current_choice or '—'}")
            if d.rationale:
                lines.append(f"  - 理由：{d.rationale}")
            for b in d.blocking:
                lines.append(f"  - ⚠ 未决：{b}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── JSON 解析（容错，同 parse_grade 风格）────────────────────────────────────
def _first_json(text: str, opener: str, closer: str):
    """从模型输出里抠出第一段完整 JSON（数组或对象），失败返回 None。"""
    s = text or ""
    i = s.find(opener)
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _coerce_decision(obj: dict) -> Decision:
    """把一条 JSON 决策容错地塑成 Decision；status 非法 → Open。"""
    status = str(obj.get("status") or OPEN).strip()
    if status not in (_CONSENSUS_STATES + (OPEN,)):
        status = OPEN
    blocking = obj.get("blocking") or []
    if isinstance(blocking, str):
        blocking = [blocking]
    return Decision(
        id=str(obj.get("id") or obj.get("title") or "?").strip(),
        title=str(obj.get("title") or obj.get("id") or "?").strip(),
        current_choice=str(obj.get("current_choice") or obj.get("choice") or "").strip(),
        alternatives=obj.get("alternatives") or [],
        rationale=str(obj.get("rationale") or "").strip(),
        status=status,
        blocking=[str(b).strip() for b in blocking if str(b).strip()],
    )


def parse_decisions(text: str) -> list:
    """从 proposal 模型输出解析 Decision 列表（容忍 ```json 包裹/前后废话）。失败返回 []。"""
    data = _first_json(text, "[", "]")
    if not isinstance(data, list):
        # 兜底：单对象也接受
        one = _first_json(text, "{", "}")
        data = [one] if isinstance(one, dict) else []
    return [_coerce_decision(o) for o in data if isinstance(o, dict)]


def apply_review(decisions, review_text: str) -> list:
    """把一轮 reviewer 的 JSON 反馈合并进决策集：按 id 改 status、追加 blocking、解决 blocking。

    reviewer 输出形如：[{"id":"d1","status":"NeedUser","add_blocking":["..."],"resolve_blocking":["..."]}]
    未提到的决策原样保留。纯函数，返回新列表（不原地改）。
    """
    review = _first_json(review_text, "[", "]")
    if not isinstance(review, list):
        return list(decisions)
    by_id = {r.get("id"): r for r in review if isinstance(r, dict) and r.get("id")}
    out = []
    for d in decisions:
        r = by_id.get(d.id)
        if not r:
            out.append(d)
            continue
        new_status = str(r.get("status") or d.status).strip()
        if new_status not in (_CONSENSUS_STATES + (OPEN,)):
            new_status = d.status
        blocking = list(d.blocking)
        for b in (r.get("add_blocking") or []):
            b = str(b).strip()
            if b and b not in blocking:
                blocking.append(b)
        for b in (r.get("resolve_blocking") or []):
            b = str(b).strip()
            if b in blocking:
                blocking.remove(b)
        out.append(Decision(d.id, d.title, d.current_choice, d.alternatives,
                             d.rationale, new_status, blocking))
    return out


# ── 两个对冲 reviewer 角色 directive（Execution ⟷ Architecture）──────────────
EXECUTION_REVIEWER = (
    "你是 **Execution Reviewer（可交付性评审员）**。你的唯一职责是把范围往下压、盯住可交付。"
    "对每个 Decision 只问四件事：① 48 小时内能做出可验证的切片吗？② 会不会牵动上百个文件/大改架构？"
    "③ 有没有更小的 MVP 能先验证核心假设？④ **这个决策怎么用 Golden / 自测证伪？** "
    "凡过大、无法在短周期内验证、或没有验证手段的，提成 blocking 或建议 status=Deferred（拆小后再做）。"
    "你不负责拔高方案，只负责让它今晚就能验证。"
)
ARCHITECTURE_REVIEWER = (
    "你是 **Architecture Reviewer（架构评审员）**。你的职责是拉高天花板、防短视。"
    "对每个 Decision 问：当前选择会不会两个月后就要推倒重来？有没有被忽略的更稳的备选？"
    "是否违反既有架构纪律（事实/差距/做法分离、禁 score、物化而非建引擎）？关键取舍是否该升级给用户拍板？"
    "发现结构性风险提成 blocking；发现必须用户拍板的方向取舍设 status=NeedUser。"
    "你不负责砍范围，只负责让方案经得起时间。"
)
REVIEWERS = (("execution", EXECUTION_REVIEWER), ("architecture", ARCHITECTURE_REVIEWER))

_REVIEW_OUTPUT_SPEC = (
    "\n\n仅输出 JSON 数组，每个被你评的 Decision 一项："
    '[{"id":"<决策id>","status":"Accepted|Rejected|Deferred|NeedUser",'
    '"add_blocking":["新提的阻塞问题"],"resolve_blocking":["你认为已澄清的旧阻塞"]}]。'
    "没有意见的决策不要列。只输出 JSON，不要解释。"
)


def build_review_prompt(role_directive: str, decisions) -> str:
    """组织一轮 reviewer 的提示：角色职责 + 当前 Decision 快照 + 严格 JSON 输出契约。"""
    body = ["以下是当前方案的决策列表，请逐条评审：", ""]
    for d in decisions:
        body.append(f"- id={d.id} | {d.title}")
        body.append(f"  当前选择：{d.current_choice or '—'}")
        if d.alternatives:
            body.append(f"  备选：{json.dumps(d.alternatives, ensure_ascii=False)}")
        if d.rationale:
            body.append(f"  理由：{d.rationale}")
        if d.blocking:
            body.append(f"  现存未决：{'; '.join(d.blocking)}")
        body.append(f"  当前状态：{d.status}")
    return role_directive + "\n\n" + "\n".join(body) + _REVIEW_OUTPUT_SPEC


def run_review(decisions, review_fn, max_rounds: int = 3, reviewers=REVIEWERS) -> dict:
    """跑完整一轮多角色评审直到停止条件命中。`review_fn(prompt)->str` 注入式（同 judge 范式）。

    返回 {decisions, rounds, stop_reason, consensus, gate}。纯编排：不碰网络（review_fn 自理）。
    """
    cur = list(decisions)
    rounds = [round_snapshot(cur)]
    stop_reason = ""
    while True:
        stop, stop_reason = should_stop(rounds, max_rounds)
        if stop:
            break
        for _name, directive in reviewers:
            try:
                out = review_fn(build_review_prompt(directive, cur))
            except Exception:
                continue                # 评审员故障 → 跳过这一脑子，不中断评审
            cur = apply_review(cur, out)
        rounds.append(round_snapshot(cur))
    return {
        "decisions": cur,
        "rounds": rounds,
        "stop_reason": stop_reason,
        "consensus": render_consensus(cur),
        "gate": gate_status(cur, user_signed=False),
    }
