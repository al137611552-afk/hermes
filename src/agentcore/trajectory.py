"""轨迹录制与固化（ADR 0023 决策 4~8）：录一段"刚才是怎么干成的" → 归并 → 参数化 →
交给与 `/技能化` 同源的提示词流水线草拟 **SOP 技能**。

**刻意不做的**（都在 ADR 里有理由，别顺手加回来）：

- **不自动检测、不自动固化**（决策 4）。判据不可靠：保守则永不触发，激进则批量生成垃圾技能，
  而技能清单的**价值密度**正是渐进披露（ADR 0014 决策 2）的前提——`description` 就是路由表，
  噪声技能直接毁掉模型的技能选择。用户按下按钮那一刻的人工信号比任何启发式都准。
- **不生成回放脚本**（决策 6）。页面一改版就碎，维护是必输的军备竞赛；稳定性来自
  "固化步骤 + 可程序化验收"，不来自坐标与选择器。
- **不建独立存储、不建检索层**（备选与权衡）。轨迹是**一次性素材**，用完即转成技能，
  全程只活在内存里（ADR 0014 不变量：物化你要学习的，别建你不需要的引擎）。

本模块是**纯逻辑**：不碰盘、不发事件、不起线程（只有一把保护自身状态的锁）。
采集面由 `bridge/conversation.py` 喂进来（工具事件流 + 用户消息 + 人工打点）。
与 `tools/trace.py`（FR-13.D 运行时插桩）**命名刻意区分**，两者无关。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

# 归并/展示上限：轨迹是给模型读的素材，不是日志——超了只会挤占它写 SOP 的注意力
MAX_STEPS = 120           # 录制期上限（到顶就不再记，状态条会显示已满）
MERGE_TARGETS = 5         # 同一工具连续调用，最多列几个目标
MAX_PARAMS = 8            # 参数化候选上限
SNIPPET = 160             # 单条摘要/旁白截断长度

# 不进轨迹的工具：这些是**过程管理**（给下一轮的自己留交接班记录），不是"这类事怎么做"的步骤。
# 录进去只会让 SOP 里混进"记得更新任务清单"这种废话。
SKIP_TOOLS = frozenset({"update_tasks", "update_notes"})

# 一眼能看出"这步在动什么"的参数名，按优先级取第一个命中的
_KEY_PARAMS = ("path", "file", "url", "command", "query", "pattern", "name", "text", "target")


@dataclass
class Step:
    """轨迹里的一步。kind: tool=agent 动作 / note=人工打点 / say=旁白或纠正。"""
    kind: str
    at: float                 # 相对录制开始的秒数
    label: str                # 一行摘要（展示 + 喂模型都用它）
    tool: str = ""
    detail: str = ""          # 打点快照摘要等补充信息
    count: int = 1            # 归并了几次同类调用
    ok: bool = True

    def as_dict(self) -> dict:
        return {"kind": self.kind, "at": round(self.at, 1), "label": self.label,
                "tool": self.tool, "detail": self.detail, "count": self.count, "ok": self.ok}


def clip(s: str, n: int = SNIPPET) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, params: dict | None) -> str:
    """工具调用 → 一行"在动什么"。取第一个有信息量的关键参数，没有就只留工具名。"""
    p = params if isinstance(params, dict) else {}
    for k in _KEY_PARAMS:
        v = p.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return f"{name}({k}={clip(v, 80)})"
    return f"{name}()"


def digest_snapshot(text: str) -> tuple[str, str]:
    """从浏览器无障碍快照里摘出 (URL, 页面标题)。抓不到就给空串——打点照样成立。

    Playwright MCP 的快照抬头形如 `- Page URL: https://…` / `- Page Title: …`；
    容错写法：整篇里找第一处，认不出就算了（**不为解析失败丢掉这一步**）。
    """
    t = str(text or "")
    url = re.search(r"(?:Page URL:|url:)\s*(\S+)", t, re.I)
    title = re.search(r"(?:Page Title:|title:)\s*(.+)", t, re.I)
    return (url.group(1).strip() if url else "",
            clip(title.group(1), 80) if title else "")


def merge_steps(steps: list[Step]) -> list[Step]:
    """归并连续的同工具调用：`read_file` 连读五个文件 → 一步「read_file × 5（a, b, c…）」。

    只并**相邻**的同工具步：中间插了别的工具/旁白就是新的一段——步骤顺序本身是 SOP 的信息，
    跨段合并会把"先搜再读"压成"读了一堆"，把做法抹平。
    """
    out: list[Step] = []
    args: list[list[str]] = []
    for s in steps:
        prev = out[-1] if out else None
        if (prev is not None and s.kind == "tool" and prev.kind == "tool"
                and s.tool and s.tool == prev.tool):
            prev.count += 1
            prev.ok = prev.ok and s.ok
            args[-1].append(_arg_of(s.label))
            continue
        out.append(Step(**dict(s.__dict__)))
        args.append([_arg_of(s.label)])
    for st, a in zip(out, args):
        if st.count > 1:
            shown = "；".join(x for x in a[:MERGE_TARGETS] if x)
            st.label = f"{st.tool} × {st.count}：{shown}" + ("；…" if len(a) > MERGE_TARGETS else "")
    return out


def _arg_of(label: str) -> str:
    """`read_file(path=a.py)` → `path=a.py`（归并时只续接参数部分，别重复工具名）。"""
    i, j = label.find("("), label.rfind(")")
    return label[i + 1: j] if 0 <= i < j else label


# ---- 参数化（决策 7：这次的事 → 这类事的**唯一关键动作**）--------------------
# 抽得保守：只认"一眼就是具体值"的四类。宁可漏，也不要把 SOP 里的通用词也挖成变量——
# 变量太多会让复用时填参变成负担，反而没人用。
_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("url", r"https?://[^\s\"'）)，,；;]+", "网址"),
    ("email", r"[\w.+-]+@[\w-]+\.[\w.]+", "账号"),
    ("date", r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}\s*年\s*\d{1,2}\s*月", "日期"),
    ("path", r"(?:[A-Za-z]:\\|\.{0,2}/)?(?:[\w.-]+[/\\])+[\w.-]+\.\w{1,6}|[\w.-]+\.\w{1,6}", "文件"),
)


def param_candidates(steps: list[Step], limit: int = MAX_PARAMS) -> list[dict]:
    """从轨迹里挑出该被参数化的具体值，按出现次数排序（同次数保持首见顺序）。

    返回 [{value, kind, name, occurrences}]，`name` 形如 `{{网址}}` / `{{网址2}}`（保证唯一）。
    """
    hay = "\n".join(s.label + " " + s.detail for s in steps)
    seen: dict[str, dict] = {}
    order: list[str] = []
    for kind, pat, zh in _PATTERNS:
        for m in re.finditer(pat, hay):
            val = m.group(0).rstrip(".,;:）)")
            if len(val) < 4 or val in seen:
                continue
            # 一个值只归第一个匹配到它的类别（URL 里的路径别再被当成文件）
            if any(val in k for k in seen):
                continue
            seen[val] = {"value": val, "kind": kind, "zh": zh,
                         "occurrences": hay.count(val)}
            order.append(val)
    ranked = sorted(order, key=lambda v: (-seen[v]["occurrences"], order.index(v)))[:limit]
    used: dict[str, int] = {}
    out = []
    for v in ranked:
        c = seen[v]
        used[c["zh"]] = used.get(c["zh"], 0) + 1
        n = c["zh"] + ("" if used[c["zh"]] == 1 else str(used[c["zh"]]))
        out.append({"value": v, "kind": c["kind"], "name": "{{%s}}" % n,
                    "occurrences": c["occurrences"]})
    return out


# ---- 固化提示词（决策 6+8：产出 SOP 技能，出口复用 /技能化 的流水线）----------

_SCOPES = {"project": "本项目（<工作区>/.hermes/skills/）", "global": "全局（对所有项目可见）"}


def build_skill_prompt(*, goal: str, steps: list[Step], params: list[dict],
                       skill_name: str = "", description: str = "",
                       scope: str = "project") -> str:
    """轨迹 → 喂给 `skill-creator` 流水线的一段指令（与 `/技能化` 同一个出口，不另造编辑器）。

    刻意写死三条要求：**参数化**（决策 7）、**写 SOP 不写回放**（决策 6）、
    **带可执行验收**（沿用内置 research-report 的做法）。
    """
    lines = [f"（使用 `skill-creator` 技能）把**刚才这段过程**固化成一个可复用的技能。",
             "",
             f"【这段过程要达成的】{clip(goal, 300) or '（未填写，请据下面的轨迹自己总结）'}",
             "", "【录到的轨迹】（按时间顺序；`我说：`是我当时的旁白或纠正，权重最高）"]
    for i, s in enumerate(steps, 1):
        body = (f"我说：{s.label}" if s.kind == "say"
                else f"{'📌' if s.kind == 'note' else '·'} {s.label}")
        lines.append(f"{i}. {body}" + (f"　（{s.detail}）" if s.detail else ""))
    if params:
        lines += ["", "【参数化候选】（下面这些具体值必须换成变量，并在 SKILL.md 里给示例值）"]
        lines += [f"- {p['name']} ← 本次的值：{p['value']}" for p in params]
    lines += [
        "",
        "【要求】",
        "1. **写「这类事怎么做」，不是「这次点了什么」**：去哪几类地方、每处怎么定位到有效信息、"
        "信源/优先级怎么排、什么算做完了。**不要写死坐标、选择器或固定的点击序列**——页面一改版就碎。",
        "2. **参数化是必做步骤**：把路径、账号、日期、公司名、目标文件、具体站点抽成 `{{变量}}` 并给示例值。"
        "不做参数化的固化不如不做。",
        "3. **带一个可执行的验收**（脚本或明确的检查清单），让复用时能自己判断有没有做到位。",
        "4. 我的旁白/纠正是**意图**，务必写进 SOP——点击流里推不出这些。",
        f"5. 落盘范围：{_SCOPES.get(scope, _SCOPES['project'])}。",
    ]
    if skill_name:
        lines.append(f"6. 技能名用 `{skill_name}`。")
    if description:
        lines.append(f"7. description 按这个意思写：{clip(description, 200)}")
    lines += ["", "写完把 SKILL.md 路径、description、变量清单、验收怎么跑告诉我。"]
    return "\n".join(lines)


# ---- 录制器（唯一的可变状态；由 bridge 喂事件）-------------------------------

@dataclass
class _Session:
    goal: str = ""
    started: float = 0.0
    steps: list[Step] = field(default_factory=list)
    full: bool = False


class TrajectoryRecorder:
    """人手动开关的录制器（决策 4）。不自动开始、不自动结束、不自动固化。

    线程安全：工具事件来自 agent worker 线程，打点/开关来自 UI 线程。
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._s: _Session | None = None

    @property
    def recording(self) -> bool:
        return self._s is not None

    def start(self, goal: str = "") -> dict:
        with self._lock:
            if self._s is not None:
                return {"ok": False, "error": "已经在录了"}
            self._s = _Session(goal=str(goal or "").strip(), started=self._clock())
        return {"ok": True, **self.state()}

    def discard(self) -> dict:
        """丢弃：轨迹是一次性素材，不留档、不落盘。"""
        with self._lock:
            self._s = None
        return {"ok": True, **self.state()}

    def state(self) -> dict:
        with self._lock:
            s = self._s
            if s is None:
                return {"recording": False, "steps": 0, "seconds": 0, "full": False}
            return {"recording": True, "steps": len(s.steps),
                    "seconds": int(self._clock() - s.started), "full": s.full,
                    "goal": s.goal}

    # -- 采集 ---------------------------------------------------------------

    def observe(self, event: str, data) -> None:
        """喂 emit 事件流（T1 会话内轨迹）。只认工具事件，其余一律忽略。

        录的是**调用**而不是结果全文：SOP 要的是"这步干了什么"，工具输出动辄几万字，
        塞进去只会把模型的注意力吃光。失败的调用也记（`ok=False`）——试错路径本身就是经验。
        """
        if event not in ("tool_use", "tool_result") or not isinstance(data, dict):
            return
        name = str(data.get("name") or "")
        if not name or name in SKIP_TOOLS:
            return
        if event == "tool_use":
            self._add(Step("tool", 0.0, describe_tool(name, data.get("input")), tool=name))
        elif data.get("ok") is False:
            with self._lock:                      # 失败：把最近一条同名步标红，不新增一步
                s = self._s
                if s is None:
                    return
                for st in reversed(s.steps):
                    if st.tool == name:
                        st.ok = False
                        break

    def say(self, text: str) -> None:
        """用户消息＝旁白/纠正（决策 5）：意图不可能从点击流里推断出来，这是信息密度最高的一类。"""
        t = clip(text, 300)
        if t:
            self._add(Step("say", 0.0, t))

    def mark(self, note: str = "", snapshot: str = "") -> dict:
        """人工打点（决策 5 T2）：人在关键处点「记一步」，附一句说明；同时抓一份现场。

        **不做定时全量快照**——会录进大量无意义中间页，把信噪比压垮；打点本身就是人在标注
        "这一步重要"，信息密度高一个量级。
        """
        url, title = digest_snapshot(snapshot)
        detail = "；".join(x for x in (url, title) if x)
        label = clip(note, 300) or (url and f"记一步：{url}") or "记一步"
        ok = self._add(Step("note", 0.0, label, detail=detail))
        return {"ok": ok, **self.state()}

    def _add(self, step: Step) -> bool:
        with self._lock:
            s = self._s
            if s is None:
                return False
            if len(s.steps) >= MAX_STEPS:
                s.full = True          # 到顶就停手：继续堆只会稀释信噪比（状态条会显示已满）
                return False
            step.at = self._clock() - s.started
            s.steps.append(step)
            return True

    # -- 收尾 ---------------------------------------------------------------

    def stop(self) -> dict:
        """结束录制并交出归并后的轨迹 + 参数化候选（**不落盘、不自动生成技能**）。"""
        with self._lock:
            s, self._s = self._s, None
        if s is None:
            return {"ok": False, "error": "当前没有在录"}
        steps = merge_steps(s.steps)
        return {"ok": True, "goal": s.goal, "seconds": int(self._clock() - s.started),
                "truncated": s.full,
                "steps": [st.as_dict() for st in steps],
                "params": param_candidates(steps)}


def steps_from_dicts(items) -> list[Step]:
    """前端改过（勾掉几步/改了措辞）的轨迹回传后，还原成 Step 以便复用同一套拼装逻辑。"""
    out = []
    for d in items or []:
        if not isinstance(d, dict):
            continue
        out.append(Step(kind=str(d.get("kind") or "tool"), at=float(d.get("at") or 0),
                        label=str(d.get("label") or ""), tool=str(d.get("tool") or ""),
                        detail=str(d.get("detail") or ""), count=int(d.get("count") or 1),
                        ok=bool(d.get("ok", True))))
    return [s for s in out if s.label]
