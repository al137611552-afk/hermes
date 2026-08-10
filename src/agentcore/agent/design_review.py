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

def focus_count(n_decisions: int, cap: int = 6) -> int:
    """一轮里让镜头**深说几条**。决策多时不逐条流水账——挑最关键的说（纯函数）。

    真机遇到过：10 条决策一次全评，散文写到一半撞 max_tokens 被截断，尾部几条的意见**全丢**，
    而主模型看不出那是有偏子集（很容易把"没提到"当成"没问题"）。与其被截断截掉尾巴，
    不如让镜头**自己挑**最该说的几条——同样的预算，信号密度高得多。
    """
    n = max(1, int(n_decisions or 1))
    return n if n <= cap else cap


def review_output_spec(n_decisions: int = 0) -> str:
    """评审员输出契约。决策多于 focus_count 时，显式要求挑重点说，别逐条铺开。"""
    k = focus_count(n_decisions)
    focus = ""
    if n_decisions and n_decisions > k:
        focus = (f"\n**范围纪律**：这轮共 {n_decisions} 条决策，**只挑其中最关键的 ≤{k} 条展开说**"
                 "（挑你认为风险最高/最可能错的），其余的不用提。宁可少而准，别逐条写成流水账"
                 "——写太长会被输出上限从中间切断，尾部意见直接丢失。\n")
    return focus + _REVIEW_OUTPUT_SPEC


# ── 默认异构：自动把两个镜头分到不同模型（ADR 0019 的核心机制，原来默认没生效）──────────
# ADR v4 锁的是"手动评审**默认异构** 2 模型"，代码注释也写着"两镜头默认异构模型（降错误相关性）"，
# 但实现的默认是 `design_review_models: {}` → provider_for 全部回落主模型
# = **同一个模型演三个角色**。同模型的错误高度相关，它挑不出自己看不见的问题；
# 真机反馈过的"产物像单模型提炼"，根因就在这里（当时以为是过程不可见，加了分屏，其实是同构）。
# 现在：用户没显式配就**自动挑**——跨 provider 优先（不同厂商错误相关性最低），
# 实在只有一个模型可用就如实降级、并让界面说清楚，而不是继续管它叫"多模型讨论"。


def _provider_of(profile: str) -> str:
    """模型档案名 `provider/model` 取 provider 段（老式扁平名就是它自己）。"""
    return (profile or "").split("/", 1)[0]


def usable_profiles(models, env_get) -> list:
    """筛出**当前真能用**的模型档案：写了 `api_key_env` 且该环境变量有值。纯函数（env 注入）。

    **没写 `api_key_env` 的不算可用**（自定义 provider 常留空）——自动挑是"替用户做主"，只该在明确
    能跑的档案里挑。原来把空 env 当成可用，结果是：面板里加过一个没填 key 的服务商，评审就会把它
    自动派成「产品镜头」，界面显示得像正常的多模型讨论，实际那一路根本调不通。
    想用无需 key 的本地端点，仍可在下拉里**显式**指定（显式配置不受此限）。
    """
    out = []
    for name, mc in (models or {}).items():
        env = getattr(mc, "api_key_env", None)
        if env is None and isinstance(mc, dict):
            env = mc.get("api_key_env")
        if env and (env_get(env) or "").strip():
            out.append(name)
    return out


def auto_reviewer_models(available, active: str, explicit=None) -> dict:
    """给三个角色（product / technical / main）定模型档案。用户显式配的优先，其余自动挑。

    挑选偏好：① 与主模型**不同 provider** 的排前面（跨厂商 = 错误相关性最低）；
    ② 两个镜头彼此也尽量不同 provider；③ 没得挑就回落主模型（此时 is_heterogeneous 为假）。
    纯函数，便于单测穷举各种"只有一个模型/只有两个/跨厂商"的组合。
    """
    explicit = migrate_reviewer_models(explicit or {})
    pool = [p for p in (available or []) if p and p != active]
    pool.sort(key=lambda p: _provider_of(p) == _provider_of(active))   # 异厂商优先（稳定排序）
    picks: list = []
    for role in ("product", "technical"):
        if explicit.get(role):
            picks.append(explicit[role])
            continue
        used_provs = {_provider_of(x) for x in picks}
        cand = (next((p for p in pool if p not in picks and _provider_of(p) not in used_provs), None)
                or next((p for p in pool if p not in picks), None)
                or active)
        picks.append(cand)
    return {"product": picks[0], "technical": picks[1], "main": explicit.get("main") or active}


