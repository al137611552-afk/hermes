"""Architecture Review Mode 引擎（规划模式下的多角色方案评审，ADR 0019）。

把"方案被反复批评-修正-收敛"物化成一条可证伪的流程：
    Proposal（抽出 Decision 列表）→ 两角色 Review（Execution ⟷ Architecture）→ Revise → Consensus → gate 开工

核心纪律（来自 ADR 0014/0018，本模块的硬约束）：
  - **评审单位 = Decision 对象**，不评文档文本——reviewer 针对"当前选择 vs 备选 tradeoff"发言。
  - **共识是四态结构化文档，不是数值**——`Accepted/Rejected/Deferred/NeedUser`，就是 `Decision.status`。
  - **开工 gate 卡可数事实 `未决阻塞==0`，不卡"共识度 80%"**——后者是 expected_gain 同款模糊分，禁用。
  - **停止条件全部可证伪、可数**——轮数 / 零新增 blocking / 连两轮只改措辞，防无限互评（同搜索 loop-until-dry）。

本模块**纯逻辑、无 IO、无网络**：reviewer 经注入式 seam `review_fn(name, prompt)->str`（同 judge 范式），便于单测/Golden。

**Reviewer 由"输出契约"定义，不由"是不是 LLM"定义**（ADR 0019）：任何东西——同模型、异构模型、规则、
静态分析器——只要吃 Decision、吐 `{id,status,add_blocking,resolve_blocking}` JSON，就是一个合法 reviewer。
引擎**完全不认识"模型"概念**：只按 `name` 喊 reviewer；某角色到底用哪个模型档案，是接线层按 name 路由的事
（异构 = 接线层一个 mapping；利用 delegate `Role.model` 字段，零引擎改动）。
"""
from __future__ import annotations

import json
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


def diagnose_decisions(text: str) -> str:
    """空结果归因（拆解为何没产出决策），供上层给出诚实提示：
    'ok'    抠到至少一条决策；
    'empty' 抠到合法 JSON 数组但为空 —— 方案没有架构级取舍（多为纯执行清单），并非报错；
    'nojson' 根本没抠到 JSON（模型吐了大白话，或被 max_tokens 截断没闭合）。
    """
    data = _first_json(text, "[", "]")
    if isinstance(data, list):
        return "ok" if any(isinstance(o, dict) for o in data) else "empty"
    if isinstance(_first_json(text, "{", "}"), dict):
        return "ok"
    return "nojson"


def apply_review(decisions, review_text: str) -> list:
    """把一轮 reviewer 的 JSON 反馈合并进决策集：按 id 改 status、追加 blocking、解决 blocking。

    reviewer 输出形如：[{"id":"d1","status":"NeedUser","add_blocking":["..."],"resolve_blocking":["..."]}]
    未提到的决策原样保留。纯函数，返回新列表（不原地改）。
    """
    # reviewer 现在先写散文意见、末尾给 ```json 结论（v4 可见辩论）：优先取 fenced 代码块里的数组，
    # 避免散文里偶发方括号误伤；无 fence（纯 JSON 输出，老测试/静态 reviewer）则退回全文首个数组。
    s = review_text or ""
    segment = s
    fi = s.rfind("```json")
    if fi >= 0:
        rest = s[fi + len("```json"):]
        end = rest.find("```")
        segment = rest if end < 0 else rest[:end]
    review = _first_json(segment, "[", "]")
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


