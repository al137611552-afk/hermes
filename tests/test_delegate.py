"""FR-9.3 委派子 Agent：delegate 工具 + 纯逻辑 + 注册表隔离（无网络、无模型）。

不跑真实子 Agent 循环（需联网）；只验工具转发、纯函数、以及"主含 delegate、子排除
delegate/update_tasks"的注册表性质。运行：python tests/test_delegate.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.providers import Message  # noqa: E402
from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.delegate import (  # noqa: E402
    ROLES,
    DelegateBinding,
    DelegateTool,
    build_grader_prompt,
    compose_task,
    extract_summary,
    parse_grade,
    resolve_role,
    summarize_activity,
)
from agentcore.tools.tasks import TaskBinding  # noqa: E402


# ---- 纯逻辑 ----------------------------------------------------------------

def test_compose_task():
    assert compose_task(" 做X ", None) == "子任务：做X"
    out = compose_task("做X", " 背景Y ")
    assert "子任务：做X" in out and "相关背景/上下文：\n背景Y" in out


def test_extract_summary_prefers_last_assistant_text():
    msgs = [
        Message("user", "子任务：x"),
        Message("assistant", [{"type": "text", "text": "中间想法"},
                              {"type": "tool_use", "id": "1", "name": "read_file", "input": {}}]),
        Message("user", [{"type": "tool_result", "tool_use_id": "1", "content": "文件内容"}]),
        Message("assistant", "最终摘要：已完成。"),
    ]
    assert extract_summary(msgs) == "最终摘要：已完成。"


def test_extract_summary_from_blocks():
    msgs = [Message("assistant", [{"type": "text", "text": "块摘要"}])]
    assert extract_summary(msgs) == "块摘要"


def test_extract_summary_fallback_when_no_text():
    assert "没有产出文本" in extract_summary([Message("user", "x")])


# ---- 工具转发 --------------------------------------------------------------

def test_delegate_tool_forwards_to_runner():
    seen = {}

    def runner(task, context, role, acceptance):
        seen.update(task=task, context=context, role=role, acceptance=acceptance)
        return "子任务完成摘要"

    tool = DelegateTool(DelegateBinding(runner))
    out = tool.run({"task": "重构模块", "context": "在 src/ 下", "role": "researcher",
                    "acceptance": "全部测试通过"})
    assert out == "子任务完成摘要"
    assert seen == {"task": "重构模块", "context": "在 src/ 下", "role": "researcher",
                    "acceptance": "全部测试通过"}
    # 不传 role/acceptance 默认 general/None
    tool.run({"task": "x"})
    assert seen["role"] == "general" and seen["acceptance"] is None


def test_delegate_tool_rejects_empty():
    tool = DelegateTool(DelegateBinding(lambda t, c, r, a: "x"))
    try:
        tool.run({"task": "   "}); assert False, "空 task 应报错"
    except ToolError:
        pass


# ---- 评分回炉（grader）纯逻辑 ----------------------------------------------

def test_parse_grade_pass():
    assert parse_grade("PASS") == (True, "")
    assert parse_grade("pass\n无需改进") == (True, "")  # 大小写不敏感
    assert parse_grade("  PASS  \n附注随意") == (True, "")


def test_parse_grade_revise_with_feedback():
    passed, fb = parse_grade("REVISE\n1. 没跑测试\n2. 漏了边界情况")
    assert passed is False and "没跑测试" in fb and "边界" in fb


def test_parse_grade_revise_without_feedback_has_fallback():
    passed, fb = parse_grade("REVISE")
    assert passed is False and fb.strip()  # 有兜底反馈


def test_parse_grade_empty_or_ambiguous_defaults_to_revise():
    assert parse_grade("")[0] is False          # 空 -> 不通过（偏严）
    assert parse_grade("我觉得还行吧")[0] is False  # 首行无 PASS -> 不通过


def test_parse_grade_pass_word_with_revise_in_head_is_revise():
    # 首行同时含 PASS 和 REVISE 视为不通过（避免"PASS? 不，REVISE"被误判通过）
    assert parse_grade("PASS or REVISE? REVISE")[0] is False


def test_build_grader_prompt_includes_parts():
    p = build_grader_prompt("修复登录 bug", "所有用例通过", "我改了 auth.py")
    assert "修复登录 bug" in p and "所有用例通过" in p and "我改了 auth.py" in p and "PASS" in p


def test_build_grader_prompt_default_acceptance():
    p = build_grader_prompt("任务X", None, "产出Y")
    assert "任务X" in p and "产出Y" in p and "达成" in p  # 无验收标准时有兜底标准


def test_build_grader_prompt_includes_evidence():
    p = build_grader_prompt("任务", "标准", "摘要", evidence="· 调用 run_bash → 3 passed")
    assert "执行证据" in p and "3 passed" in p


def test_summarize_activity_pairs_tools_and_results():
    msgs = [
        Message("user", "子任务"),
        Message("assistant", [{"type": "tool_use", "id": "1", "name": "write_file",
                               "input": {"path": "result.txt"}}]),
        Message("user", [{"type": "tool_result", "tool_use_id": "1", "content": "已写入 result.txt"}]),
        Message("assistant", "完成"),
    ]
    ev = summarize_activity(msgs)
    assert "write_file" in ev and "已写入 result.txt" in ev and "→" in ev


def test_summarize_activity_empty_when_no_tools():
    ev = summarize_activity([Message("assistant", "我建议你这么做")])
    assert "未实际动手" in ev or "无工具调用" in ev


# ---- 角色（FR-9.5）---------------------------------------------------------

def test_resolve_role_fallback():
    assert resolve_role("researcher").name == "researcher"
    assert resolve_role(None).name == "general"
    assert resolve_role("乱填的").name == "general"   # 未知回退 general


def test_role_tool_permissions():
    g, r, rev, t = (ROLES["general"], ROLES["researcher"],
                    ROLES["reviewer"], ROLES["tester"])
    # general 全放开
    assert g.allows("write_file") and g.allows("run_powershell") and g.allows("read_file")
    # researcher/reviewer 只读：禁写、禁命令、禁截图
    for ro in (r, rev):
        assert ro.allows("read_file") and ro.allows("grep_search") and ro.allows("recall")
        assert not ro.allows("write_file") and not ro.allows("edit_file")
        assert not ro.allows("run_powershell") and not ro.allows("take_screenshot")
    # tester：只读 + 可跑命令（动态 shell 名 run_*），但仍不能写
    assert t.allows("read_file") and t.allows("run_powershell") and t.allows("run_bash")
    assert not t.allows("write_file") and not t.allows("edit_file")


def test_researcher_allows_browser_tools():
    """深度调研（Phase B）：researcher 放行浏览器导航/浏览类 MCP 工具，但排除高风险的；
    server 前缀任意；其它只读角色不放行浏览器。"""
    r, rev, t = ROLES["researcher"], ROLES["reviewer"], ROLES["tester"]
    # 放行浏览/导航/基本交互（server 名可任意：browser__ / pw__ 都认）
    for name in ("browser__browser_navigate", "browser__browser_snapshot",
                 "browser__browser_click", "pw__browser_type", "x__browser_wait_for"):
        assert r.allows(name), name
    # 排除高风险：跑任意 JS / 传文件 / 提交表单 / 关页等
    for name in ("browser__browser_evaluate", "browser__browser_run_code_unsafe",
                 "browser__browser_file_upload", "browser__browser_fill_form",
                 "browser__browser_close"):
        assert not r.allows(name), name
    # 仅 researcher 放行浏览器；reviewer/tester 不放行
    assert not rev.allows("browser__browser_navigate")
    assert not t.allows("browser__browser_navigate")
    # researcher 仍不能写文件/跑本地命令
    assert not r.allows("write_file") and not r.allows("run_powershell")


# ---- 注册表隔离：主含 delegate；子（不传 binding）排除 delegate/update_tasks ----

def test_main_registry_has_delegate(tmp: Path):
    reg = build_registry(
        tmp,
        delegate_binding=DelegateBinding(lambda t, c: "x"),
        task_binding=TaskBinding(object(), lambda: 1, lambda e, d: None),
    )
    assert "delegate" in reg.names() and "update_tasks" in reg.names()
    assert reg.is_dangerous("delegate") is False


def test_subagent_registry_excludes_delegate_and_tasks(tmp: Path):
    # 子 Agent 注册表 = 不传 delegate_binding / task_binding
    reg = build_registry(tmp)
    assert "delegate" not in reg.names()       # 防无限嵌套
    assert "update_tasks" not in reg.names()    # 不碰主任务清单
    # 常规工具仍在
    assert "read_file" in reg.names() and "run_powershell" in reg.names()


def test_registry_filtered_by_role(tmp: Path):
    full = build_registry(tmp)  # read/write/edit/list/grep/glob/run_powershell/take_screenshot
    researcher = full.filtered(ROLES["researcher"].allows)
    names = set(researcher.names())
    # 只读：含读/搜索/代码检索，禁写/编辑/命令/截图
    assert {"read_file", "list_dir", "grep_search", "glob_search",
            "code_outline", "find_symbol"} <= names
    assert not (names & {"write_file", "edit_file", "take_screenshot"})
    assert not any(n.startswith("run_") for n in names)
    tester = full.filtered(ROLES["tester"].allows)
    assert "run_powershell" in tester.names() and "read_file" in tester.names()
    assert "write_file" not in tester.names()
    # general 不过滤
    assert set(full.filtered(ROLES["general"].allows).names()) == set(full.names())


# ---- 自定义角色（FR-10.5） ---------------------------------------------------

class _Spec:
    """模拟 config.RoleSpec（鸭子类型即可）。"""
    def __init__(self, label="", directive="", tools=None, model=None):
        self.label, self.directive, self.tools, self.model = label, directive, tools, model


def test_build_roles_custom_whitelist_and_model():
    from agentcore.tools.delegate import build_roles
    roles = build_roles({
        "docwriter": _Spec(label="文档", directive="写文档",
                           tools=["read_file", "write_file"], model="m2"),
        "free": _Spec(),                       # 不限工具
        "tester": _Spec(label="自定测试", tools=["read_file"]),  # 同名覆盖内置
        "": _Spec(label="忽略我"),             # 空名跳过
    })
    dw = roles["docwriter"]
    assert dw.label == "文档" and dw.model == "m2" and not dw.allow_all
    assert dw.allows("read_file") and dw.allows("write_file")
    assert not dw.allows("run_powershell") and not dw.allows("grep_search")  # 所列即所得
    assert roles["free"].allow_all and roles["free"].label == "free"
    assert roles["tester"].label == "自定测试" and not roles["tester"].allows("run_powershell")
    assert "" not in roles
    # 内置角色仍在
    assert roles["general"].allow_all and roles["researcher"].allows("git_status")
    # resolve 用合并表；未知仍回退 general
    assert resolve_role("docwriter", roles).model == "m2"
    assert resolve_role("不存在", roles).name == "general"


def test_delegate_tool_dynamic_schema_with_custom_roles():
    from agentcore.tools.delegate import build_roles
    roles = build_roles({"docwriter": _Spec(label="文档", tools=["read_file"])})
    tool = DelegateTool(DelegateBinding(lambda t, c, r: "ok", roles))
    enum = tool.input_schema["properties"]["role"]["enum"]
    assert "docwriter" in enum and "general" in enum and "researcher" in enum
    assert "docwriter" in tool.description and "文档" in tool.description
    assert getattr(tool, "parallel_safe", False) is True
    # 不带自定义：enum 只有内置四个
    tool2 = DelegateTool(DelegateBinding(lambda t, c, r: "ok"))
    assert sorted(tool2.input_schema["properties"]["role"]["enum"]) == \
        sorted(["general", "researcher", "reviewer", "tester"])


# ---- 并行委派（FR-10.5，loop 层） ---------------------------------------------

def test_parallel_delegates_run_concurrently(tmp: Path):
    """同一回合的多个 delegate 并发执行：总耗时≈单个耗时；结果按原调用顺序回灌。"""
    import time
    from agentcore.agent import AgentLoop, PermissionGate
    from agentcore.providers import StreamEvent, ToolCall
    from agentcore.tools import ToolRegistry
    from agentcore.tools.base import Tool

    class SlowDelegate(Tool):
        name = "delegate"
        parallel_safe = True
        description = "x"
        input_schema = {"type": "object", "properties": {}}
        def __init__(self):  # noqa: D401 — 不需 workspace
            pass
        def run(self, params):
            time.sleep(0.3)
            return f"done-{params['task']}"

    class FakeProvider:
        def __init__(self):
            self.turn = 0
        def stream_chat(self, messages, system=None, tools=None):
            self.turn += 1
            if self.turn == 1:  # 一轮发 3 个 delegate
                for i in (1, 2, 3):
                    yield StreamEvent("tool_use", meta={"call": ToolCall(
                        id=f"c{i}", name="delegate", input={"task": f"t{i}"})})
                yield StreamEvent("done", meta={"stop_reason": "tool_use"})
            else:
                yield StreamEvent("text", "汇总完成")
                yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    events = []
    loop = AgentLoop(FakeProvider(), ToolRegistry([SlowDelegate()]),
                     PermissionGate(lambda req: None), max_steps=5)
    t0 = time.time()
    msgs = loop.run([Message("user", "并行测试")], None, lambda e, d: events.append((e, d)))
    elapsed = time.time() - t0
    assert elapsed < 0.7, f"应并行（串行需 0.9s+），实际 {elapsed:.2f}s"
    # tool_result 回灌按原调用顺序
    feed = next(m for m in msgs if m.role == "user" and isinstance(m.content, list))
    ids = [b["tool_use_id"] for b in feed.content if b["type"] == "tool_result"]
    outs = [b["content"] for b in feed.content if b["type"] == "tool_result"]
    assert ids == ["c1", "c2", "c3"] and outs == ["done-t1", "done-t2", "done-t3"]
    assert sum(1 for e, _ in events if e == "tool_result") == 3


def test_single_delegate_and_serial_tools_unchanged(tmp: Path):
    """单个 delegate / 普通工具：仍走顺序路径，行为不变。"""
    from agentcore.agent import AgentLoop, PermissionGate
    from agentcore.providers import StreamEvent, ToolCall
    from agentcore.tools import ToolRegistry
    from agentcore.tools.base import Tool

    order = []

    class T(Tool):
        input_schema = {"type": "object", "properties": {}}
        description = "x"
        def __init__(self, name):
            self.name = name
        def run(self, params):
            order.append(self.name)
            return self.name

    class P:
        def __init__(self):
            self.turn = 0
        def stream_chat(self, messages, system=None, tools=None):
            self.turn += 1
            if self.turn == 1:
                yield StreamEvent("tool_use", meta={"call": ToolCall(id="a", name="t1", input={})})
                yield StreamEvent("tool_use", meta={"call": ToolCall(id="b", name="t2", input={})})
                yield StreamEvent("done", meta={"stop_reason": "tool_use"})
            else:
                yield StreamEvent("text", "ok")
                yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    loop = AgentLoop(P(), ToolRegistry([T("t1"), T("t2")]),
                     PermissionGate(lambda req: None), max_steps=5)
    loop.run([Message("user", "x")], None, lambda e, d: None)
    assert order == ["t1", "t2"]                   # 顺序保持


def test_loop_forces_summary_on_max_steps(tmp: Path):
    """撞 max_steps 时强制收尾：最后一条是 assistant 总结，委派子任务不再回灌空摘要。"""
    from agentcore.agent.loop import AgentLoop
    from agentcore.agent.gate import PermissionGate
    from agentcore.providers.base import StreamEvent, ToolCall
    from agentcore.tools.delegate import extract_summary

    class FakeProv:
        def stream_chat(self, messages, system=None, tools=None):
            if not tools:  # 收尾轮（禁用工具）→ 文本总结，而非又一个工具调用
                yield StreamEvent("text", "总结：已搜集到部分赔率数据，未抓全。")
                yield StreamEvent("done", meta={"stop_reason": "end_turn"})
                return
            yield StreamEvent("tool_use", meta={"call": ToolCall("c", "list_dir", {"path": "."})})
            yield StreamEvent("done", meta={"stop_reason": "tool_use"})

    reg = build_registry(tmp)
    loop = AgentLoop(FakeProv(), reg, PermissionGate(lambda d: None), max_steps=3)
    msgs = loop.run([Message("user", "查赔率")], None, lambda *a: None)
    assert msgs[-1].role == "assistant"
    assert "总结" in extract_summary(msgs)   # 委派能提取到非空摘要
    assert loop.hit_max_steps is True        # 撞上限标志置位（供 run_subagent 标注「子任务未完成」）


def test_loop_no_hit_flag_on_normal_finish(tmp: Path):
    """模型不再调工具、正常收尾 → 不置 hit_max_steps（结果是完整的）。"""
    from agentcore.agent.loop import AgentLoop
    from agentcore.agent.gate import PermissionGate
    from agentcore.providers.base import StreamEvent

    class FakeProv:
        def stream_chat(self, messages, system=None, tools=None):
            yield StreamEvent("text", "直接答完")
            yield StreamEvent("done", meta={"stop_reason": "end_turn"})

    loop = AgentLoop(FakeProv(), build_registry(tmp), PermissionGate(lambda d: None), max_steps=3)
    loop.run([Message("user", "hi")], None, lambda *a: None)
    assert loop.hit_max_steps is False


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