def is_heterogeneous(plan: dict) -> bool:
    """两个镜头是否真的落在不同模型上——这是"多模型讨论"这个说法成不成立的唯一判据。"""
    return bool(plan) and plan.get("product") != plan.get("technical")


_REVIEW_OUTPUT_SPEC = (
    "\n\n**你只是进言，不做决定**：hub-and-spoke（ADR 0019 v5）——你只向**主模型**进言，最终采纳/反驳/收敛"
    "全由主模型逐条回复决定。你**建议**的 status/blocking 是给主模型的参考，不会直接改动方案；尤其**不得替方案"
    "改 current_choice**（那是主模型的权，你只挑问题）。\n"
    "请分两部分作答：\n"
    "① 先用简洁中文写你的评审意见——针对你有看法的 Decision，说清「当前选择 vs 备选」的问题/风险/建议"
    "（这是给用户与主模型看的讨论，像同行评审一样直说，别客套）；\n"
    "② 最后另起一行，输出结构化**建议** JSON 数组（用 ```json 代码块包裹），每个你评过的 Decision 一项：\n"
    '```json\n[{"id":"<决策id>","status":"Accepted|Rejected|Deferred|NeedUser",'
    '"add_blocking":["新提的阻塞问题"],"resolve_blocking":["你认为已澄清的旧阻塞"]}]\n```\n'
    "没有意见的决策不要列。JSON 必须是最后一段、可被机器解析（散文里别用方括号）。"
)


def prose_only(text: str) -> str:
    """取模型输出的**散文段**，丢掉末尾的 ```json 结构化块（对读者是噪声）。纯函数。"""
    s = text or ""
    fi = s.rfind("```json")
    return (s[:fi] if fi >= 0 else s).strip()


# 第 2 轮起喂给评审员的"上一轮主模型回复"（C6）。**只喂 hub 的回复、不喂对方镜头的意见**：
# 两个镜头彼此看不见是 ADR 0019 的核心机制（独立双审 → 降错误相关性），互相看见就会被带偏，
# 那正是"默认异构"刚治好的病。评审员要能听见的是**决策者**怎么回应了自己。
_RESPOND_DIRECTIVE = (
    "\n\n**回应主模型**：上面是主模型读完你（和另一位镜头）的进言后做的决定。这一轮你要**回应它**，"
    "不是把同一份意见原样重发：\n"
    "- 被它反驳的：要么给出**可证伪的**理由坚持（举反例/说清代价），要么明确让步并说明为什么改主意；\n"
    "- 被它采纳的：不必重复表扬，直接跳过；\n"
    "- 只提这一轮**还真正值得说**的；没有新东西可说就明说「本轮无补充」，别为凑字数造新问题。"
)


def build_review_prompt(role_directive: str, decisions, main_reply: str = "") -> str:
    """组织一轮 reviewer 的提示：角色职责 + 当前 Decision 快照（+ 上一轮主模型回复）+ 严格 JSON 输出契约。

    `main_reply` = 上一轮主模型（hub）的回复原文，第 2 轮起由 run_review 传入。不传＝第一轮，
    评审员只看方案本身（独立首审）。
    """
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
    tail = review_output_spec(len(list(decisions)))
    prose = prose_only(main_reply)
    if prose:
        body.append("")
        body.append("【主模型上一轮的回复】（它读了双方进言后做的决定）")
        body.append(prose)
        tail = _RESPOND_DIRECTIVE + tail
    return role_directive + "\n\n" + "\n".join(body) + tail