# ── 两个对冲评审员 directive（产品/市场镜头 ⟷ 技术镜头）──────────────────────────
# ADR 0019 v4：把外部「复制给 GPT 再发给 Kimi 讨论」的体验显式建模为两个正交对冲镜头——
# 一个只从产品/市场价值挑刺、一个只从技术工程挑刺，主模型再收敛（= 3 方视角）。两镜头默认异构模型
# （降错误相关性）。两 directive 都强制**可证伪、只针对具体 Decision 发言**，产品镜头尤其禁「感觉不错」式空话。
PRODUCT_REVIEWER = (
    "【产品评审】你是 **产品/市场评审员（Product）**。默认立场：从市场、产品路线图、用户价值角度审，"
    "防止「技术上成立但产品上没人要 / 优先级错」的决策。对每个 Decision 问（全部要可证伪、落到具体事实，"
    "**禁「感觉不错 / 挺合理」这类空话**）：① 目标用户是谁、在什么场景用这个决策的产物？"
    "② 它服务哪个产品目标 / 路线图节点，还是偏离了主线？③ 竞品 / 现状是否已有等价物，我们这样做的差异化与理由？"
    "④ 优先级对吗——是不是过早优化、该后置，或有更高价值的事没做？"
    "产品 / 市场层面站不住的提成 blocking 或建议 status=Deferred；必须用户拍板的产品方向设 status=NeedUser。"
    "你只从产品价值挑刺，不做技术选型。"
)
TECHNICAL_REVIEWER = (
    "【技术评审】你是 **技术评审员（Technical）**。默认立场：把技术选型、架构、可行性与工程风险审扎实，"
    "既压范围也防短视。对每个 Decision 问：① 48 小时内能做出可验证切片吗，会不会改上百个文件、有没有更小 MVP？"
    "② 技术选型 X vs 备选 Y 的 tradeoff 是什么，当前选择两个月后会不会推倒重来？"
    "③ 有没有逻辑漏洞、被忽略的更稳备选、没考虑的边界 / 风险 / 维护成本？"
    "④ 怎么用 Golden / 自测证伪？是否违反既有架构纪律（事实 / 差距 / 做法分离、禁 score、物化而非建引擎）？"
    "工程风险或遗漏提成 blocking，过大 / 无法短周期验证建议 status=Deferred，必须用户拍板的技术取舍设 status=NeedUser。"
    "你只从技术角度挑刺，不评产品价值。"
)
REVIEWERS = (("product", PRODUCT_REVIEWER), ("technical", TECHNICAL_REVIEWER))

# 旧键（v3 及以前的 execution/architecture）→ 新键（product/technical）迁移映射：
# 兼容用户已存的 config.yaml design_review_models 与历史会话，读时归一，不强迫用户改配置。
REVIEWER_ALIASES = {"execution": "product", "architecture": "technical"}


def migrate_reviewer_models(mapping) -> dict:
    """把 design_review_models 里旧角色键归一到新键（execution→product、architecture→technical）；丢空值。"""
    out = {}
    for k, v in (mapping or {}).items():
        if v:
            out[REVIEWER_ALIASES.get(k, k)] = v
    return out

