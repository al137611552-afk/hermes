"""Agent 主循环：plan → act → observe。

每一轮调用 provider 跑一次模型；若模型要求调用工具，则（危险工具过权限
gate 后）执行，把 tool_result 回灌，再进入下一轮；直到模型不再调用工具或
达到 max_steps 上限。

事件通过注入的 emit(event, data) 回调推给上层（bridge -> 前端）：
- chunk        文本增量（data: str）
- tool_use     模型发起工具调用（data: {id, name, input}）
- tool_result  工具执行结果（data: {id, name, ok, output}）
- error        出错（data: str）
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..providers import BaseProvider, Message
from ..tools import ToolError, ToolOutput, ToolRegistry
from .contract import NUDGE_BROWSE, NUDGE_LOGIN, NUDGE_STUCK, Need
from .gate import PermissionGate
from .world_state import WorldState, fingerprint

# 工具被用户拒绝时回灌给模型的提示
_DENIED = "用户拒绝了本次操作。请不要重试该操作，可改用其它方式或询问用户。"

# 情境自启（反复改不好 → 提示用 trace_run）：编辑类工具 + 失败信号标记
_EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit"})
_FAIL_MARKERS = ("Traceback", "AssertionError", "FAILED", "未通过", "失败",
                 "Error:", "error:", "Exception", "🧪")


def looks_failing(text: str) -> bool:
    """工具输出是否含失败信号（纯逻辑、启发式）。"""
    return any(m in (text or "") for m in _FAIL_MARKERS)


# 浏览类只读工具（逐个看文件/检索）——用于"大库里浏览太多还没用 search_code"的检测
_BROWSE_TOOLS = frozenset({"read_file", "list_dir", "grep_search", "glob_search", "code_outline"})
_BROWSE_NUDGE_AT = 6   # 大库里累计浏览这么多次还没用 search_code，就提示一次

# ── Need → 注入文案（块 A：单一"差距→注入"选择点，见 docs/adr/0014）──────────
#
# loop.py 的三个情境探测器负责从工具调用里**探测事实**并归到一个 Need；具体
# 「提示什么」统一由这里按 Need 选择。这样判断（探测）与做法（注入文案）分开，
# 后续要改某个 Need 的应对、或让 Policy 接管，只动这一处。文案与重构前逐字一致，
# 故行为等价。
def _nudge_injection(need: Need, **ctx) -> str:
    """按 Need 选注入文案（纯函数）。ctx 提供 PROGRESS_STALLED 所需的 path/count。"""
    if need is NUDGE_LOGIN:
        return ("[系统] 刚打开的页面是**登录墙**（需要登录才能看内容）。**必须**用 ask_user 工具暂停、"
                "提示用户在弹出的浏览器里登录后回复『继续』，等回复了再 browser_navigate 重开目标页继续。"
                "**严禁** browser_navigate 到 google / baidu / bing 等搜索引擎绕开登录——那不是用户要的、会被判为绕路。")
    if need is NUDGE_BROWSE:
        return (
            "[系统观察] 你已经逐个浏览了不少文件来找代码——这个项目较大，"
            "用 **search_code** 给一句意图描述（如「处理 X 的地方」「鉴权逻辑」）能一次拉到最相关的几段、"
            "比逐个 list/read/grep 省很多步。先 search_code 定位、再 read_file 看细节。"
        )
    if need is NUDGE_STUCK:
        return (
            f"[系统观察] 你已经第 {ctx['count']} 次修改 `{ctx['path']}`、而它仍在失败——"
            "反复改同一处通常是**没定位准、在盲改**。先停下别再猜：用 **trace_run** 跑一段调用相关函数的"
            "驱动代码，直接看每一步的中间值，定位到底是哪一步 / 哪个值算错了，再针对性修。"
        )
    raise ValueError(f"无对应注入文案的 Need: {need}")


# 登录墙检测：浏览器结果里这些**强信号**才判为"被登录墙挡住"（避免误伤"页头有个登录按钮但正文可读"的页）
_LOGIN_WALL_RE = __import__("re").compile(
    r"请先?登[录入]|登[录入]后(?:查看|继续|可见)|需要登[录入]|扫码登[录入]|未登[录入]|"
    r"(?:please |you (?:need|must) to? )?(?:sign|log)\s?in(?: to (?:continue|view|see))?|"
    r"login required|/login|/signin|/passport|accounts?\.\w+/(?:login|signin)", __import__("re").I)


def detect_login_wall(calls, out_by_id: dict, state: dict) -> "str | None":
    """浏览器穿透下，某次 browser_* 结果像登录墙时，返回一条强制指令（纯逻辑，每轮最多提一次）：
    必须用 ask_user 让用户登录、禁止换 google/baidu 等搜索引擎绕开。"""
    if state.get("nudged"):
        return None
    for c in calls:
        name = getattr(c, "name", "")
        if name.split("__", 1)[-1].startswith("browser_"):
            if _LOGIN_WALL_RE.search(str(out_by_id.get(getattr(c, "id", None), ""))):
                state["nudged"] = True
                return _nudge_injection(NUDGE_LOGIN)
    return None


def detect_browse_nudge(calls, state: dict, enabled: bool, search_available: bool) -> "str | None":
    """检测「大项目里逐个浏览很多文件、却没用 search_code」，提示按意图检索（纯逻辑，原地更新 state）。

    state: {"browse": int, "used_search": bool, "nudged": bool}。每轮对话只提示一次。
    """
    if not enabled or not search_available or state.get("nudged"):
        # 仍要记录 used_search，避免关掉再开时误判；但不提示
        for c in calls:
            if getattr(c, "name", "") == "search_code":
                state["used_search"] = True
        return None
    for c in calls:
        name = getattr(c, "name", "")
        if name == "search_code":
            state["used_search"] = True
        elif name in _BROWSE_TOOLS:
            state["browse"] = state.get("browse", 0) + 1
    if not state.get("used_search") and state.get("browse", 0) >= _BROWSE_NUDGE_AT:
        state["nudged"] = True
        return _nudge_injection(NUDGE_BROWSE)
    return None


def detect_stuck_edit(calls, out_by_id: dict, edit_counts: dict, nudged: set,
                      threshold: int, trace_available: bool) -> "str | None":
    """检测「同一文件反复改且仍在失败」，返回一次性提示文本（纯逻辑，原地更新 edit_counts/nudged）。

    触发条件：某编辑类工具命中同一 path 累计 ≥ threshold 次，且**本步有失败信号**，且该 path 还没提示过，
    且环境里有 trace_run 可用。每个 path 只提示一次，避免反复打扰。
    """
    if threshold <= 0 or not trace_available:
        return None
    step_failing = any(looks_failing(str(v)) for v in out_by_id.values())
    for c in calls:
        if getattr(c, "name", "") not in _EDIT_TOOLS:
            continue
        path = (getattr(c, "input", None) or {}).get("path", "")
        if not path:
            continue
        edit_counts[path] = edit_counts.get(path, 0) + 1
        if edit_counts[path] >= threshold and step_failing and path not in nudged:
            nudged.add(path)
            return _nudge_injection(NUDGE_STUCK, path=path, count=edit_counts[path])
    return None


def _short_args(params) -> str:
    """把工具入参压成一行简短描述，用于死路提示文案。"""
    p = params or {}
    for k in ("command", "path", "file_path", "pattern", "query", "url", "name"):
        v = p.get(k)
        if v:
            s = " ".join(str(v).split())
            return s[:80] + ("…" if len(s) > 80 else "")
    return ""


def _latest_user_text(messages) -> str:
    """取最后一条 user 消息的纯文本，作为本轮"用户目标"（块H3a 裁判的相关性基准）。

    content 可能是 str，或块列表（text/tool_result/image 混合）——只抽 text，跳过工具结果块。
    """
    for m in reversed(messages or []):
        if getattr(m, "role", None) != "user":
            continue
        c = getattr(m, "content", None)
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and not b.get("tool_use_id"):
                    t = b.get("text", "")
                    if t and not t.startswith("[用户追加]") and not t.startswith("[系统"):
                        parts.append(t)
            if parts:
                return " ".join(parts).strip()
    return ""


def detect_repeated_failure(calls, out_by_id, world, failure_memory, nudged_fps, threshold=2):
    """块E：同一条路（指纹）反复**非瞬时**失败 → 注入"此路已 N 次不通"事实，促模型换思路。

    瞬时 IO 失败**不计**（那是 block D 自动重试的活，不是死路）。每条失败记入 WorldState
    （本会话）+ FailureMemory（跨会话持久）；命中阈值（本会话累计 ≥threshold 或跨会话已知死路）
    且本指纹本轮未提示过 → 返回注入文案（事实，非指令）。仿现有 detector：探测+记录+返回。
    """
    from .taxonomy import ErrorClass
    transient = ErrorClass.TRANSIENT_IO.value
    for c in calls:
        text = out_by_id.get(c.id, "") or ""
        _ev, classes = AgentLoop._assess(c.name, text, True, getattr(c, "input", None))
        nontransient = [getattr(x, "value", x) for x in classes
                        if getattr(x, "value", x) != transient]
        if not nontransient:
            continue  # 成功 / 纯瞬时 → 不是死路
        fp = fingerprint(c.name, getattr(c, "input", None))
        n = world.record_failure(fp, nontransient, detail=text[:200])
        cross = None
        if failure_memory is not None:
            try:
                failure_memory.record(fp, nontransient, detail=text[:200])
                cross = failure_memory.known_deadend(fp, threshold)
            except Exception:  # noqa: BLE001 — 记忆故障绝不影响主循环
                cross = None
        if (n >= threshold or cross is not None) and fp not in nudged_fps:
            nudged_fps.add(fp)
            total = cross[0] if cross else n
            dom = cross[1] if cross else nontransient[0]
            return (f"[系统观察] 这条路（{c.name}：{_short_args(getattr(c, 'input', None))}）"
                    f"已累计 {total} 次以「{dom}」失败。重复同样的做法大概率仍失败——"
                    f"请换一条思路（不同命令/参数/工具，或先排查根因），不要再原样重试。")
    return None


def detect_low_quality_research(calls, out_by_id, nudged_queries, max_nudges=1):
    """块H2：联网搜索**返回了但不达标**（如结果无一在预算内）→ 注入"换词/换源重搜"事实，促模型重搜。

    判据 = ResearchEvaluator 产出的 blocker `issues`（当前=预算约束未满足，可证伪的硬事实）。
    per-query 计数封顶（max_nudges）防同一搜索被无限催重搜；模型若换了关键词=新 query=另起计数。
    **喂事实而非硬拦截**（同块E 死路提示）：只把"这次不达标"作为事实回灌，重搜与否由模型定。
    """
    from .evaluators import evaluate
    for c in calls:
        if getattr(c, "name", "") != "web_search":
            continue
        params = getattr(c, "input", None)
        text = out_by_id.get(getattr(c, "id", None), "")
        try:
            ev = evaluate("web_search", text, params if isinstance(params, dict) else None)
        except Exception:  # noqa: BLE001 — 评估故障绝不影响主循环
            continue
        if ev is None or not ev.issues:
            continue
        q = ""
        if isinstance(params, dict):
            q = str(params.get("query") or params.get("q") or "")
        key = q.strip().lower()
        n = nudged_queries.get(key, 0)
        if n >= max(1, max_nudges):
            continue
        nudged_queries[key] = n + 1
        return ("[系统观察] 这次搜索返回了结果，但**质量不达标**：" + ev.issues[0] +
                "。请不要止步于此——换更精准的关键词，或改用别的数据源/检索方式（如浏览器直通、"
                "换平台）重搜一次，尽量满足用户给的约束（预算/品类等）；"
                "**别凭训练记忆直接编**——这类约束需要实时来源核对。")
    return None


def detect_offtarget_research(calls, out_by_id, goal, judge_fn, nudged_queries,
                             max_nudges=1, images_by_id=None):
    """块H3a：web_search 结果经**模型裁判**判语义相关性（"夏季"≠秋冬款等），不对题→提示重搜。

    在 H2 的预算正则之后跑（H2 已就该 query 提示过则跳过，避免重复）。裁判故障/对题 → 不拦。
    per-query 封顶同 H2。multimodal 预留 images_by_id（H3b 接图后用）。
    """
    from .judge import judge_research
    for c in calls:
        if getattr(c, "name", "") != "web_search":
            continue
        params = getattr(c, "input", None)
        text = out_by_id.get(getattr(c, "id", None), "")
        if not (text and text.strip()):
            continue
        q = ""
        if isinstance(params, dict):
            q = str(params.get("query") or params.get("q") or "")
        key = q.strip().lower()
        if nudged_queries.get(key, 0) >= max(1, max_nudges):
            continue
        imgs = (images_by_id or {}).get(getattr(c, "id", None))
        v = judge_research(goal, text, judge_fn, images=imgs)
        if v.on_target:
            continue
        nudged_queries[key] = nudged_queries.get(key, 0) + 1
        reasons = "；".join(v.off[:3]) if v.off else "多数结果与目标的关键限定不符"
        # 块H3c：三态。**部分污染**（有可萃取的相关少数）→ 挑出来用、丢无关的，**别整批丢、别凭记忆编**；
        # 不再说"请不要采用这些结果"那种诱导整批丢弃→退回训练数据的话。
        if v.salvageable:
            keep = "；".join(v.use[:4])
            return ("[系统观察] 这次结果**部分有效**：可采用并标注来源的有——" + keep +
                    "；无关的（" + reasons + "）丢弃即可。**别因为掺了无关项就整批丢、更别凭训练记忆硬编**；"
                    "用上面这些有效内容作答，不足再补搜。")
        # **基本是垃圾**（一条都不相关）→ 才换词/换源重搜；明确禁止凭记忆顶替。
        sug = v.suggestion or "换更精准的关键词，或改用别的数据源/检索方式重搜一次"
        return ("[系统观察] 这次搜索结果**基本不对题**：" + reasons +
                "。" + sug + "，尽量贴合用户目标（季节/品类/性别/时效等）；"
                "**别凭训练记忆直接作答**——这类问题需要实时来源。")
    return None


def detect_offtarget_answer(goal, answer_text, images, judge_fn, max_images=6):
    """块H3b：对**带图的最终答案**做多模态裁判——把答案配图（截图/浏览器图块，模型本轮真"看过"
    的像素）连同用户目标一起喂模型，判图文是否对题（如"夏季睡衣"答案配的却是冬季厚款图）。

    不对题 → 返回一条让模型据图重筛/重搜的提示；对题/无图/无目标/无答案 → None。
    judge_fn 故障由 judge_research 内 try 包死，一律放行不拦（绝不因裁判出错卡住收尾）。
    """
    from .judge import judge_research
    imgs = list(images or [])[-max(1, max_images):]
    if not imgs or not (goal and goal.strip()) or not (answer_text and answer_text.strip()):
        return None
    v = judge_research(goal, answer_text, judge_fn, images=imgs)
    if v.on_target:
        return None
    reasons = "；".join(v.off[:3]) if v.off else "配图与目标的关键限定（季节/款式/品类等）对不上"
    sug = v.suggestion or "据图重新筛选符合目标的项，必要时换词/换源重搜后再作答"
    return ("[系统观察] 你这版答案**配图与目标不符**：" + reasons +
            "。请不要就这么给——" + sug + "。")


# 块H3c：时效敏感信号（需实时数据的问句）+ 已声明过时/已引用来源的标志（纯正则，零成本，不调模型）
_FRESH_RE = re.compile(
    r"最新|今年|去年|明年|实时|现在|目前|最近|当前|今天|本月|今|价格|多少钱|报价|售价|股价|汇率|"
    r"行情|榜单|排行|销量|促销|优惠|新款|发布|上市|20\d\d|618|双11|双十一|黑五")
_DISCLAIM_RE = re.compile(
    r"可能(已)?过时|以实时为准|以官方为准|基于(我的)?训练|截至我所知|训练数据|无法联网|"
    r"可能已变化|建议(自行)?核实|请以实际|仅供参考|数据可能不是最新")
_CITED_RE = re.compile(r"https?://|】\(http|来源[:：]|引用[:：]|据(.{0,8})报道")


def detect_ungrounded_answer(goal, answer_text, did_research):
    """块H3c：接地/时效闸。本轮**做过搜索**、问题**需要实时数据**，但最终答案**既没引用搜到的来源、
    也没声明可能过时**——大概率是放弃搜索内容、凭训练记忆硬答（→ 易过时、白搜）。返回一条要求
    "基于搜到的有效内容作答并标注来源，没有就明确声明过时"的提示；否则 None。

    纯正则、零模型成本。**保守触发**：只在"时效敏感 + 做过搜索 + 既无引用又无声明"三者同时成立时。
    模型若已引用来源（接地）或已声明过时（诚实），都算过关，不打扰——避免误杀正当的稳定知识兜底。
    """
    if not did_research or not (goal and goal.strip()) or not (answer_text and answer_text.strip()):
        return None
    if not _FRESH_RE.search(goal):
        return None
    if _CITED_RE.search(answer_text) or _DISCLAIM_RE.search(answer_text):
        return None
    return ("[系统观察] 这个问题需要**实时数据**（价格/最新/榜单等），但你的答案没有引用任何搜到的来源、"
            "像是凭训练记忆给出——这很容易**过时**，也浪费了本轮搜到的有效内容。请**基于本轮搜到的有效条目"
            "作答并标注来源**；若确实没有可靠来源，就**明确声明**「以下基于训练知识、可能已过时，建议以实时为准」，"
            "不要让人误以为是当前准确信息。")


# ── Novelty / Progress（确定性事实，无模型、无分数）+ 换源策略阶梯 ─────────────
#
# 见 docs/adr/0018。重搜空转的根因之一：换关键词泛搜，但搜索引擎排序不变 → 反复
# 召回同一批站点、零新信息。Novelty = 本轮是否带来**新域名**（可证伪、去重事实，
# 非 expected_gain 那种模型臆测的浮点分）。Progress 据此二态：
#   · NEW_INFORMATION（有新域名）→ 还值得换词再搜（沿用 H2/H3a 文案）
#   · NO_PROGRESS（零新域名）   → 别再换词泛搜，按阶梯换**检索策略/来源**
# 严守 ADR 0014：探测只产事实（域名差集），是否换/怎么换由这层文案 + 全局预算决定。
_DOMAIN_RE = re.compile(r"https?://(?:www\.)?([a-z0-9.\-]+\.[a-z]{2,})", re.I)


def extract_domains(text: str) -> "set[str]":
    """从搜索结果文本里抽出现的域名（去 www.、小写），作为 Novelty 的确定性信号源。"""
    return {m.group(1).lower().rstrip(".") for m in _DOMAIN_RE.finditer(text or "")}


# 换源阶梯：泛搜不奏效时**逐级升级检索方式**（不是再换关键词）。先焊死这条具体阶梯，
# 等 Vision/Browser 等第二个消费者真要复用时再提炼通用 Search Policy（避免预先抽象）。
_SEARCH_STRATEGIES = (
    ("site_filter",
     "改用**站内/官方源定向检索**：在 query 里加 `site:` 限定到权威站点"
     "（如 `site:` 官网域名、`site:github.com`、知名垂直站/榜单站），"
     "或直接搜「官方 公告/文档/报价」，绕开泛搜噪声。"),
    ("browser",
     "改用**浏览器直通**：用 browser_navigate 打开权威页面（官网/榜单页/电商详情页）"
     "直接读取，而不是反复泛搜——搜索引擎排序对这个问题已被证明不奏效。"),
    ("ask_user",
     "**停止盲搜，改用 ask_user** 向用户确认更精确的限定"
     "（具体型号/平台/时间范围/可信来源），拿到后再定向检索。"),
)


def switch_strategy_nudge(step: int) -> "str | None":
    """块H（换源策略）：连续重搜**零新信息**（NO_PROGRESS）→ 按阶梯换检索方式/来源。

    step 从 0 起逐级升级；超出阶梯返回 None（交由全局重搜预算的止血出口收尾）。
    纯函数、零模型成本。文案明确「换的是检索方式、不是再换关键词」。
    """
    if step < 0 or step >= len(_SEARCH_STRATEGIES):
        return None
    _name, how = _SEARCH_STRATEGIES[step]
    return ("[系统观察] 这一轮重搜**没带来任何新来源**（还是之前那几个站点）——"
            "继续用同样的方式泛搜大概率仍原地打转。" + how +
            "（换的是**检索方式/来源**，不是再换几个关键词。）")


class AgentLoop:
    def __init__(
        self,
        provider: BaseProvider,
        registry: ToolRegistry,
        gate: PermissionGate,
        *,
        max_steps: int = 25,
        hook_runner=None,
        stuck_threshold: int = 0,
        browse_nudge: bool = False,
        auto_retry: bool = False,
        retry_max_attempts: int = 2,
        retry_backoff_base: float = 0.5,
        failure_memory=None,
        deadend_threshold: int = 2,
        research_refine: bool = False,
        research_refine_max: int = 1,
        research_max_rounds: int = 3,
        research_judge=None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.gate = gate
        self.max_steps = max_steps
        self.hook_runner = hook_runner  # 可编程 hooks（PreToolUse/PostToolUse）；None=无
        self.stuck_threshold = stuck_threshold  # 情境自启：反复改同一文件失败→提示 trace_run；0=关
        self.browse_nudge = browse_nudge        # 情境自启：大库里浏览太多→提示 search_code（按工作区规模启用）
        self.auto_retry = auto_retry            # 块D：瞬时 IO 失败自动退避重试（工具调用级）
        self.retry_max_attempts = retry_max_attempts
        self.retry_backoff_base = retry_backoff_base
        self.failure_memory = failure_memory    # 块E：跨会话死路记忆（FailureMemory 实例）；None=关
        self.deadend_threshold = deadend_threshold  # 同一条路累计失败 ≥ 此值 → 提示换思路
        self.research_refine = research_refine   # 块H2：联网搜索不达标→提示重搜；False=关
        self.research_refine_max = research_refine_max  # 同一 query 最多催重搜几次（防无限）
        self.research_max_rounds = research_max_rounds   # **整轮**催重搜总预算；达上限→停搜、综合作答（防换词无限重搜）
        self.research_judge = research_judge     # 块H3a：模型裁判 judge_fn(prompt,images)->str；None=只用H1/H2正则
        import time as _t
        self._sleep = _t.sleep                  # 退避用；测试可替换为 no-op

    def run(
        self,
        messages: list[Message],
        system: str | None,
        emit: Callable[[str, object], None],
        cancel: threading.Event | None = None,
        take_injects=None,
    ) -> list[Message]:
        """跑完一整轮对话（可能含多步工具调用）。

        messages 会被原地追加 assistant 与 tool_result 消息；返回同一列表，
        供 bridge 写回会话历史。

        cancel：可选的取消标志，在每一回合开始前检查；置位则立即停止后续回合
        （不打断当前回合内已在进行的模型流，回合间生效，见 FR-8.3）。

        take_injects：可选 `() -> list[str]` 回调，返回并清空"用户在执行中追加的补充消息"
        （steering）。每次工具往返回灌时拉取，把补充附进同一条 user 消息——模型下一轮即看到
        「工具结果 + 用户补充」，可据此重新评估、调整当前任务方向，而非等任务做完再当新事处理。
        """
        tools = self.registry.to_schemas()
        # 用量累计（FR-11.8）：跨步累加 token，记步数，回合末发 usage 事件。
        total = {"input": 0, "output": 0, "cache_read": 0}
        steps = 0
        warned = False
        self.hit_max_steps = False   # 本轮是否撞步数上限（供委派标注"子任务未完成"）
        edit_counts: dict[str, int] = {}  # 情境自启：本轮各文件被编辑次数
        nudged: set[str] = set()          # 已提示过 trace_run 的文件（每文件只提一次）
        browse_state: dict = {}           # 情境自启：浏览计数 / 是否用过 search_code / 是否已提示
        login_state: dict = {}            # 登录墙：本轮是否已强制提示过"用 ask_user 登录、别换搜索引擎"
        world = WorldState()              # 块E：本轮世界状态（Need 历史 / 死路计数）
        deadend_fps: set[str] = set()     # 块E：已就某指纹提示过换思路（每路一次）
        research_nudged: dict = {}        # 块H2：已就某搜索 query 催过重搜的计数（每 query 封顶）
        research_nudge_count = 0           # 块H2/H3a：**整轮**催重搜总次数（全局预算，防换词绕过 per-query cap）
        research_stopped = False           # 全局预算用尽 → 已发"停搜、综合作答"出口（每轮一次）
        seen_domains: set[str] = set()     # 换源策略：本轮搜过的全部域名（Novelty 去重事实，判"是否带来新来源"）
        search_strategy_step = 0           # 换源策略：阶梯当前级（NO_PROGRESS 时逐级 site→browser→ask_user）
        research_goal = _latest_user_text(messages)  # 块H3a：用户目标（裁判判相关性的基准）
        seen_images: list[dict] = []      # 块H3b：本轮模型"看过"的配图块（截图/浏览器图），供终局多模态裁判
        did_research = False              # 块H3b：本轮是否做过研究（web_search/browser_*）——给配图判定划范围
        answer_refined = False            # 块H3b：终局带图答案已据图重判一次（每轮封顶，防无限）
        _names = self.registry.names() if hasattr(self.registry, "names") else []
        browser_present = any(n.split("__", 1)[-1].startswith("browser_") for n in _names)

        for _ in range(self.max_steps):
            if cancel is not None and cancel.is_set():
                break  # 收到取消：停在回合边界，已追加的消息照常返回/落库
            steps += 1
            # 步数接近上限时预警一次（长任务"在推进还是打转"可感知）
            if not warned and self.max_steps >= 5 and steps >= int(self.max_steps * 0.8):
                warned = True
                emit("step_warning", {"steps": steps, "max_steps": self.max_steps})
            assistant_text = ""
            calls = []
            errored = False
            cancelled = False
            stop_reason = None

            for ev in self.provider.stream_chat(messages, system=system, tools=tools):
                if cancel is not None and cancel.is_set():  # 立即响应停止：中断流式（对标主流，不等回合结束）
                    cancelled = True
                    break
                if ev.type == "text":
                    assistant_text += ev.text
                    emit("chunk", ev.text)
                elif ev.type == "thinking":
                    emit("thinking", ev.text)  # 仅展示，不计入答案、不持久化
                elif ev.type == "tool_use":
                    calls.append(ev.meta["call"])
                elif ev.type == "error":
                    emit("error", ev.text)
                    errored = True
                    break
                elif ev.type == "done":
                    stop_reason = ev.meta.get("stop_reason")
                    u = ev.meta.get("usage")
                    if u:
                        for k in total:
                            total[k] += u.get(k, 0) or 0
                    break

            if cancelled:  # 流式被停止打断：保留已输出的部分文本，不执行本轮残缺工具调用
                if assistant_text.strip():
                    messages.append(Message("assistant", assistant_text))
                break
            if errored:
                break

            # 输出撞到 max_tokens 上限被截断：此时 tool_use 的入参（如 write_file 的
            # content）很可能不完整，执行它会写出空/残缺文件，模型见状又重试 -> 死循环。
            # 故记下已生成文本、明确报错并停止，不执行被截断的工具调用。
            if stop_reason in ("max_tokens", "length"):
                if assistant_text.strip():
                    messages.append(Message("assistant", assistant_text))
                emit("error",
                     f"模型输出达到 max_tokens 上限被截断（stop_reason={stop_reason}），"
                     "已停止以避免执行不完整的工具调用（如写出空文件）。"
                     "请在 config.yaml 调高该模型的 max_tokens，或让它分步/分块写入。")
                break

            if not calls:
                # 模型不再调用工具：本轮结束。终局两道答案级闸（每轮最多触发一次重答，answer_refined 封顶）：
                #  · 块H3b：带图答案多模态裁判——配图与目标不符（如"夏季"配冬季款图）→ 据图重选。
                #  · 块H3c：接地/时效闸——需实时数据却凭训练记忆硬答（无引用无声明）→ 据搜到内容重答或声明过时。
                # 整段 try 包死：裁判/检测故障绝不影响正常收尾。
                if (self.research_refine and research_goal and assistant_text.strip()
                        and did_research and not answer_refined):
                    nudge = None
                    try:
                        if self.research_judge is not None and seen_images:
                            nudge = detect_offtarget_answer(
                                research_goal, assistant_text, seen_images, self.research_judge)
                        if nudge is None:
                            nudge = detect_ungrounded_answer(
                                research_goal, assistant_text, did_research)
                    except Exception:  # noqa: BLE001
                        nudge = None
                    if nudge:
                        answer_refined = True
                        messages.append(Message("assistant", assistant_text))
                        messages.append(Message("user", [{"type": "text", "text": nudge}]))
                        emit("research_hint", {"text": nudge})
                        continue
                messages.append(Message("assistant", assistant_text))
                break

            # 1) 记录 assistant 这轮的 text + tool_use blocks
            blocks: list[dict] = []
            if assistant_text.strip():
                blocks.append({"type": "text", "text": assistant_text})
            for c in calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
            messages.append(Message("assistant", blocks))

            # 2) 执行工具，收集 tool_result blocks（按原调用顺序组装回灌）。
            #    同回合多个 parallel_safe 工具（目前=delegate）并发执行（FR-10.5）；
            #    富内容块（如截图 image）单独收集，作为并列块追加到同一条 user 消息
            #    （部分端点不解析 tool_result 内嵌图片，见 ToolOutput / ADR-0010）。
            results, extra_blocks = self._exec_calls(calls, emit)

            # 块H3b：累积本轮模型真"看过"的配图（截图/浏览器图块）+ 标记是否做过研究——
            # 供本轮收尾时对"带图答案"做一次多模态相关性裁判（范围限研究/购物，避免误扰编程截图）。
            if not did_research:
                did_research = any(
                    getattr(c, "name", "") == "web_search"
                    or getattr(c, "name", "").split("__", 1)[-1].startswith("browser_")
                    for c in calls)
            seen_images.extend(b for b in extra_blocks if b.get("type") == "image")

            # 3) tool_result（+ 富内容并列块）作为 user 消息回灌，进入下一轮。
            #    若用户在执行中追加了补充（steering），附进**同一条** user 消息——既让模型下一轮
            #    立刻看到「工具结果 + 用户补充」并据此调整，又不破坏 user/assistant 交替。
            inject_blocks: list[dict] = []
            if take_injects is not None:
                for t in take_injects():
                    if t and t.strip():
                        inject_blocks.append({"type": "text", "text": f"[用户追加] {t.strip()}"})
            # 情境自启：反复改同一文件且仍失败 → 自动提示用 trace_run 看证据（不再盲改）
            if self.stuck_threshold > 0:
                out_by_id = {r["tool_use_id"]: r.get("content", "") for r in results}
                nudge = detect_stuck_edit(
                    calls, out_by_id, edit_counts, nudged,
                    self.stuck_threshold, "trace_run" in self.registry.names())
                if nudge:
                    inject_blocks.append({"type": "text", "text": nudge})
                    emit("stuck_hint", {"text": nudge})
            # 情境自启：大库里逐个浏览很多文件还没用 search_code → 提示按意图检索
            if self.browse_nudge:
                bn = detect_browse_nudge(calls, browse_state, True,
                                         "search_code" in self.registry.names())
                if bn:
                    inject_blocks.append({"type": "text", "text": bn})
                    emit("search_hint", {"text": bn})
            # 浏览器穿透下撞登录墙 → 当场强制注入：必须 ask_user 让用户登录，禁止换搜索引擎绕开
            # （静态 directive 压不住"绕去 google/baidu"，关键时刻硬怼更可靠）
            if browser_present:
                out_by_id = {r["tool_use_id"]: r.get("content", "") for r in results}
                lw = detect_login_wall(calls, out_by_id, login_state)
                if lw:
                    inject_blocks.append({"type": "text", "text": lw})
                    emit("login_hint", {"text": lw})
            # 块E：同一条路反复非瞬时失败 → 提示换思路（死路记忆，跨会话累积）。纯观测+注入，
            # 整段 try/except 包死：记忆/分类故障绝不影响工具结果回灌。
            if self.failure_memory is not None:
                try:
                    out_by_id = {r["tool_use_id"]: r.get("content", "") for r in results}
                    df = detect_repeated_failure(calls, out_by_id, world, self.failure_memory,
                                                 deadend_fps, self.deadend_threshold)
                    if df:
                        inject_blocks.append({"type": "text", "text": df})
                        emit("deadend_hint", {"text": df})
                except Exception:  # noqa: BLE001
                    pass
            # 块H2/H3a：联网搜索不达标/不对题 → 提示重搜。**全局预算**封顶（research_max_rounds）：
            # per-query cap 会被"换关键词"绕过（每换个说法=新 key），故再加一道**整轮总预算**——
            # 累计催重搜达上限后，**翻面**：不再催搜，强制"停搜、用现有最相关内容综合作答+声明局限"
            # 一次性出口（防无限重搜→1500s 交白卷）。纯观测+注入、try 包死。
            if self.research_refine:
                try:
                    searched_calls = [c for c in calls if getattr(c, "name", "") == "web_search"]
                    if searched_calls and not research_stopped:
                        out_by_id = {r["tool_use_id"]: r.get("content", "") for r in results}
                        # Novelty/Progress（确定性事实）：本轮搜索带来了**新域名**吗？
                        round_text = " ".join(
                            str(out_by_id.get(getattr(c, "id", None), "")) for c in searched_calls)
                        new_domains = extract_domains(round_text) - seen_domains
                        seen_domains |= extract_domains(round_text)
                        if research_nudge_count >= max(1, self.research_max_rounds):
                            # 预算用尽：止血出口——停搜、萃取现有、声明局限（贯彻 H3c"优先萃取/声明，不空转"）
                            research_stopped = True
                            rq = ("[系统观察] 这个问题已**重搜多次仍不理想**，请**立即停止继续搜索**——"
                                  "用目前已搜到的最相关内容**直接综合作答**，挑出有用的部分；"
                                  "并明确声明「部分信息可能不全或非最新，建议以实时来源为准」。"
                                  "不要再重搜，也不要凭空编造。")
                            inject_blocks.append({"type": "text", "text": rq})
                            emit("research_hint", {"text": rq})
                        else:
                            rq = detect_low_quality_research(calls, out_by_id, research_nudged,
                                                             self.research_refine_max)
                            if rq is None and self.research_judge is not None and research_goal:
                                rq = detect_offtarget_research(
                                    calls, out_by_id, research_goal, self.research_judge,
                                    research_nudged, self.research_refine_max)
                            if rq:
                                research_nudge_count += 1
                                # Progress=NO_PROGRESS（本轮零新来源）→ 别再换词泛搜，按阶梯换检索方式/来源。
                                # NEW_INFORMATION（有新域名）→ 沿用 H2/H3a 的"换词重搜"文案（换词仍有进展）。
                                if not new_domains:
                                    switch = switch_strategy_nudge(search_strategy_step)
                                    if switch:
                                        search_strategy_step += 1
                                        rq = switch
                                inject_blocks.append({"type": "text", "text": rq})
                                emit("research_hint", {"text": rq})
                except Exception:  # noqa: BLE001
                    pass
            messages.append(Message("user", results + extra_blocks + inject_blocks))
        else:
            self.hit_max_steps = True
            # 撞步数上限：强制收尾一轮——禁用工具，让模型基于已收集信息立即给出总结/结论。
            # 否则 messages 最后一条是 tool_result、无任何文本产出，委派子任务回灌空摘要（FR-9.3）、
            # 长任务也只能裸退。把收尾指令并入最后那条 user 消息（撞上限时它一定是 tool_result），
            # 避免两条连续 user 破坏交替。
            if cancel is None or not cancel.is_set():
                hint = {"type": "text", "text": (
                    "[系统] 已达到步数上限，不能再调用任何工具。请立即基于上面已经收集到的信息，"
                    "给出尽可能有用的总结/结论：包含已获得的关键数据/发现，以及尚未完成的部分。不要再请求工具。"
                )}
                last = messages[-1] if messages else None
                if last is not None and last.role == "user" and isinstance(last.content, list):
                    last.content.append(hint)
                else:
                    messages.append(Message("user", [hint]))
                final_text = ""
                try:
                    for ev in self.provider.stream_chat(messages, system=system, tools=[]):
                        if ev.type == "text":
                            final_text += ev.text
                            emit("chunk", ev.text)
                        elif ev.type == "done":
                            u = ev.meta.get("usage")
                            if u:
                                for k in total:
                                    total[k] += u.get(k, 0) or 0
                            break
                        elif ev.type == "error":
                            break
                except Exception:  # noqa: BLE001 — 收尾失败不影响已有结果返回
                    final_text = ""
                if final_text.strip():
                    messages.append(Message("assistant", final_text))
            emit("error", f"已达到最大步数上限（{self.max_steps}），已基于已收集信息收尾。")

        # 回合末上报用量（FR-11.8）：tokens 全 0（端点没回传用量）则不发，避免噪音
        if total["input"] or total["output"]:
            emit("usage", {**total, "steps": steps, "max_steps": self.max_steps})
        return messages

    _PARALLEL_CAP = 4  # 同回合并发执行的 parallel_safe 调用上限（FR-10.5）

    def _exec_calls(self, calls, emit) -> tuple[list[dict], list[dict]]:
        """执行一个回合内的全部工具调用，返回 (tool_result 块, 富内容并列块)，均按原调用顺序。

        同回合出现 ≥2 个 parallel_safe 工具调用（目前只有 delegate）时丢进线程池并发跑
        （对标 Claude Code 一轮发多个 Task），其余工具保持原有的顺序执行语义。
        gate/emit/记忆库/进程表均已线程安全；子任务事件带 sub_id，前端多块并存。
        """
        parallel_ids: set[str] = set()
        if len(calls) > 1:
            for c in calls:
                try:
                    if getattr(self.registry.get(c.name), "parallel_safe", False):
                        parallel_ids.add(c.id)
                except ToolError:
                    pass
            if len(parallel_ids) < 2:  # 只有一个可并行的：没有并发收益，走顺序路径
                parallel_ids.clear()

        outputs: dict[str, tuple[str, bool, list[dict]]] = {}
        futures: dict[str, object] = {}
        executor = None
        if parallel_ids:
            executor = ThreadPoolExecutor(max_workers=min(self._PARALLEL_CAP, len(parallel_ids)))
            for c in calls:
                if c.id in parallel_ids:
                    emit("tool_use", {"id": c.id, "name": c.name, "input": c.input})
                    futures[c.id] = executor.submit(
                        self._exec_tool_with_retry, c.name, c.input, emit=emit, call=c)
        try:
            for c in calls:  # 串行组照旧（与并行组并发进行）
                if c.id in futures:
                    continue
                emit("tool_use", {"id": c.id, "name": c.name, "input": c.input})
                outputs[c.id] = self._exec_tool_with_retry(c.name, c.input, emit=emit, call=c)
                self._emit_result(emit, c, outputs[c.id])
            for c in calls:  # 收并行组结果（按原序等待/上报）
                if c.id in futures:
                    outputs[c.id] = futures[c.id].result()
                    self._emit_result(emit, c, outputs[c.id])
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        results: list[dict] = []
        extra_blocks: list[dict] = []
        for c in calls:
            output, _ok, blocks = outputs[c.id]
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": output})
            # diff 块仅供前端内联展示，不作并列块回灌模型（模型已知改了什么，回灌冗余+耗 token）
            extra_blocks.extend(b for b in blocks if b.get("type") != "diff")
        return results, extra_blocks

    @staticmethod
    def _assess(name: str, output: str, ok: bool, params=None) -> "tuple[dict | None, list]":
        """对一条工具结果做事实评估 + 错误分类（块B+C），返回 (eval_event|None, error_classes)。

        被 `_emit_result`（观测）与 `_exec_tool_with_retry`（决策）共用，确保两处口径一致。
        无适配 Evaluator 时：成功→不评估；失败→直接对原文跑分类（兜底覆盖硬错误）。
        """
        try:
            from .evaluators import evaluate, score
            from .taxonomy import ErrorClass, classify, classify_text
            _ev = evaluate(name, output, params)
            if _ev is not None:
                klasses = classify(_ev, output)
                return ({**_ev.as_event(), "score": score(_ev),
                         "error_classes": [c.value for c in klasses]}, klasses)
            if not ok:
                return (None, classify_text(output or ""))   # 硬错误无 Evaluator → 裸分类
        except Exception:
            pass
        return (None, [])

    @staticmethod
    def _emit_result(emit, call, out: tuple[str, bool, list[dict]]) -> None:
        output, ok, blocks = out
        ev = {"id": call.id, "name": call.name, "ok": ok, "output": output}
        # 块B/C 事实层：能评估的工具结果附结构化 Evaluation + score + error_classes。
        # 纯观测——不参与任何控制流（ADR 0014）。
        eval_event, _ = AgentLoop._assess(call.name, output, ok, getattr(call, "input", None))
        if eval_event is not None:
            ev["eval"] = eval_event
        img = next((b for b in blocks if b.get("type") == "image"), None)
        if img:  # 给前端一张缩略图
            src = img["source"]
            ev["image"] = f"data:{src['media_type']};base64,{src['data']}"
        d = next((b for b in blocks if b.get("type") == "diff"), None)
        if d:  # 写/编辑的本次 diff：内联展示在对话流（仅前端，不回灌模型）
            ev["diff"] = {"path": d["path"], "text": d["diff"]}
        emit("tool_result", ev)

    def _exec_tool_with_retry(self, name: str, params: dict, *, emit=None, call=None
                              ) -> tuple[str, bool, list[dict]]:
        """块D：在 `_exec_tool` 外包一层瞬时 IO 自动重试。

        失败分类（块B+C）命中 `TRANSIENT_IO` 且未撞上限 → 退避后重试，不打扰模型；
        其它失败 / 成功 → 原样返回。auto_retry 关时退化为直接 `_exec_tool`。
        重试事件经 `tool_retry` 上报（纯观测）。这是第一条 `Need→Decision` 硬规则的执行点。
        """
        out = self._exec_tool(name, params)
        if not self.auto_retry:
            return out
        from .policy import decide_retry
        attempts = 1
        while True:
            text, ok, _blocks = out
            _eval, classes = self._assess(name, text, ok, params)
            dec = decide_retry(classes, attempts,
                               max_attempts=self.retry_max_attempts,
                               backoff_base=self.retry_backoff_base)
            if dec is None:
                return out
            if emit is not None and call is not None:
                emit("tool_retry", {"id": getattr(call, "id", None), "name": name,
                                    "attempt": dec.attempt, "delay": dec.delay,
                                    "reason": dec.reason})
            self._sleep(dec.delay)
            out = self._exec_tool(name, params)
            attempts += 1

    def _exec_tool(self, name: str, params: dict) -> tuple[str, bool, list[dict]]:
        """执行单个工具，返回 (结果文本, 是否成功, 额外内容块)。危险工具先过权限 gate。

        普通工具返回 str -> 额外块为空；返回 ToolOutput 的工具（如截屏）-> 带 image 块。
        """
        try:
            tool = self.registry.get(name)
        except ToolError as e:
            return str(e), False, []

        # PreToolUse hooks（程序化守卫）：可拦截（退出码 2）或放行+警告（退出码 1）。
        pre_warn = None
        if self.hook_runner is not None:
            allowed, msg = self.hook_runner.pre(name, params)
            if not allowed:
                return (msg or "操作被 PreToolUse hook 拦截。"), False, []
            pre_warn = msg

        if tool.dangerous and not self.gate.confirm(name, params):
            return _DENIED, False, []

        try:
            out = tool.run(params)
        except ToolError as e:
            return str(e), False, []
        except Exception as e:  # noqa: BLE001 — 工具内部异常也回灌给模型
            return f"工具执行异常：{type(e).__name__}: {e}", False, []

        text, blocks = (out.text, out.blocks) if isinstance(out, ToolOutput) else (out, [])
        # PostToolUse hooks：把 hook stdout 追加到结果回灌模型（如 linter 诊断）。
        if self.hook_runner is not None:
            post = self.hook_runner.post(name, params, text if isinstance(text, str) else str(text))
            if post:
                text = f"{text}\n{post}"
        if pre_warn:  # 放行但带警告：警告并入结果
            text = f"{text}\n{pre_warn}"
        return text, True, blocks