# ── 主模型（hub）逐轮回复 directive + prompt + apply（ADR 0019 v5：唯一改 Decision 状态处）────
# hub-and-spoke：两评审员各自只向主模型进言（build_review_prompt），主模型逐轮读双方意见 → 逐 Decision 表态
# （采纳/反驳/追问、真做取舍）→ 输出结构化决策 JSON。**这是全流程唯一能改 Decision 的 status/blocking/current_choice
# 的地方**（决策 A）；评审员的 JSON 只当参考进言（apply_review 从不被 run_review 调用于评审员输出）。
MAIN_REPLY_DIRECTIVE = (
    "【主模型收敛】你是这份方案的**主模型（决策者）**。两位评审员（产品镜头、技术镜头）刚对你的方案逐条进言，"
    "你要**逐一回复**并对每个被点到的 Decision 做出**决定**——这是 hub-and-spoke：评审员只进言，改不改方案由你拍。\n"
    "硬纪律（否则退化成假讨论，必须遵守）：\n"
    "① **言之有物**：每条回复必须**绑定具体 Decision id**、可证伪、真做取舍——明说采纳了谁的哪条、反驳了谁的哪条、"
    "为什么。**禁**「你们说得都有道理 / 综合考虑 / 都很合理」这类空话（同项目既有禁「感觉不错」的可证伪纪律）。\n"
    "② 采纳某评审员提的问题→把它写进该 Decision 的 add_blocking 或改 status；反驳→在散文里给可证伪理由并 resolve_blocking；"
    "拿不定的产品/技术方向→status=NeedUser 交用户拍板。\n"
    "③ 你**可以**调整 current_choice（把方案改得更好，这是你的权），但要在散文里说清为什么改。\n"
    "④ 停止条件是可数的：不制造无谓的新 blocking 来拖轮次；该收敛就收敛（把已澄清的移进 resolve_blocking）。**禁任何"
    "共识百分比/评分**。"
)
_MAIN_REPLY_OUTPUT_SPEC = (
    "\n\n请分两部分作答：\n"
    "① 先用简洁中文**逐条回复**评审员意见——每条点名 Decision id，说清你采纳/反驳/追问了什么、为什么（可证伪、真取舍，"
    "禁空话）；\n"
    "② 最后另起一行，输出结构化决策 JSON 数组（用 ```json 代码块包裹），**只列你本轮做了决定的 Decision**：\n"
    '```json\n[{"id":"<决策id>","current_choice":"<你定的选择，可留原样>",'
    '"status":"Accepted|Rejected|Deferred|NeedUser|Open",'
    '"add_blocking":["你决定保留/新增的阻塞问题"],"resolve_blocking":["你判定已澄清的阻塞"]}]\n```\n'
    "没做决定的 Decision 不要列（保持原状）。JSON 必须是最后一段、可被机器解析（散文里别用方括号）。"
)


def main_output_spec(n_decisions: int = 0) -> str:
    """主模型（hub）的输出契约。决策多时给**散文**限范围，但 JSON 不许省。

    与评审员那条范围纪律不同的是后果：镜头被截断只是少几条意见，**主模型被截断＝末尾的 JSON 没了，
    这一轮的所有决定一条都不生效**（`apply_main_reply` 拿不到数组）。所以这里压的是散文长度，
    并明说"JSON 才是唯一生效的部分"。
    """
    k = focus_count(n_decisions)
    if not n_decisions or n_decisions <= k:
        return _MAIN_REPLY_OUTPUT_SPEC
    return (f"\n\n**范围纪律**：这轮共 {n_decisions} 条决策。**散文只展开最关键的 ≤{k} 条**"
            "（其余条目有决定就直接写进 JSON，不必逐条写理由）。"
            "注意：**只有末尾的 JSON 会真正生效**，散文写太长会把 JSON 挤出输出上限，"
            "那样这一轮的决定**一条都不会落地**——宁可少说，也要把 JSON 完整写出来。"
            + _MAIN_REPLY_OUTPUT_SPEC)