_REVIEW_OUTPUT_SPEC = (
    "\n\n请分两部分作答：\n"
    "① 先用简洁中文写你的评审意见——针对你有看法的 Decision，说清「当前选择 vs 备选」的问题/风险/建议"
    "（这是给用户看的讨论，像同行评审一样直说，别客套）；\n"
    "② 最后另起一行，输出结构化结论 JSON 数组（用 ```json 代码块包裹），每个你评过的 Decision 一项：\n"
    '```json\n[{"id":"<决策id>","status":"Accepted|Rejected|Deferred|NeedUser",'
    '"add_blocking":["新提的阻塞问题"],"resolve_blocking":["你认为已澄清的旧阻塞"]}]\n```\n'
    "没有意见的决策不要列。JSON 必须是最后一段、可被机器解析（散文里别用方括号）。"
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


# 评审 verdict 输出天生紧凑（每条决策就 {id,status,blocking} 几十 token），此上限是**防长篇大论的安全网**、
# 不是紧箍：设得宽松（覆盖 ~50 条决策的 verdict），既挡住模型跑偏写小作文，又不至于把 verdict 数组从中间切断。
REVIEW_MAX_TOKENS = 2048
REVIEW_TIMEOUT_S = 90             # 单个角色单次调用超时（秒）：慢/卡的调用不无限等，超时按空评审跳过


def _run_reviewers_parallel(review_fn, prompts, timeout: int = REVIEW_TIMEOUT_S) -> list:
    """并发跑一轮的多个角色评审，各自带超时；返回与 prompts 同序的评审文本（故障/超时→"[]"）。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
    if not prompts:
        return []
    with ThreadPoolExecutor(max_workers=len(prompts)) as ex:
        futs = [ex.submit(review_fn, name, prompt) for name, prompt in prompts]
        outs = []
        for f in futs:
            try:
                outs.append(f.result(timeout=timeout))
            except (FTimeout, Exception):   # noqa: BLE001 — 超时/故障：这一脑子当没意见，不中断评审
                outs.append("[]")
        return outs


def escalate_unresolved(decisions) -> list:
    """评审收敛后，凡还没收敛到共识四态的（Open）决策一律升级为 NeedUser（交用户拍板）。

    否则「收敛后仍未完成项」会卡在 Open：`is_blocking` 为真锁死 gate，但前端又不把 Open 当"待拍板"
    → 没有拍板入口、用户无从推进。升级为 NeedUser 后面板必给拍板控件，gate 依旧不自动放行（守 ADR 0014）。
    保留原 blocking 与 choice，只改状态。
    """
    out = []
    for d in decisions:
        if d.status == OPEN:
            out.append(Decision(d.id, d.title, d.current_choice, d.alternatives,
                                 d.rationale, NEEDUSER, list(d.blocking)))
        else:
            out.append(d)
    return out


def run_review(decisions, review_fn, max_rounds: int = 3, reviewers=REVIEWERS,
               timeout: int = REVIEW_TIMEOUT_S, on_event=None) -> dict:
    """跑完整一轮多角色评审直到停止条件命中。

    `review_fn(name, prompt)->str` 注入式 seam（同 judge 范式）：引擎按 reviewer **名字**调用，
    **不认识"模型"**——接线层据 name 把不同角色路由到不同模型档案（异构 = 那一个 mapping）。
    返回 {decisions, rounds, stop_reason, consensus, gate}。纯编排：不碰网络（review_fn 自理）。
    """
    def _emit(kind, payload):
        if on_event:
            try:
                on_event(kind, payload)
            except Exception:  # noqa: BLE001 — 事件回调故障不该中断评审
                pass
    cur = list(decisions)
    rounds = [round_snapshot(cur)]
    stop_reason = ""
    round_idx = 0
    while True:
        stop, stop_reason = should_stop(rounds, max_rounds)
        if stop:
            break
        round_idx += 1
        _emit("round_start", {"round": round_idx})
        # 一轮内两个角色**并行**：都审同一份轮初快照（更像独立双审），各自超时/故障→空评审跳过。
        # 异构时两角色打不同端点 → 真同时跑，一轮耗时 ≈ max 而非 sum。
        prompts = [(name, build_review_prompt(directive, cur)) for name, directive in reviewers]
        outs = _run_reviewers_parallel(review_fn, prompts, timeout=timeout)
        for (name, _directive), out in zip(reviewers, outs):   # 顺序合并两份评审（apply 只改 status/blocking）
            cur = apply_review(cur, out)
            _emit("reviewer_done", {"round": round_idx, "reviewer": name, "verdict": out})
        rounds.append(round_snapshot(cur))
    cur = escalate_unresolved(cur)          # 收敛后仍未定的 Open → NeedUser（交用户拍板，不留死状态）
    _emit("converged", {"stop_reason": stop_reason, "rounds": len(rounds) - 1})
    return {
        "decisions": cur,
        "rounds": rounds,
        "stop_reason": stop_reason,
        "consensus": render_consensus(cur),
        "gate": gate_status(cur, user_signed=False),
    }


# ── IO 适配器：把 provider 包成引擎 seam（唯一碰 provider 的地方，IO 在 provider 内）──
def make_review_fn(provider_for, max_tokens: int = REVIEW_MAX_TOKENS, on_delta=None):
    """把"按 reviewer 名取 provider"的 `provider_for(name)->provider` 包成 seam `review_fn(name, prompt)->str`。

    **异构路由的唯一落点**：provider_for 内部据 name 选不同模型档案（如 `build_provider(config, profile)`），
    Execution 用主模型、Architecture 路由到异构档即可。`provider_for(name)` 返回 None → 该角色跳过（吐空评审）。
    本函数同 `_make_research_judge` 范式：自身无 IO，IO 在注入的 provider.stream_chat 内。
    """
    from ..providers.base import Message      # 延迟导入，保持模块 import 期纯净

    def review_fn(name, prompt):
        provider = provider_for(name)
        if provider is None:
            return "[]"                        # 没配该角色的模型 → 无意见，不阻断评审
        out = []
        for ev in provider.stream_chat([Message("user", prompt)], system=None,
                                       tools=[], max_tokens=max_tokens):
            if getattr(ev, "type", None) == "text":
                out.append(ev.text)
                if on_delta:                       # 逐 token 推给前端分屏（v4 实时辩论）
                    try:
                        on_delta(name, ev.text)
                    except Exception:  # noqa: BLE001 — 推流故障不阻断评审
                        pass
        return "".join(out)
    return review_fn


# ── 评审会话状态机：api/前端驱动它（评审 → 逐条拍板 → 签字 → gate）──────────────
class DesignReviewSession:
    """一次方案评审的可驱动状态：持有 Decision 集，支持跑评审、用户逐条拍板 NeedUser、签字、查 gate。

    纯逻辑（review_fn 注入）。供 conversation/api 在规划模式下驱动；前端按其 gate()/consensus() 渲染。
    """

    def __init__(self, decisions, max_rounds: int = 3, timeout: int = REVIEW_TIMEOUT_S) -> None:
        self.decisions = list(decisions)
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.signed = False
        self.last_result = None

    @classmethod
    def from_proposal(cls, proposal_text: str, max_rounds: int = 3,
                      timeout: int = REVIEW_TIMEOUT_S) -> "DesignReviewSession":
        """从模型 proposal 输出抽 Decision 列表建会话。"""
        return cls(parse_decisions(proposal_text), max_rounds, timeout)

    def review(self, review_fn, on_event=None) -> dict:
        """跑一整轮多角色评审（直到停止条件），更新决策集。返回 run_review 结果。

        on_event(kind, payload)：可选，逐轮/逐角色进度回调（round_start / reviewer_done / converged），
        供 conversation 转成前端事件做实时分屏；缺省=不回调（纯逻辑/单测不受影响）。
        """
        res = run_review(self.decisions, review_fn, self.max_rounds,
                         timeout=self.timeout, on_event=on_event)
        self.decisions = res["decisions"]
        self.signed = False                    # 决策集变了 → 旧签字作废
        self.last_result = res
        return res

    def resolve(self, decision_id: str, status: str, current_choice=None) -> bool:
        """用户拍板一个决策：设其共识态（须四态之一）、可定稿 choice、清空该条 blocking。

        改动后**作废已有签字**（不能签完又偷改）。命中并合法返回 True，否则 False。
        """
        if status not in _CONSENSUS_STATES:
            return False
        hit = False
        out = []
        for d in self.decisions:
            if d.id == decision_id:
                hit = True
                out.append(Decision(
                    d.id, d.title,
                    d.current_choice if current_choice is None else str(current_choice),
                    d.alternatives, d.rationale, status, []))
            else:
                out.append(d)
        if hit:
            self.decisions = out
            self.signed = False
        return hit

    def sign(self) -> None:
        """用户签字确认开工——仅在零未决时才有意义（gate 仍会复核 count_blocking）。"""
        self.signed = True

    def gate(self) -> dict:
        return gate_status(self.decisions, self.signed)

    def consensus(self) -> str:
        return render_consensus(self.decisions)

    def can_start(self) -> bool:
        return can_start_coding(self.decisions, self.signed)
