"""Agent 主循环：plan → act → observe。

每一轮调用 provider 跑一次模型；若模型要求调用工具，则（危险工具过权限
gate 后）执行，把 tool_result 回灌，再进入下一轮；直到模型不再调用工具
（= 自然收敛，正常出口）。max_steps 只是防跑飞的安全阈值（默认 200，非正常
任务边界）——正常任务到不了，撞到多半是原地打转，此时强制收尾一轮。

事件通过注入的 emit(event, data) 回调推给上层（bridge -> 前端）：
- chunk        文本增量（data: str）
- tool_use     模型发起工具调用（data: {id, name, input}）
- tool_result  工具执行结果（data: {id, name, ok, output}）
- error        出错（data: str）
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..context import estimate_tokens, estimate_tokens_text
from ..providers import BaseProvider, Message
from ..store.usage import provider_kind
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
        # 用 request_handoff 而不是 ask_user：登录要用户**动手**，不是让用户**拍板**——
        # 这正是 config.yaml 系统提示词里写死的分工。此处曾一直停在换手工具落地之前的写法
        # （FR-13.H/ADR 0023 之前），等于**最高权限的硬注入在教模型用错工具**，
        # 与系统提示词自相矛盾（同 v3.63 那条已知坑：加新工具要把提示词里指向旧做法的路标一起改）。
        return ("[系统] 刚打开的页面是**登录墙**（需要登录才能看内容）。这一步只有用户本人能做："
                "**必须**用 request_handoff 把控制权交还用户（说明为什么要换手、以及交回后你会怎么验证），"
                "等用户做完交回，再 browser_navigate 重开目标页确认。若重开后仍是登录页才再换手一次。"
                "**别自己破解滑块/验证码**（必输）；**严禁** browser_navigate 到 google / baidu / bing 等"
                "搜索引擎绕开登录——那不是用户要的、会被判为绕路。")
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


# 这些工具的 blocker 是**质量差距**，不是"这条路走不通"，故不进失败语料（ADR 0027 决策 11）。
# `web_search` 的 issues＝「返回了但不达标」（预算没满足），块H2 已有专门处置（催重搜/换源阶梯）；
# 而它**真正的硬失败**（超时/无结果）反倒不产 issues——走 `_EMPTY_MARKERS` 那条路只留 signals。
# 也就是说记进来的必然是质量差距，方向正好是反的。后果（块 V4 收割时照出）：
# 同一个 query 被当死路累计、与 research_hint 重复插话；且 taxonomy 没有"质量不达标"这一类，
# 全落进 unknown——6 条 unknown 的路里 5 条是它，把 Learning 的证据面整个带偏。
_QUALITY_ONLY_TOOLS = frozenset({"web_search"})


def detect_repeated_failure(calls, out_by_id, world, failure_memory, nudged_fps, threshold=2,
                            on_failure=None, workspace=None):
    """块E：同一条路（指纹）反复**非瞬时**失败 → 注入"此路已 N 次不通"事实，促模型换思路。

    瞬时 IO 失败**不计**（那是 block D 自动重试的活，不是死路）。每条失败记入 WorldState
    （本会话）+ FailureMemory（跨会话持久）；命中阈值（本会话累计 ≥threshold 或跨会话已知死路）
    且本指纹本轮未提示过 → 返回注入文案（事实，非指令）。仿现有 detector：探测+记录+返回。
    """
    from .taxonomy import ErrorClass
    transient = ErrorClass.TRANSIENT_IO.value
    for c in calls:
        if c.name in _QUALITY_ONLY_TOOLS:
            continue
        text = out_by_id.get(c.id, "") or ""
        _ev, classes = AgentLoop._assess(c.name, text, True, getattr(c, "input", None))
        nontransient = [getattr(x, "value", x) for x in classes
                        if getattr(x, "value", x) != transient]
        if not nontransient:
            continue  # 成功 / 纯瞬时 → 不是死路
        # workspace 传下去做路径归一：不归一则同一条路在不同工作区/不同评测跑里指纹不同，
        # 跨会话记忆与块G 聚合双双失真（ADR 0027 决策 2）。
        fp = fingerprint(c.name, getattr(c, "input", None), workspace)
        n = world.record_failure(fp, nontransient, detail=text[:200])
        cross = None
        if failure_memory is not None:
            try:
                # 先看历史再记：要知道"这次失败之前，这条路是不是已经known dead"。
                prior = failure_memory.known_deadend(fp, 1)
                # 记「做法」标签供块G 离线聚合。ADR-0014 明说不建决策引擎，模型的 Decision
                # 拿不到；能如实记的是两条：**用了哪个工具**（工具选择就是做法，且 fingerprint 是
                # 哈希、事后反查不出工具名），以及**是不是被提示过仍走同一条路**（RETRY_SAME 的强证据，
                # 也直接回答"提示到底有没有用"）。以前这个字段从来没传过，块G 的 evidence 恒为空。
                repeated = fp in nudged_fps or prior is not None
                label = c.name + ("|after_nudge" if repeated else "")
                failure_memory.record(fp, nontransient, decision=label, detail=text[:200])
                cross = failure_memory.known_deadend(fp, threshold)
                if on_failure is not None:
                    on_failure(fp, list(nontransient), label)   # 块G 消费/影子用，纯观测
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


# 撞 max_tokens 的转向指令（FR-10.2 补丁）：单次输出装不下一个大文件时，**别把问题甩给用户**
# ——用户不该为了画个原型图去研究 max_tokens 是什么。同"情境自启"范式（detect_stuck_edit 那套）：
# 检测到撞墙 → 给模型一条**可执行**的换姿势指令，让它自己分块写。劝两次仍撞才报错停下。
_TRUNCATION_NUDGES = (
    "[系统观察] 你上一次的输出**撞到单次 max_tokens 上限被截断**了（多半是想一次性写完一个大文件），"
    "那次工具调用的入参不完整、已被丢弃，什么都没写成。**别原样重发**，改成分块写：\n"
    "① 先 `write_file` 写第一块（骨架 / 前 1/3）；\n"
    "② 之后每块用 `write_file` 且 **`append: true`** 追加，一块控制在 ~150 行以内；\n"
    "③ 全部写完再读一遍确认完整。\n"
    "或者把大文件拆成几个小文件（如 HTML / CSS / JS 分开），单次输出量自然降下来。",
    "[系统观察] **又被截断了**。这次必须把内容拆得更碎：每块不超过 ~100 行、至少分 3 块，"
    "每块都用 `write_file` + `append: true`，写一块停一下再写下一块。别再尝试一次性输出整份内容。",
)


def truncation_nudge(n_done: int) -> "str | None":
    """撞 max_tokens 第 n 次时给模型的转向指令；劝过 len(_TRUNCATION_NUDGES) 次仍撞 → None（交由调用方报错停下）。"""
    if n_done < 0 or n_done >= len(_TRUNCATION_NUDGES):
        return None
    return _TRUNCATION_NUDGES[n_done]


class _LiveList(list):
    """append 时回调一次的消息列表——让"边跑边落库"不必在十来个 append 点各加一句。

    回合内的消息此前是**跑完才一次性落库**的，于是应用中途退出（崩溃/强关/断电）＝
    这一轮搜到的东西全部蒸发，库里一条都没有（2026-08-27 用户第三次报同一个现象）。
    按停止不受影响——那条路 loop 正常返回、照常落库；丢的只有**非正常退出**。

    用 list 子类而不是在每个 append 点加回调：`AgentLoop.run` 里有十来处 append，
    往后还会加，逐点埋钩子迟早漏一处，而漏掉的那处正好是"某类消息永远不落库"这种静默的坑。
    """
    __slots__ = ("_hook",)

    def __init__(self, items, hook) -> None:
        super().__init__(items)
        self._hook = hook

    def append(self, item) -> None:
        super().append(item)
        try:
            self._hook(item)
        except Exception:  # noqa: BLE001 — 落库失败绝不能把正在跑的这一轮带走
            pass


class AgentLoop:
    def __init__(
        self,
        provider: BaseProvider,
        registry: ToolRegistry,
        gate: PermissionGate,
        *,
        max_steps: int = 25,
        time_budget_s: float = 0,
        hook_runner=None,
        stuck_threshold: int = 0,
        browse_nudge: bool = False,
        auto_retry: bool = False,
        retry_max_attempts: int = 2,
        retry_backoff_base: float = 0.5,
        failure_memory=None,
        strategy_store=None,
        deadend_threshold: int = 2,
        research_refine: bool = False,
        research_refine_max: int = 1,
        research_max_rounds: int = 3,
        research_judge=None,
        tool_budget=None,
        workspace=None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.gate = gate
        self.max_steps = max_steps
        # 本轮墙钟上限（秒，0=不限）。治的是**木桶效应**：并行委派时主 Agent 要等最慢的那个子任务
        # （2026-08-26 真跑：三个 subagent_done 落在 537.6/629.7/646.4s，白等 109s）。
        # 撞上限**不是杀线程**——走与撞步数上限完全相同的收尾路径：禁掉工具、让它把已拿到的
        # 东西总结出来交回。半成品明确标注比硬杀掉一个跑了十分钟的子任务有用得多。
        self.time_budget_s = float(time_budget_s or 0)
        self.hook_runner = hook_runner  # 可编程 hooks（PreToolUse/PostToolUse）；None=无
        self.stuck_threshold = stuck_threshold  # 情境自启：反复改同一文件失败→提示 trace_run；0=关
        self.browse_nudge = browse_nudge        # 情境自启：大库里浏览太多→提示 search_code（按工作区规模启用）
        self.auto_retry = auto_retry            # 块D：瞬时 IO 失败自动退避重试（工具调用级）
        self.retry_max_attempts = retry_max_attempts
        self.retry_backoff_base = retry_backoff_base
        self.failure_memory = failure_memory    # 块E：跨会话死路记忆（FailureMemory 实例）；None=关
        # 块G 运行时消费（ADR 0017）：只读 StrategyStore。None=不消费；
        # 有 store 但没有 active 策略时也是彻底 no-op（render_advice 返回空串）。
        self.strategy_store = strategy_store
        self.deadend_threshold = deadend_threshold  # 同一条路累计失败 ≥ 此值 → 提示换思路
        self.research_refine = research_refine   # 块H2：联网搜索不达标→提示重搜；False=关
        self.research_refine_max = research_refine_max  # 同一 query 最多催重搜几次（防无限）
        self.research_max_rounds = research_max_rounds   # **整轮**催重搜总预算；达上限→停搜、综合作答（防换词无限重搜）
        self.research_judge = research_judge     # 块H3a：模型裁判 judge_fn(prompt,images)->str；None=只用H1/H2正则
        # 会话级工具预算（ToolBudget 实例）：**主 Agent 与所有子 Agent 共用同一个**，否则上限形同虚设。
        # None=不限次（存量调用方与测试零行为变化）。
        self.tool_budget = tool_budget
        # 工作区根：只用于死路指纹的路径归一（ADR 0027 V0）。None=不归一，存量行为不变。
        self.workspace = workspace
        import time as _t
        self._sleep = _t.sleep                  # 退避用；测试可替换为 no-op

    def run(
        self,
        messages: list[Message],
        system: str | None,
        emit: Callable[[str, object], None],
        cancel: threading.Event | None = None,
        take_injects=None,
        on_message=None,
    ) -> list[Message]:
        """跑完一整轮对话（可能含多步工具调用）。

        messages 会被原地追加 assistant 与 tool_result 消息；返回同一列表，
        供 bridge 写回会话历史。

        cancel：可选的取消标志，在每一回合开始前检查；置位则立即停止后续回合
        （不打断当前回合内已在进行的模型流，回合间生效，见 FR-8.3）。

        take_injects：可选 `() -> list[str]` 回调，返回并清空"用户在执行中追加的补充消息"
        （steering）。每次工具往返回灌时拉取，把补充附进同一条 user 消息——模型下一轮即看到
        「工具结果 + 用户补充」，可据此重新评估、调整当前任务方向，而非等任务做完再当新事处理。

        on_message：可选 `(Message) -> None` 回调，**每追加一条消息就调一次**，供调用方
        边跑边落库（见 `_LiveList`）。给了它，返回的就是包装后的列表（行为与 list 一致）。
        """
        if on_message is not None and not isinstance(messages, _LiveList):
            # 边跑边落库（见 _LiveList）。**已经包过就不再包**——auto_test 修复轮会拿着同一个
            # 列表再调一次 run，重复包等于每条消息落两遍。
            messages = _LiveList(messages, on_message)
        tools = self.registry.to_schemas()
        # 用量累计（FR-11.8 / ADR 0025）：跨步累加 token，记步数，回合末发 usage 事件。
        # `input` 一律指**未命中缓存**的输入；缓存写/读分列（单价不同，合并即丢失可算性）。
        total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        # 端点没回传用量时改用估算，并**如实标记**——以前那种情况静默按 0 计，
        # 于是那一轮"看起来免费"。一个自信的错数比缺失更危险（ADR 0025 决策 3）。
        measured = True
        steps = 0
        warned = False
        self.hit_max_steps = False   # 本轮是否撞步数上限（供委派标注"子任务未完成"）
        self.hit_deadline = False    # 本轮是否撞墙钟上限（同上，两者标注文案不同）
        deadline = (time.monotonic() + self.time_budget_s) if self.time_budget_s > 0 else None
        # 本轮是否死在模型侧错误（provider 把流式失败转成 error 事件，loop 正常返回、**不抛异常**）。
        # 不暴露出去，委派侧就分不清"子任务做完了"和"子任务半路挂了"，会把残缺结果当成功交给主 Agent
        # ——2026-08-26 模型端点 402 时真机踩到。
        self.stream_error = ""
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
        trunc_nudges = 0                  # 撞 max_tokens 已劝过几次改分块写（劝满即真报错停下）
        _names = self.registry.names() if hasattr(self.registry, "names") else []
        browser_present = any(n.split("__", 1)[-1].startswith("browser_") for n in _names)

        for _ in range(self.max_steps):
            if cancel is not None and cancel.is_set():
                break  # 收到取消：停在回合边界，已追加的消息照常返回/落库
            if deadline is not None and time.monotonic() >= deadline:
                # 超时判定放在**步与步之间**（同 cancel）：不打断在飞的工具/模型流，
                # 只是不再开新的一步。跑满 15 步的子 Agent 因此最多超出"最后一步"的时长。
                self.hit_deadline = True
                break
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
                    self.stream_error = ev.text
                    break
                elif ev.type == "done":
                    stop_reason = ev.meta.get("stop_reason")
                    u = ev.meta.get("usage")
                    if u:
                        for k in total:
                            total[k] += u.get(k, 0) or 0
                    else:
                        measured = False
                        total["input"] += estimate_tokens(messages, system)
                        total["output"] += estimate_tokens_text(assistant_text)
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
                hint = truncation_nudge(trunc_nudges)
                if hint is not None:
                    # 自己换姿势重来，而不是停下让用户去改配置（用户不该为了画原型图研究 max_tokens）。
                    # 注意别产生两条连续 user 消息（破坏 role 交替）：能并进上一条就并。
                    trunc_nudges += 1
                    block = {"type": "text", "text": hint}
                    last = messages[-1] if messages else None
                    if last is not None and last.role == "user" and isinstance(last.content, list):
                        last.content.append(block)
                    else:
                        messages.append(Message("user", hint))
                    emit("truncation_hint", {"text": hint, "n": trunc_nudges})
                    continue
                emit("error",
                     f"模型输出达到 max_tokens 上限被截断（stop_reason={stop_reason}），"
                     "已提示分块写入仍未成功，故停止（避免执行不完整的工具调用、写出残缺文件）。"
                     "请在设置面板把该模型档案的 max_tokens 调到它的真实上限，或把任务拆小些再来。")
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
                    seen_classes: list = []
                    df = detect_repeated_failure(
                        calls, out_by_id, world, self.failure_memory,
                        deadend_fps, self.deadend_threshold,
                        on_failure=lambda _fp, classes, _label: seen_classes.extend(classes),
                        workspace=self.workspace)
                    if df:
                        inject_blocks.append({"type": "text", "text": df})
                        emit("deadend_hint", {"text": df})
                    # 块G：影子记录（只记不改路）+ 已生效策略注入（没有 active 策略＝零注入）
                    if self.strategy_store is not None and seen_classes:
                        from .learning import render_advice, shadow_report
                        items = self.strategy_store.list()
                        sr = shadow_report(items, seen_classes)
                        if sr:
                            emit("learning_shadow", sr)
                        advice = render_advice(items, seen_classes)
                        if advice:
                            inject_blocks.append({"type": "text", "text": advice})
                            emit("learning_advice", {"text": advice,
                                                     "strategies": sr.get("active", [])})
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
        # 两种上限（步数 / 墙钟）走**同一条收尾路径**——差别只在告诉模型是哪一种。
        if self.hit_max_steps or self.hit_deadline:
            # 撞上限：强制收尾一轮——禁用工具，让模型基于已收集信息立即给出总结/结论。
            # 否则 messages 最后一条是 tool_result、无任何文本产出，委派子任务回灌空摘要（FR-9.3）、
            # 长任务也只能裸退。把收尾指令并入最后那条 user 消息（撞上限时它一定是 tool_result），
            # 避免两条连续 user 破坏交替。
            if cancel is None or not cancel.is_set():
                why = ("已达到本次子任务的**时间上限**（墙钟）——别人还在等你的结果"
                       if self.hit_deadline
                       else "已达到防跑飞步数上限（工具调用次数过多，疑似在原地打转）")
                hint = {"type": "text", "text": (
                    f"[系统] {why}。现在不能再调用任何工具。"
                    "请立即基于上面已经收集到的信息，给出尽可能有用的总结/结论：包含已获得的关键数据/发现、"
                    "尚未完成的部分，以及若要继续该换的思路。不要再请求工具。"
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
                            else:
                                measured = False
                                total["input"] += estimate_tokens(messages, system)
                                total["output"] += estimate_tokens_text(final_text)
                            break
                        elif ev.type == "error":
                            break
                except Exception:  # noqa: BLE001 — 收尾失败不影响已有结果返回
                    final_text = ""
                if final_text.strip():
                    messages.append(Message("assistant", final_text))
            if self.hit_deadline:
                emit("error", f"已达时间上限（{int(self.time_budget_s)}s），已基于已收集到的信息收尾。"
                              f"如确属超长子任务，可在设置调高「子任务时间上限」（0 = 不限）。")
            else:
                emit("error", f"工具调用已达防跑飞上限（{self.max_steps} 步，疑似原地打转），已基于已收集信息收尾。"
                              f"如确属超长任务，可在设置调高「防跑飞上限」或改用委派拆分。")

        # 回合末上报用量（FR-11.8 / ADR 0025）：全 0 则不发，避免噪音。
        # 带上 model/provider/measured——落台账时要按**真实 model_id** 计价（档名可以随便起），
        # 且子 Agent 可能用的是另一个模型，不能拿主对话的模型名去套。
        if total["input"] or total["output"]:
            emit("usage", {
                **total, "steps": steps, "max_steps": self.max_steps,
                "measured": measured,
                "model": getattr(self.provider, "model", None),
                "provider": provider_kind(self.provider),
            })
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
                    emit("tool_use", {"id": c.id, "name": c.name, "input": c.input,
                                      "agentic": self._is_agentic(c.name)})
                    futures[c.id] = executor.submit(
                        self._exec_tool_with_retry, c.name, c.input, emit=emit, call=c)
        try:
            for c in calls:  # 串行组照旧（与并行组并发进行）
                if c.id in futures:
                    continue
                emit("tool_use", {"id": c.id, "name": c.name, "input": c.input,
                                  "agentic": self._is_agentic(c.name)})
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

    def _is_agentic(self, name: str) -> bool:
        """这次调用是不是**委派给另一个 agent**（codex 那类：一次调用跑几分钟、会改一堆文件）。

        由代码判定后随事件下发，**不让前端按工具名猜**——名字是 server 起的，猜必然漏。
        """
        try:
            tool = self.registry.get(name)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(tool, "_takes_cwd", False))

    def _exec_tool_with_retry(self, name: str, params: dict, *, emit=None, call=None
                              ) -> tuple[str, bool, list[dict]]:
        """块D：在 `_exec_tool` 外包一层瞬时 IO 自动重试。

        失败分类（块B+C）命中 `TRANSIENT_IO` 且未撞上限 → 退避后重试，不打扰模型；
        其它失败 / 成功 → 原样返回。auto_retry 关时退化为直接 `_exec_tool`。
        重试事件经 `tool_retry` 上报（纯观测）。这是第一条 `Need→Decision` 硬规则的执行点。
        """
        out = self._exec_tool(name, params, emit=emit, call=call)
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
            out = self._exec_tool(name, params, emit=emit, call=call)
            attempts += 1

    def _exec_tool(self, name: str, params: dict, *, emit=None, call=None
                   ) -> tuple[str, bool, list[dict]]:
        """执行单个工具，返回 (结果文本, 是否成功, 额外内容块)。危险工具先过权限 gate。

        普通工具返回 str -> 额外块为空；返回 ToolOutput 的工具（如截屏）-> 带 image 块。
        """
        try:
            tool = self.registry.get(name)
        except ToolError as e:
            return str(e), False, []

        # 会话级预算闸：在 hooks / gate / 执行之前——预算用尽就不该惊动用户去确认一次注定不跑的调用。
        # 回灌的是「预算用尽 + 该怎么办」的事实，标记为失败，模型据此收敛（不是 gate 那种安全 deny）。
        if self.tool_budget is not None:
            over = self.tool_budget.consume(name)
            if over:
                return over, False, []

        # PreToolUse hooks（程序化守卫）：可拦截（退出码 2）或放行+警告（退出码 1）。
        pre_warn = None
        if self.hook_runner is not None:
            allowed, msg = self.hook_runner.pre(name, params)
            if not allowed:
                return (msg or "操作被 PreToolUse hook 拦截。"), False, []
            pre_warn = msg

        # 调用前补参（MCP 的 cwd / 续话 id）。**必须在 gate 之前**：确认条上要显示的是
        # 真正会执行的参数——cwd 决定它在哪儿干活，看不到就等于没确认。
        if hasattr(tool, "prepare"):
            try:
                params = tool.prepare(params)
            except Exception:  # noqa: BLE001 — 补参失败就按原样走，别把调用带崩
                pass
        # always_confirm：agent 型 MCP server（codex 那类）每次都问，不吃「全部允许」
        if tool.dangerous and not self.gate.confirm(
                name, params, always_ask=bool(getattr(tool, "always_confirm", False))):
            return _DENIED, False, []

        try:
            # 支持实时流输出的工具（run_powershell 前台）：给它一个 stream 回调，边跑边把输出增量推前端。
            # tool_use_id 绑定，前端按 id 把 delta 追加到对应运行中的工具块。推流失败不影响执行。
            if getattr(tool, "wants_stream", False) and emit is not None and call is not None:
                def _stream(kind, delta):
                    emit("tool_stream", {"id": call.id, "name": name, "stream": kind, "delta": delta})
                out = tool.run(params, stream=_stream)
            else:
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