def build_main_reply_prompt(decisions, reviewer_outputs) -> str:
    """组织主模型一轮回复的提示：当前 Decision 快照 + 本轮两评审员的进言（散文+建议 JSON 原文）。

    `reviewer_outputs` = [(name, text), ...]（本轮评审员输出，按序）。主模型读双方意见 + 当前决策，
    逐条表态并输出结构化决策 JSON（唯一改状态处）。
    """
    body = ["以下是当前方案的决策列表：", ""]
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
    body.append("")
    body.append("本轮两位评审员的进言如下（这是给你参考的意见，不是已生效的改动）：")
    for name, text in reviewer_outputs:
        label = dict(REVIEWERS).get(name, name)
        # 只取评审员散文意见段（```json 建议块对主模型是噪声，主模型自己重新决策）
        body.append("")
        body.append(f"【{name}｜{label}】")
        body.append(prose_only(text) or "（本镜头无意见）")
    return MAIN_REPLY_DIRECTIVE + "\n\n" + "\n".join(body) + main_output_spec(len(list(decisions)))


def apply_main_reply(decisions, reply_text: str) -> list:
    """把主模型一轮回复的 JSON 决策合并进决策集——**唯一改 Decision 状态处**（决策 A，ADR 0019 v5）。

    与 `apply_review`（评审员进言，禁改 current_choice）的关键区别：主模型**可**改 current_choice。
    其余（status 校验、add/resolve blocking、优先 fenced 数组解析）沿用同款容错。未提到的决策原样保留。
    纯函数，返回新列表。
    """
    s = reply_text or ""
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
        # 决策 A：主模型（且仅主模型）能定稿 current_choice；缺省/空则保留原选择。
        new_choice = d.current_choice
        if "current_choice" in r:
            c = str(r.get("current_choice") or "").strip()
            if c:
                new_choice = c
        blocking = list(d.blocking)
        for b in (r.get("add_blocking") or []):
            b = str(b).strip()
            if b and b not in blocking:
                blocking.append(b)
        for b in (r.get("resolve_blocking") or []):
            b = str(b).strip()
            if b in blocking:
                blocking.remove(b)
        out.append(Decision(d.id, d.title, new_choice, d.alternatives,
                             d.rationale, new_status, blocking))
    return out


# 评审 verdict 输出天生紧凑（每条决策就 {id,status,blocking} 几十 token），此上限是**防长篇大论的安全网**、
# 不是紧箍：设得宽松（覆盖 ~50 条决策的 verdict），既挡住模型跑偏写小作文，又不至于把 verdict 数组从中间切断。
REVIEW_MAX_TOKENS = 2048
REVIEW_TOKENS_PER_DECISION = 600   # 每条决策的散文+JSON 经验开销（实测 4096 在 10 条时必被截断）
REVIEW_TOKENS_OVERHEAD = 800       # 开场/收尾/JSON 数组框架等固定开销


def scale_review_budget(base: int, n_decisions: int, model_cap: "int | None" = None) -> int:
    """按决策条数伸缩单镜头的输出预算，再顶到模型单次上限（纯函数）。

    固定上限 + 随规模线性增长的输出 = 必然截断，而且**截在尾部**——后面的决策一条都没评到。
    这个组合在本项目里栽过三次（shell 输出头部截断、web_fetch cap、这里），
    统一的解法都是：预算跟着规模走，够不着就明说范围，别无声地丢尾巴。
    """
    want = max(int(base or 0), REVIEW_TOKENS_PER_DECISION * max(1, int(n_decisions or 1))
               + REVIEW_TOKENS_OVERHEAD)
    if model_cap and model_cap > 0:
        want = min(want, int(model_cap))
    return max(1, want)


REVIEW_TIMEOUT_CAP_FACTOR = 2.0    # 超时最多放宽到基准的几倍（够 10 条决策写完，又不让卡死的调用拖太久）


