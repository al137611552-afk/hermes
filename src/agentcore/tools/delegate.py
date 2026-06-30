"""委派子任务工具（FR-9.3）：主 Agent 把一个具体子任务交给独立上下文的子 Agent。

子 Agent 用全新的消息历史跑自己的 AgentLoop（同工作区、同工具，但**不含** delegate /
update_tasks，避免无限嵌套与污染主任务清单），跑完**只把一段摘要**作为 tool_result
回灌主 Agent——主上下文因此保持精简（子任务的大量中间步骤不进主历史）。

实际起跑子循环的逻辑在 Conversation.run_subagent（需 provider/registry/gate/emit），
本工具只通过 DelegateBinding 转发。纯函数（compose_task / extract_summary）便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import Tool, ToolError

# 注入到子 Agent system 的基础角色指令（所有角色共用）
SUBAGENT_DIRECTIVE = (
    "你是被主 Agent 委派来完成一个**具体子任务**的子 Agent。专注把这个子任务做完，用工具实际去做、"
    "不要只给建议。完成后用一段**简洁摘要**说明：你做了什么、结果或产物（涉及的文件/关键发现）、"
    "以及主 Agent 需要知道的要点或后续注意事项。不要寒暄、不要复述子任务原文。"
)

# 只读类工具名（researcher/reviewer/tester 都可用）；shell 工具名是动态 run_<shell>，单独判定
_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_dir", "grep_search", "glob_search", "search_code",
    "code_outline", "find_symbol", "recall",
    "git_status", "git_diff", "git_log",
    "list_processes", "read_process_output",  # 看后台进程是只读；stop_process 不在
    "web_search", "web_fetch",                # 联网检索只读（FR-11.1）
    "ask_user",                               # 向用户提问/请其登录——非破坏，子 Agent 也可用
})

# 浏览器导航/浏览类 MCP 工具（Playwright MCP，深度调研站内下钻用）：含基本交互（点击/输入/翻页），
# 但排除能跑任意 JS / 传文件等高风险的（evaluate / run_code_unsafe / file_upload / fill_form /
# drag / drop / close / resize / handle_dialog）。按「去掉 server 前缀后的工具基名」匹配，故 MCP
# server 名可任意自定义（browser__browser_navigate / pw__browser_navigate 都认）。
_BROWSE_TOOLS = frozenset({
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_hover", "browser_type", "browser_press_key", "browser_select_option",
    "browser_scroll", "browser_wait_for", "browser_tabs", "browser_take_screenshot",
    "browser_network_requests", "browser_network_request", "browser_console_messages",
})


@dataclass(frozen=True)
class Role:
    """子 Agent 角色（FR-9.5/10.5）：定制指令 + 工具限权 + 可选按角色配模型。

    内置角色按能力判定（只读名单 + run_ 前缀，兼容动态 shell 名）；
    自定义角色（config agent.roles）用显式工具白名单 `tools`（FR-10.5）。
    """
    name: str
    label: str            # 前端显示用的中文名
    directive: str        # 追加到子 Agent system 的职责说明
    allow_all: bool = False   # general：放开全部工具
    allow_shell: bool = False  # 是否允许 run_<shell> 命令工具
    allow_browse: bool = False  # 是否允许浏览器导航/浏览类 MCP 工具（researcher 深度调研站内下钻用）
    tools: "frozenset[str] | None" = None  # 显式白名单（自定义角色）；None=按内置能力判定
    model: "str | None" = None             # 该角色用的模型档案；None=subagent_model→当前模型

    def allows(self, tool_name: str) -> bool:
        if self.allow_all:
            return True
        if self.tools is not None:  # 自定义白名单：所列即所得
            return tool_name in self.tools
        if tool_name in _READ_ONLY_TOOLS:
            return True
        if self.allow_shell and tool_name.startswith("run_"):
            return True
        if self.allow_browse and tool_name.split("__", 1)[-1] in _BROWSE_TOOLS:
            return True
        return False


ROLES: dict[str, Role] = {
    "general": Role(
        "general", "通用", "", allow_all=True,
    ),
    "researcher": Role(
        "researcher", "调研",
        "你的职责是**深度调研**：围绕目标查清事实、产出有依据且可追溯的结论，"
        "**不修改任何本地文件、不执行本地命令**。像人一样逐层深挖、别浮于表面：\n"
        "1. 先把目标拆成几个可回答的**子问题**；\n"
        "2. **逐层下钻、不止步于搜索摘要或一级页面**：从入口找到线索后顺着点进去看明细——"
        "聚合数据→具体条目→分布/排名→定性内容（评论/正文/要点），每深一层都带着新问题继续；\n"
        "3. **有浏览器工具（browser_*）时——一切走浏览器**（此时你没有 web_search/web_fetch，也别去找它们）："
        "browser_navigate 到目标站（用户点名了站就直接去，如「在知乎查」就开 zhihu.com）或搜索引擎 → "
        "**点进一个具体结果 → 到内容页再 browser_snapshot 读**。**别从搜索/列表页的 snapshot 里硬读答案、更别"
        "为此放弃**——搜索页又杂又长、读不出很正常（不是被墙），**点进去到具体内容页就好读了**；正文长就滚动"
        "多次 snapshot 读全。**搜索结果页里带 ref 的标题/链接条目就是结果——挑相关的前几条用其 ref browser_click "
        "点进去读；只要列出了相关条目，就别换另一个搜索引擎重搜**（再开百度/必应/谷歌重搜关键词＝换汤不换药的绕路，禁止）。"
        "**snapshot 看着空 / 像骨架 / 没看到正文时，别急着判「页面没加载 / 要登录 / 被反爬」就换路**——"
        "很多站（知乎等）正文是 JS 懒加载或在下方：重新 browser_navigate 等一下再 snapshot、或往下 scroll 再 snapshot、"
        "或直接点进一个具体结果到内容页；**snapshot 没出全内容是常态，不等于页面没加载**。"
        "**没有浏览器工具时**才用 web_search 找入口 URL + web_fetch 读正文；\n"
        "4. 边查边记住关键数据点（数值/来源/时间），**多源交叉印证、留意冲突**；\n"
        "5. 最后**综合成结论汇报**：关键论断标来源，说明确定性与缺口。别搜一轮就收、别只给一级摘要。\n"
        "**最关键——别轻易判「不相关」**：别只看搜索摘要 / 页面标题 / snapshot 的标题栏就判定某页没有答案，"
        "**往下读正文、内容看够了再判断**——答案常藏在正文深处，标题看着不直接也可能正是要找的。\n"
        "**遇到登录墙 / 要登录 / 验证码 / 滑块——别绕去别的搜索引擎或来源！** 先用 **ask_user** 暂停，提示用户："
        "「需要登录 <网站> 才能继续，请在弹出的浏览器窗口里登录（划过滑块），登录好回复『继续』」。"
        "**用户回复『继续』后＝默认他已经登录好了，要信任他**：直接 browser_navigate 重新打开**目标页**（别再去登录页）、"
        "读正文继续干活——登录态在持久 profile 里、重开一下就生效。**别再用 ask_user 问第二遍、也别一看到残留的登录提示就以为没登上**"
        "（可能只是没刷新）；只有重开目标页、读了内容**确实仍是登录页**时，才说明可能没登上、再问用户一次。**别自己破解滑块/验证码**（必输）。"
        "只有当用户明确说登不了/不登时，才换官方 API / 其它来源、或说明已跳过。",
        allow_browse=True,
    ),
    "reviewer": Role(
        "reviewer", "评审",
        "你的职责是**评审**代码或改动质量：只读、**不改动**。指出问题、风险与改进建议，并给出依据"
        "（具体文件/行）。结论要可执行。",
    ),
    "tester": Role(
        "tester", "测试",
        "你的职责是**运行测试/验证并报告**：可执行命令来跑测试，但**不要修改代码**。说明你跑了什么、"
        "结果如何、失败的原因与定位。", allow_shell=True,
    ),
}


def build_roles(custom: "dict | None") -> dict[str, Role]:
    """内置角色 + config `agent.roles` 自定义角色合并（同名覆盖内置，FR-10.5）。

    custom 的值为 RoleSpec（pydantic）或等价对象：label / directive / tools / model。
    tools 省略（None）= 全工具；给了列表 = 显式白名单。
    """
    roles = dict(ROLES)
    for name, spec in (custom or {}).items():
        key = (name or "").strip()
        if not key:
            continue
        tools = getattr(spec, "tools", None)
        roles[key] = Role(
            key,
            (getattr(spec, "label", "") or key).strip(),
            (getattr(spec, "directive", "") or "").strip(),
            allow_all=tools is None,
            tools=frozenset(tools) if tools is not None else None,
            model=(getattr(spec, "model", None) or None),
        )
    return roles


def resolve_role(name: "str | None", roles: "dict[str, Role] | None" = None) -> Role:
    """按名取角色；缺省或未知一律回退 general（宽容，避免模型小笔误卡住）。"""
    table = roles or ROLES
    return table.get((name or "general").strip(), table.get("general", ROLES["general"]))


def compose_task(task: str, context: str | None) -> str:
    """把子任务描述 + 可选上下文组成子 Agent 的首条 user 消息。"""
    task = (task or "").strip()
    ctx = (context or "").strip()
    if ctx:
        return f"子任务：{task}\n\n相关背景/上下文：\n{ctx}"
    return f"子任务：{task}"


def summarize_activity(messages: list, max_items: int = 12, max_chars: int = 160) -> str:
    """从子 Agent 的消息里提炼「执行证据」：实际调用了哪些工具 + 结果摘要（纯逻辑）。

    让验收员据**真实动作**评分（看到「跑了 pytest → 3 passed」「写了 result.txt」），
    而不是只看最后一句自述、把没贴日志的合理工作误判为"无依据"。
    """
    lines: list[str] = []
    pending: "dict | None" = None
    for m in messages or []:
        content = getattr(m, "content", None)
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = str(b.get("input", ""))
                if len(inp) > max_chars:
                    inp = inp[:max_chars] + "…"
                lines.append(f"· 调用 {b.get('name')}（{inp}）")
            elif b.get("type") == "tool_result":
                c = b.get("content", "")
                c = c if isinstance(c, str) else str(c)
                c = " ".join(c.split())
                if len(c) > max_chars:
                    c = c[:max_chars] + "…"
                if lines:
                    lines[-1] += f" → {c}"
    if not lines:
        return "（无工具调用记录——子 Agent 可能只给了文字、未实际动手。）"
    return "\n".join(lines[-max_items:])


def build_grader_prompt(task: str, acceptance: "str | None", summary: str,
                        evidence: str = "") -> str:
    """组织验收员（grader）的评分提示（纯逻辑，委派评分回炉）。

    依据**执行证据**（子 Agent 实际调用的工具与结果）判断，而非要求它在摘要里粘贴日志——
    这是真跑校准后的关键：避免把"没贴证据"的合理工作误判为不通过。
    """
    crit = (acceptance or "").strip() or "合理、完整地达成上述任务目标，关键产物具体可信、无明显缺口或偏题。"
    ev = (evidence or "").strip() or "（未提供执行证据。）"
    return (
        "你是一个**讲道理的验收员**。下面是委派给子 Agent 的任务、验收标准、它的**执行证据**"
        "（实际调用的工具与结果）、以及它的产出摘要。请据此判断子任务是否**合理地达成**。\n"
        "判定准则：\n"
        "- **PASS**：执行证据显示关键产物/结果具体且可信地满足了验收标准"
        "（例如：确实读了相关文件、确实写出了正确产物、测试确实跑过且通过、给出了具体数值/结论）。\n"
        "- **REVISE**：仅当存在**实质问题**——偏题、明显遗漏某条验收要点、产物缺失或为半成品、"
        "结论空泛无实质、或证据与结论明显矛盾。**不要**仅因为'摘要里没贴完整日志/diff'就打回"
        "已由执行证据佐证、显然完成的工作。\n"
        "**输出格式**：第一行只写 `PASS` 或 `REVISE`（大写）；若 REVISE，从第二行起用 1~3 条"
        "具体、可执行的反馈指出差距，不要含糊客套。\n\n"
        f"【任务】\n{task.strip()}\n\n【验收标准】\n{crit}\n\n"
        f"【执行证据】\n{ev}\n\n【子 Agent 产出摘要】\n{summary.strip()}"
    )


def parse_grade(text: str) -> "tuple[bool, str]":
    """解析 grader 输出 -> (是否通过, 反馈文本)（纯逻辑）。

    第一行含 PASS（且不含 REVISE）判为通过；否则不通过，反馈取其余行（空则给兜底语）。
    解析不确定时**偏向不通过**（让它多打磨一轮，比误放过半成品稳）。
    """
    t = (text or "").strip()
    if not t:
        return False, "验收员未给出有效结论，请检查产出是否完整并补全。"
    lines = t.splitlines()
    head = lines[0].strip().upper()
    passed = ("PASS" in head) and ("REVISE" not in head)
    if passed:
        return True, ""
    feedback = "\n".join(lines[1:]).strip()
    if not feedback:  # 判 REVISE 但没给反馈：从整体取，再兜底
        feedback = t if head not in ("REVISE",) else "产出未达验收标准，请补全缺口、修正偏差后重做。"
    return False, feedback


def extract_summary(messages: list) -> str:
    """从子 Agent 跑完的消息里取最后一段 assistant 文本作为摘要。"""
    for m in reversed(messages):
        if getattr(m, "role", None) != "assistant":
            continue
        c = m.content
        if isinstance(c, str):
            text = c.strip()
        elif isinstance(c, list):
            text = "\n".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        else:
            text = ""
        if text:
            return text
    return "（子任务已结束，但没有产出文本摘要。）"


@dataclass
class DelegateBinding:
    """把"起跑子 Agent"的能力注入工具：runner(task, context, role) -> 摘要文本。

    roles 为合并后的角色表（内置 + config 自定义，FR-10.5），供工具动态生成 schema/描述。
    """
    runner: Callable[[str, "str | None", str, "str | None"], str]  # (task, context, role, acceptance)
    roles: "dict[str, Role] | None" = None


_BUILTIN_ROLE_DESC = (
    "general(默认,全工具) / researcher(只读,调研检索) / reviewer(只读,评审) / "
    "tester(只读+可跑命令,测试验证)"
)


class DelegateTool(Tool):
    name = "delegate"
    # 同一回合发出的多个 delegate 调用会被并行执行（FR-10.5，loop 据此标记分组）
    parallel_safe = True

    def __init__(self, binding: DelegateBinding) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._b = binding
        roles = binding.roles or ROLES
        customs = [r for n, r in roles.items() if n not in ROLES]
        role_desc = _BUILTIN_ROLE_DESC
        if customs:
            role_desc += "；自定义角色：" + " / ".join(
                f"{r.name}({r.label}{'，限定工具' if r.tools is not None else ''})" for r in customs
            )
        self.description = (
            "把一个**相对独立、较重**的子任务交给一个独立上下文的子 Agent 去完成，完成后只返回一段摘要"
            "（主对话上下文因此保持精简）。适合：调研/检索一大片代码、评审、跑测试、批量改动等中间步骤多、"
            "主线只关心结论的子任务。**互不依赖的子任务可在同一轮一次发出多个 delegate 调用，会并行执行**；"
            f"有依赖的按顺序分轮派。可用 role 指定子 Agent 角色：{role_desc}——只读角色更专注也更安全。"
            "简单的一两步操作直接自己做，不要委派。子 Agent 不能再委派。"
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "交给子 Agent 的子任务，写清目标与完成标准，自包含",
                },
                "context": {
                    "type": "string",
                    "description": "可选：子 Agent 需要的相关背景（相关文件、约束、已知信息等）",
                },
                "acceptance": {
                    "type": "string",
                    "description": "可选：本子任务的**验收标准**（怎样算做好/做完）。开启评分回炉时，"
                                   "验收员据此判断是否打回重做；写清可检验的完成条件能显著提升子任务质量。",
                },
                "role": {
                    "type": "string",
                    "enum": sorted(roles),
                    "description": "子 Agent 角色（默认 general）：researcher/reviewer 只读，tester 只读+可跑命令",
                },
            },
            "required": ["task"],
        }

    def run(self, params: dict) -> str:
        task = (params.get("task") or "").strip()
        if not task:
            raise ToolError("task 不能为空")
        return self._b.runner(task, params.get("context"), params.get("role") or "general",
                              params.get("acceptance"))