def scale_review_timeout(base: int, n_decisions: int, base_budget: int = REVIEW_MAX_TOKENS,
                         cap_factor: float = REVIEW_TIMEOUT_CAP_FACTOR) -> int:
    """单角色超时随规模伸缩（纯函数）：**预算涨了多少倍，超时同比放宽多少**，封顶 cap_factor 倍。

    **只放宽预算不放宽超时＝白放宽**：10 条决策的进言按 6800 token 预算写，30 tok/s 下要 ~227s，
    而超时钉在 180s → 写到一半被超时打断、回捞成部分内容，**尾部决策照样丢**，只是换了个丢法。
    `base_budget` 传实际用的基线预算（run_review 从 review_fn 上取），预算没涨时超时也不该动；
    封顶是防另一头：真卡住的调用不该因为决策多就拖到十分钟。
    """
    ref = max(1, int(base_budget or REVIEW_MAX_TOKENS))
    ratio = scale_review_budget(ref, n_decisions) / ref
    return max(1, int(int(base or 0) * min(max(1.0, ratio), float(cap_factor))))
REVIEW_TIMEOUT_S = 90             # 单个角色单次调用超时（秒）：慢/卡的调用不无限等，超时按空评审跳过


def _run_reviewers_serial(review_fn, prompts, timeout: int = REVIEW_TIMEOUT_S, cancel=None) -> list:
    """**顺序**跑一轮的多个角色评审（产品先说、技术再回应——像两个模型轮流讨论）；各自带独立超时；
    返回与 prompts 同序的评审文本（故障/超时→"[]"）。

    v4 由并行改顺序：① 分屏辩论逐个流式打字更像"讨论"、不再两列同时乱蹦；② 规避同一 API key
    并发多路请求被上游限流导致某一路空手而归（真机：技术镜头没输出）。每角色一个单线程执行器只为
    施加超时，不为并发。低频主动动作，一轮 ≈ sum 的延迟可接受。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
    outs = []
    partial = getattr(review_fn, "partial", {})   # review_fn 增量存的已流式内容（见 make_review_fn）
    for name, prompt in prompts:
        if cancel and cancel():   # 用户已取消评审（退规划/关面板/进开发）：剩下的角色不再发起模型调用，直接空手跳过
            outs.append("[]")
            continue
        with ThreadPoolExecutor(max_workers=1) as ex:   # 仅用于施加单角色超时；顺序执行=逐个流式
            f = ex.submit(review_fn, name, prompt)
            try:
                outs.append(f.result(timeout=timeout))
            except FTimeout:   # 超时：**回捞已流式到的部分内容**，别丢成 "[]"——否则前端已打出的散文被覆盖、主模型也读不到（真机 bug）
                outs.append((partial.get(name) or "").strip() or "[]")
            except Exception:   # noqa: BLE001 — 其它故障：这一脑子当没意见，不中断评审
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


MAIN = "main"                    # 主模型（hub）在 review_fn seam 里的保留名——接线层据此路由到主模型档
MAIN_REPLY_TIMEOUT_S = 120       # 主模型逐轮回复超时（比评审员宽：它要读双方意见 + 逐条表态，输出更长）


def run_review(decisions, review_fn, max_rounds: int = 3, reviewers=REVIEWERS,
               timeout: int = REVIEW_TIMEOUT_S, on_event=None,
               main_timeout: int = MAIN_REPLY_TIMEOUT_S, min_rounds: int = 1, cancel=None) -> dict:
    """跑完整多轮 hub-and-spoke 评审直到停止条件命中（ADR 0019 v5）。

    每轮：两评审员各自**只向主模型进言**（串行流式，避免同 key 并发限流）→ **主模型逐轮回复**（读双方意见、
    逐 Decision 表态、输出结构化决策 JSON）→ apply 主模型 JSON（**唯一改 Decision 状态处**，决策 A）→ 判停止。
    评审员的进言**不改 Decision 状态**（apply_review 不在此调用），只作为参考喂给主模型。

    `review_fn(name, prompt)->str` 注入式 seam：引擎按名字调用——评审员名（product/technical）+ 主模型保留名
    MAIN（"main"）；接线层据 name 路由到不同模型档（异构 = 那一个 mapping）。**每轮一次主模型调用**（决策 B）。
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
    last_main_out = ""                  # 上一轮主模型回复：第 2 轮起喂回评审员，让讨论真是"回应"（C6）
    eff_min = max(0, min(min_rounds, max_rounds - 1))   # 讨论轮下限，但不越过 max_rounds 硬顶
    while True:
        if cancel and cancel():          # 协作式取消：用户退出规划/关面板/开工时中止，别把评审跑到底（与开发并发）
            stop_reason = "cancelled"
            break
        stop, stop_reason = should_stop(rounds, max_rounds)
        # 用户明确要"评审员基于主模型回复再讨论"：**提前收敛**（no_new_blocking/wording_only 等非 max_rounds 原因）
        # 在跑满 eff_min 个讨论轮前不生效，保证 hub 至少来回 eff_min 轮（评审员→主模型→评审员…）。max_rounds 仍是硬顶。
        # should_stop 决策内核未动（golden 不破）——这是 run_review 编排层的下限门。
        if stop and stop_reason != "max_rounds" and round_idx < eff_min:
            stop, stop_reason = False, ""
        if stop:
            break
        round_idx += 1
        _emit("round_start", {"round": round_idx})
        # 1) 两评审员**顺序**进言：都审同一份轮初快照（独立双审），各自超时/故障→空进言跳过。
        #    v4 由并行改顺序——分屏逐个流式打字像"讨论"，且规避同 key 并发被限流（见 _run_reviewers_serial）。
        #    v5：评审员输出**不 apply**（只进言），逐条 emit 供前端分屏。
        try:
            review_fn.scope = len(cur)     # 供 make_review_fn 按规模伸缩预算（同 .partial 的属性约定）
        except AttributeError:             # 传进来的是不可挂属性的可调用对象（如 lambda 之外的 C 函数）
            pass
        # C6：第 2 轮起把**上一轮主模型的回复**喂给评审员——原来每轮只喂决策快照，评审员既听不见 hub
        # 也听不见对方，"再讨论一轮"实际是"看着被改过的决策再审一遍"，不是回应。只喂 hub 不喂对方镜头：
        # 两个镜头彼此独立是降错误相关性的核心（见 _RESPOND_DIRECTIVE）。
        prompts = [(name, build_review_prompt(directive, cur, last_main_out))
                   for name, directive in reviewers]
        # 超时与预算成对伸缩：只放宽预算不放宽超时＝写到一半被打断，尾部照样丢（见 scale_review_timeout）。
        # 基线预算从 review_fn 上取（同 .scope/.partial 的属性约定），预算没涨时超时也不动。
        base_budget = getattr(review_fn, "base_max_tokens", REVIEW_MAX_TOKENS)
        outs = _run_reviewers_serial(review_fn, prompts,
                                     timeout=scale_review_timeout(timeout, len(cur), base_budget),
                                     cancel=cancel)
        reviewer_outputs = []
        for (name, _directive), out in zip(reviewers, outs):
            reviewer_outputs.append((name, out))
            _emit("reviewer_done", {"round": round_idx, "reviewer": name, "verdict": out})
        if cancel and cancel():          # 评审员跑完后再确认一次：取消就别再发起主模型那次（更长的）调用
            stop_reason = "cancelled"
            break
        # 2) **主模型逐轮回复**（一次调用）：读双方进言 + 当前决策 → 逐条表态 → 结构化决策 JSON。
        _emit("main_reply_start", {"round": round_idx})
        main_prompt = build_main_reply_prompt(cur, reviewer_outputs)
        main_out = _run_reviewers_serial(
            review_fn, [(MAIN, main_prompt)],
            timeout=scale_review_timeout(main_timeout, len(cur), base_budget), cancel=cancel)[0]
        # 3) apply 主模型 JSON —— **唯一改 Decision 状态处**（决策 A：可改 status/blocking/current_choice）。
        cur = apply_main_reply(cur, main_out)
        last_main_out = main_out
        _emit("main_reply_done", {"round": round_idx, "reply": main_out})
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
def make_review_fn(provider_for, max_tokens: int = REVIEW_MAX_TOKENS, on_delta=None,
                   main_max_tokens: "int | None" = None):
    """把"按 reviewer 名取 provider"的 `provider_for(name)->provider` 包成 seam `review_fn(name, prompt)->str`。

    **异构路由的唯一落点**：provider_for 内部据 name 选不同模型档案（如 `build_provider(config, profile)`），
    评审员用异构档、主模型（MAIN="main"）路由到主档即可。`provider_for(name)` 返回 None → 该角色跳过（吐空）。
    主模型逐轮回复（name==MAIN）更长：用 `main_max_tokens`（缺省=不限，走模型单次预算）而非评审员的紧上限，
    避免逐条回复被从中间切断。本函数同 `_make_research_judge` 范式：自身无 IO，IO 在注入的 provider.stream_chat 内。
    """
    from ..providers.base import Message      # 延迟导入，保持模块 import 期纯净
    partial = {}                              # {name: 已流式文本}——供 _run_reviewers_serial 超时时回捞（别丢成 "[]"）

    def review_fn(name, prompt):
        provider = provider_for(name)
        if provider is None:
            return "[]"                        # 没配该角色的模型 → 无意见，不阻断评审
        if name == MAIN:
            mt = main_max_tokens                      # 主模型逐轮回复放宽上限（更长、别被切断）
        else:
            # 评审员：预算跟着决策条数走，再顶到该模型自己的单次上限（超了会被 API 拒绝）
            mt = scale_review_budget(max_tokens, getattr(review_fn, "scope", 0),
                                     getattr(provider, "max_tokens", None))
        out, stop, err = [], "", ""
        partial[name] = ""
        for ev in provider.stream_chat([Message("user", prompt)], system=None,
                                       tools=[], max_tokens=mt):
            t = getattr(ev, "type", None)
            if t == "text":
                out.append(ev.text)
                partial[name] = "".join(out)       # 增量存：即便随后超时被打断，已打出的内容也不丢
                if on_delta:                       # 逐 token 推给前端讨论流（实时辩论）
                    try:
                        on_delta(name, ev.text)
                    except Exception:  # noqa: BLE001 — 推流故障不阻断评审
                        pass
            elif t == "error":
                # provider 把调用失败（401/400/订阅失效/网络）当事件吐出来。**原来这里没接**，
                # 于是这一路返回空字符串 → 前端那一栏空白、主模型收到「（本镜头无意见）」，
                # 整场评审看着正常跑完，实际少了一半的对冲视角。真机就是这么中招的
                # （包里带的 ARK key 有值但 CodingPlan 已过期 → 该镜头静默缺席）。
                err = (getattr(ev, "text", "") or "调用失败").strip()
            elif t == "done":
                stop = (getattr(ev, "meta", None) or {}).get("stop_reason", "")
        if err:                                    # 调用失败：**说出来**，别让缺席伪装成"没意见"
            who = "主模型" if name == MAIN else "本镜头"
            note = (f"\n\n_（⚠ {who}没能参与本轮：{err[:200]}。"
                    "这一路的意见**完全缺席**——别把沉默当「没问题」。"
                    "去「设置 → Provider」检查该模型的 API Key / 订阅是否有效，"
                    "或在顶部「模型 ▾」给这个角色换一个可用的模型档。）_")
            out.append(note)
            if on_delta:
                try:
                    on_delta(name, note)
                except Exception:  # noqa: BLE001
                    pass
        if stop in ("max_tokens", "length"):       # 达上限被截断：补可见提示（别让用户/主模型面对无声断句）
            if name == MAIN:
                # 主模型被截断比镜头严重一个量级：末尾 JSON 没了 = 本轮决定一条都不落地，必须说清楚。
                note = ("\n\n_（⚠ 主模型回复达输出上限被截断：结构化决策 JSON 可能不完整，"
                        "**本轮的决定可能没有生效**（决策状态保持原样）。请调高该模型档的 max_tokens，"
                        "或减少一次评审的决策条数后点 ↻ 重跑。）_")
            else:
                note = ("\n\n_（⚠ 本镜头输出达上限被截断：**排在后面的决策没有被评到**，"
                        "请把以上意见当作只覆盖了前一部分的**有偏子集**——没被提到 ≠ 没问题。"
                        "可在设置调高「评审结论上限」，或减少一次评审的决策条数。）_")
            out.append(note)
            if on_delta:
                try:
                    on_delta(name, note)
                except Exception:  # noqa: BLE001
                    pass
        result = "".join(out)
        partial[name] = result
        return result
    review_fn.partial = partial                # 暴露给 _run_reviewers_serial 超时回捞
    review_fn.base_max_tokens = max_tokens     # 暴露基线预算：run_review 据此把超时与预算成对伸缩
    return review_fn


# ── 评审会话状态机：api/前端驱动它（评审 → 逐条拍板 → 签字 → gate）──────────────
class DesignReviewSession:
    """一次方案评审的可驱动状态：持有 Decision 集，支持跑评审、用户逐条拍板 NeedUser、签字、查 gate。

    纯逻辑（review_fn 注入）。供 conversation/api 在规划模式下驱动；前端按其 gate()/consensus() 渲染。
    """

    def __init__(self, decisions, max_rounds: int = 3, timeout: int = REVIEW_TIMEOUT_S,
                 min_rounds: int = 1) -> None:
        self.decisions = list(decisions)
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.min_rounds = min_rounds       # 讨论轮下限：保证评审员至少基于主模型回复回应一轮（用户明确要）
        self.signed = False
        self.last_result = None

    @classmethod
    def from_proposal(cls, proposal_text: str, max_rounds: int = 3,
                      timeout: int = REVIEW_TIMEOUT_S, min_rounds: int = 1) -> "DesignReviewSession":
        """从模型 proposal 输出抽 Decision 列表建会话。"""
        return cls(parse_decisions(proposal_text), max_rounds, timeout, min_rounds)

    def review(self, review_fn, on_event=None, cancel=None) -> dict:
        """跑一整轮多角色评审（直到停止条件），更新决策集。返回 run_review 结果。

        on_event(kind, payload)：可选，逐轮进度回调（round_start / reviewer_done / main_reply_start /
        main_reply_done / converged），供 conversation 转成前端事件做实时分屏；缺省=不回调（纯逻辑/单测不受影响）。
        """
        res = run_review(self.decisions, review_fn, self.max_rounds,
                         timeout=self.timeout, on_event=on_event, min_rounds=self.min_rounds, cancel=cancel)
        self.decisions = res["decisions"]
        self.signed = False                    # 决策集变了 → 旧签字作废
        self.last_result = res
        return res

    def to_dict(self) -> dict:
        """序列化成可落库的纯 JSON（决策 + 签字 + 编排参数）。纯函数级，无 IO。

        `last_result` 里只有 `stop_reason` 值得留（rounds 快照是 set，且重启后再无用处）；
        共识文档由 `render_consensus(decisions)` 现算，不存冗余副本。
        """
        return {
            "decisions": [
                {"id": d.id, "title": d.title, "current_choice": d.current_choice,
                 "alternatives": d.alternatives, "rationale": d.rationale,
                 "status": d.status, "blocking": list(d.blocking)}
                for d in self.decisions
            ],
            "signed": bool(self.signed),
            "max_rounds": self.max_rounds,
            "min_rounds": self.min_rounds,
            "timeout": self.timeout,
            "stop_reason": (self.last_result or {}).get("stop_reason", ""),
            "reviewed": self.last_result is not None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DesignReviewSession | None":
        """反序列化；数据缺/坏/没有决策一律返回 None（当作没评审过，别让会话打不开）。"""
        if not isinstance(data, dict):
            return None
        raw = data.get("decisions")
        if not isinstance(raw, list) or not raw:
            return None
        decisions = [_coerce_decision(o) for o in raw if isinstance(o, dict)]
        if not decisions:
            return None
        s = cls(decisions,
                int(data.get("max_rounds") or 3),
                int(data.get("timeout") or REVIEW_TIMEOUT_S),
                int(data.get("min_rounds") or 1))
        s.signed = bool(data.get("signed"))
        if data.get("reviewed"):
            # 只回填"评过 + 停在哪"，不伪造 rounds 快照——前端只读这两项（reviewed/stop_reason）。
            s.last_result = {"stop_reason": data.get("stop_reason") or "", "restored": True}
        return s

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
