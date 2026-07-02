"""FR-8.1：Conversation 运行时 + Api 管理器的状态隔离自测（无网络、无模型）。

只验"每对话私有状态互不串"与"Api 委派/会话切换"的结构性质，不跑真实 agent 循环
（send_message 需联网）。运行：python tests/test_conversation.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.bridge import Api, Conversation  # noqa: E402
from agentcore.config import (  # noqa: E402
    AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
)
from agentcore.providers import Message  # noqa: E402

# set_active_model/set_subagent_model 会把选择写回 APP_DIR/config.yaml（真实项目 config）。
# 测试里 patch 成 noop，避免污染——持久化逻辑由 test_model_select.py 用临时路径单独覆盖。
import agentcore.bridge.api as _apimod  # noqa: E402
_apimod.persist_model_selection = lambda **k: None


def _config(tmp: Path) -> AppConfig:
    """构造一个不触网的最小配置：双模型、持久化用临时库、记忆/MCP/自动规范全关。"""
    return AppConfig(
        active_model="m1",
        models={
            "m1": ModelConfig(provider="anthropic", model="x", api_key_env="K"),
            "m2": ModelConfig(provider="openai", model="y", api_key_env="K"),
        },
        agent=AgentConfig(
            workspaces_root=str(tmp / "ws"),
            auto_conventions=False,  # 关掉后台生成规范，避免触网
        ),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
        memory=MemoryConfig(enabled=False),  # 关记忆，避免自动抽取触网
        mcp=MCPConfig(enabled=False),
    )


def _api(tmp: Path) -> Api:
    return Api(_config(tmp))


def test_active_is_conversation(tmp: Path):
    api = _api(tmp)
    assert isinstance(api.active, Conversation)
    # 草稿对话：未落库、有自己的 gate / registry / workspace
    assert api.active.session_id is None
    assert api.active.gate is not None
    assert api.active.registry is not None
    assert api.active.workspace.name == "_scratch"


def test_ask_user_available_in_plan_mode(tmp: Path):
    """规划模式要能调 ask_user 给用户拍板（FR-11.5 + ask_user）。"""
    from agentcore.bridge.conversation import _PLAN_TOOLS
    assert "ask_user" in _PLAN_TOOLS                          # 工具集放行 ask_user
    api = _api(tmp)
    assert "ask_user" in api.active.registry.names()         # 主对话已注册
    plan_reg = api.active.registry.filtered(lambda n: n in _PLAN_TOOLS)
    assert "ask_user" in plan_reg.names()                    # 规划模式 filtered 后仍在


def test_parse_crazy_verdict():
    """自主模式末尾标记解析：DONE / CONTINUE（中英文冒号）/ 无标记。"""
    from agentcore.bridge.conversation import _parse_crazy_verdict
    assert _parse_crazy_verdict("干完了 [[DONE]]") == ("done", "")
    assert _parse_crazy_verdict("还没 [[CONTINUE: 写测试]]") == ("continue", "写测试")
    assert _parse_crazy_verdict("[[CONTINUE：中文冒号也行]]") == ("continue", "中文冒号也行")
    assert _parse_crazy_verdict("没有任何标记") == (None, "")


def test_set_crazy_mode_toggles_gate_and_ask(tmp: Path):
    """开启 crazy：危险操作免确认 + ask_user 自动放行；关闭复位。"""
    conv = _api(tmp).active
    conv.set_crazy_mode(True)
    assert conv.crazy_mode and conv.gate._allow_all and conv._ask._auto
    conv.set_crazy_mode(False)
    assert not conv.crazy_mode and not conv.gate._allow_all and not conv._ask._auto


def test_run_autonomous_loops_until_done(tmp: Path):
    """外层循环：CONTINUE 用其下一步续命、DONE 收工；指令依次传递、收尾复位。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99   # 本测试不测空转
    scripted = ["第1轮 [[CONTINUE: 继续A]]", "第2轮 [[CONTINUE: 继续B]]", "完成 [[DONE]]"]
    calls: list[str] = []
    def fake_round(prompt):
        with conv.lock:
            conv.history.append(Message("assistant", scripted[len(calls)]))
        calls.append(prompt)
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做个东西", max_rounds=10)
    assert r["reason"] == "goal_reached" and r["rounds"] == 3
    assert len(calls) == 3 and "做个东西" in calls[0] and "继续A" in calls[1] and "继续B" in calls[2]
    assert not conv.crazy_mode and not conv.gate._allow_all   # finally 复位


def test_run_autonomous_budget_exhausted(tmp: Path):
    """模型一直不收工 → 触预算上限停。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99   # 本测试只测轮数预算，不测空转
    def fake_round(prompt):
        with conv.lock:
            conv.history.append(Message("assistant", "还在干 [[CONTINUE: 继续]]"))
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做个东西", max_rounds=3)
    assert r["reason"] == "budget_exhausted" and r["rounds"] == 3


def test_run_autonomous_stops_on_cancel(tmp: Path):
    """中途 stop（_cancel.set）→ 回合间退出。"""
    conv = _api(tmp).active
    def fake_round(prompt):
        conv._cancel.set()  # 模拟用户停止
        with conv.lock:
            conv.history.append(Message("assistant", "[[CONTINUE: x]]"))
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做", max_rounds=10)
    assert r["reason"] == "stopped" and r["rounds"] == 1


def test_run_autonomous_token_budget(tmp: Path):
    """累计 token 触预算上限 → 回合间停。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99
    conv.res.config.agent.crazy_max_tokens = 100
    def fake_round(prompt):
        conv.emit("usage", {"input": 60, "output": 60})   # 每轮 120 token
        with conv.lock:
            conv.history.append(Message("assistant", "[[CONTINUE: x]]"))
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做", max_rounds=10)
    assert r["reason"] == "token_budget" and r["rounds"] == 1


def test_run_autonomous_stalls(tmp: Path):
    """连续多轮没动用任何工具（纯文字）→ 判空转停。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 2
    def fake_round(prompt):
        with conv.lock:
            conv.history.append(Message("assistant", "我在想… [[CONTINUE: 继续]]"))  # 无 tool_use
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做", max_rounds=10)
    assert r["reason"] == "stalled" and r["rounds"] == 2


def test_run_autonomous_hit_max_forces_continue(tmp: Path):
    """撞步数上限的轮即便收尾里写了 [[DONE]] 也不算完成 → 强制继续（不被截断误判达成）。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99
    calls: list[str] = []
    def fake_round(prompt):
        i = len(calls)
        conv._last_turn_hit_max = (i == 0)          # 仅第 1 轮撞上限
        with conv.lock:
            conv.history.append(Message("assistant", "总结 [[DONE]]"))  # 每轮收尾都写了 DONE
        calls.append(prompt)
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做", max_rounds=5)
    # 第1轮撞上限+DONE → 不收工、强制续；第2轮 DONE 且没撞上限 → 才真收工
    assert r["reason"] == "goal_reached" and r["rounds"] == 2


def test_enqueue_steers_during_crazy(tmp: Path):
    """crazy 运行中（_running_turn 未置位）的补充也走 steering 注入，而非另起队列/worker 并发。"""
    conv = _api(tmp).active
    conv.set_crazy_mode(True)
    r = conv.enqueue("补充需求X")
    assert r.get("steering") is True and "补充需求X" in conv._inject  # 走 steering
    assert conv._queue.empty()                                        # 没排队成新任务
    conv.set_crazy_mode(False)


def test_run_autonomous_user_inject_not_treated_as_done(tmp: Path):
    """crazy 本轮有用户中途补充时，即便模型 [[DONE]] 也不轻信 → 续一轮确认。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99
    calls: list[str] = []
    def fake_round(prompt):
        if len(calls) == 0:                  # 第 1 轮：模拟用户中途补充被 loop 消费
            conv._inject.append("再加个功能X")
            conv._take_injects()             # crazy 下会置 _last_turn_had_inject=True
        with conv.lock:
            conv.history.append(Message("assistant", "处理完了 [[DONE]]"))
        calls.append(prompt)
        return {"ok": True}
    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("做", max_rounds=5)
    # 第1轮有补充+DONE → 不收工续命；第2轮 DONE 且无补充 → 才真收工
    assert r["reason"] == "goal_reached" and r["rounds"] == 2


def test_crazy_round_uses_fresh_context_not_history(tmp: Path):
    """B/Ralph 核心：crazy 每轮只喂【本轮目标+状态】，不带 crazy 前的对话历史（隔离、不串味）。"""
    conv = _api(tmp).active
    with conv.lock:  # 模拟 crazy 前的无关对话
        conv.history.append(Message("user", "之前聊的别的事 ABC"))
        conv.history.append(Message("assistant", "好的回复 XYZ"))
    captured: dict = {}
    def fake_run_turn(messages, *, fresh=False):
        captured["messages"] = list(messages); captured["fresh"] = fresh
        return {"ok": True}
    conv._run_turn = fake_run_turn
    conv._run_crazy_round("目标GOAL 推进它")
    assert captured["fresh"] is True                 # 走 fresh 路径
    assert len(captured["messages"]) == 1            # 只喂本轮一条
    txt = captured["messages"][0].content
    assert "目标GOAL" in txt                          # 含本轮目标
    assert "ABC" not in txt and "XYZ" not in txt      # 不含 crazy 前历史
    assert any("目标GOAL" in (m.content if isinstance(m.content, str) else "")
               for m in conv.history)                 # prompt 仍落 history 供显示


def test_build_crazy_prompt_anchors_goal_and_state(tmp: Path):
    """fresh 轮的 prompt 锚定目标 + 引导写 notes/tasks + 自评标记。"""
    conv = _api(tmp).active
    p1 = conv._build_crazy_prompt("做个解析器", None, first=True)
    assert "做个解析器" in p1 and "update_tasks" in p1 and "[[DONE]]" in p1
    p2 = conv._build_crazy_prompt("做个解析器", "实现 lexer", first=False)
    assert "做个解析器" in p2 and "实现 lexer" in p2 and "跨轮记忆" in p2


def test_gate_blocks_destructive_when_allow_all(tmp: Path):
    """危险命令黑名单：免确认态下毁灭性命令仍被拦；普通命令放行。"""
    from agentcore.agent.gate import PermissionGate, is_destructive
    assert is_destructive("run_bash", {"command": "rm -rf /"})
    assert is_destructive("run_powershell", {"command": "del /s /q C:\\data"})
    assert not is_destructive("run_bash", {"command": "python -m pytest -q"})
    g = PermissionGate(lambda d: None)
    g._allow_all = True
    assert g.confirm("run_bash", {"command": "rm -rf /tmp/x"}) is False   # 黑名单拦
    assert g.confirm("run_bash", {"command": "ls -la"}) is True           # 普通放行


def test_system_injects_extra_dirs(tmp: Path):
    """授权目录后，system 须告知模型授权目录的存在与完整路径（否则它读错/臆测拒绝）。"""
    conv = _api(tmp).active
    d = tmp / "ext"; d.mkdir()
    conv.add_dir(str(d))
    sysmsg = conv._effective_system() or ""
    assert "额外授权目录" in sysmsg and str(d.resolve()) in sysmsg


def test_subagent_system_injects_extra_dirs(tmp: Path):
    """子 Agent 的 system 也须注入授权目录（否则子任务读错工作区、臆测无权限）。"""
    conv = _api(tmp).active
    d = tmp / "ext"; d.mkdir()
    conv.add_dir(str(d))
    from agentcore.tools.delegate import resolve_role
    role = resolve_role("researcher", conv._roles)
    sysmsg = conv._subagent_system(role)
    assert "额外授权目录" in sysmsg and str(d.resolve()) in sysmsg


def test_recall_memories_relevance_and_pinned(tmp: Path):
    """记忆分层召回：按当前任务相关性 top-k + 稳定用户事实(pinned)常驻；不相关的不注入。"""
    conv = _api(tmp).active
    class FakeMem:
        rows = [
            {"id": 1, "kind": "fact", "content": "crazy 模式撞步数上限要强制续命"},
            {"id": 2, "kind": "fact", "content": "add-dir 授权目录要注入子 agent system"},
            {"id": 3, "kind": "user", "content": "用户邮箱 al137611552"},
            {"id": 4, "kind": "fact", "content": "前端用 Three.js 渲染 3D 模型"},
        ]
        def list(self, limit=None): return list(self.rows)
    conv.res.memory = FakeMem()
    out = conv._recall_memories("怎么修复 crazy 撞上限的问题", limit=2)
    cs = " | ".join(m["content"] for m in out)
    assert "crazy" in cs          # 相关项被召回
    assert "用户邮箱" in cs        # 用户事实常驻（pinned）
    assert "Three.js" not in cs   # 不相关项不注入


def test_recall_principle_pinned(tmp: Path):
    """固化出的框架原则(principle)优先常驻——即便与当前任务关键词不匹配也召回。"""
    conv = _api(tmp).active
    class FakeMem:
        rows = [
            {"id": 1, "kind": "principle", "content": "框架原则：关键路径要端到端真跑验证"},
            {"id": 2, "kind": "fact", "content": "某无关细节 xyzqwer"},
        ]
        def list(self, limit=None): return list(self.rows)
    conv.res.memory = FakeMem()
    out = conv._recall_memories("完全不相关的查询 abc123", limit=5)
    assert any(m["kind"] == "principle" for m in out)   # 框架原则常驻


def test_build_consolidate_request():
    """固化请求：含新碎片 + 已有原则(去重)，要求输出 principle。"""
    from agentcore.longmem import build_consolidate_request
    system, msgs = build_consolidate_request(["碎片A", "碎片B"], ["已有原则X"])
    assert "固化" in system
    u = msgs[0].content
    assert "碎片A" in u and "已有原则X" in u and "principle" in u


def test_recall_history_search_and_tool(tmp: Path):
    """recall_history：跨会话搜原始对话记录 + 工具格式化输出（细节的无损来源）。"""
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("音响 demo", "m1")
    store.add_message(sid, "user", "帮我做个 3D 音响展示页面")
    store.add_message(sid, "assistant", "我用 Three.js 写了一个可旋转的音响模型")
    rows = store.search_messages("音响 Three.js", 5)
    assert any("Three.js" in r["text"] for r in rows)        # 跨会话检索命中
    out = api.active.registry.get("recall_history").run({"query": "音响"})
    assert "Three.js" in out and "音响 demo" in out          # 工具格式化含会话标题+原文
    empty = api.active.registry.get("recall_history").run({"query": "完全不存在的词xyzq"})
    assert "没搜到" in empty


def test_search_messages_api(tmp: Path):
    """跨会话全局搜索 API（P3）：命中返回结果、空查询/无命中安全。"""
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("音响 demo", "m1")
    store.add_message(sid, "user", "帮我做个 3D 音响展示页面")
    store.add_message(sid, "assistant", "我用 Three.js 写了一个可旋转的音响模型")
    r = api.search_messages("Three.js")
    assert r["ok"] and any("Three.js" in x["text"] for x in r["results"])
    assert r["results"][0]["session_id"] == sid and r["results"][0]["title"] == "音响 demo"
    assert api.search_messages("   ")["results"] == []          # 空查询不搜
    assert api.search_messages("绝不存在zzzq")["results"] == []  # 无命中返回空


def test_session_pinned_ordering(tmp: Path):
    """会话置顶（P3）：置顶组排在最前、组内仍按更新时间；取消置顶恢复。"""
    api = _api(tmp)
    store = api.res.store
    a = store.create_session("会话A", "m1")
    b = store.create_session("会话B", "m1")  # b 更晚创建 -> 默认在前
    assert [s["id"] for s in store.list_sessions()][:2] == [b, a]
    r = api.set_session_pinned(a, True)
    assert r["ok"]
    ids = [s["id"] for s in store.list_sessions()]
    assert ids[0] == a                       # 置顶后 a 排最前
    assert next(s for s in store.list_sessions() if s["id"] == a)["pinned"] == 1
    api.set_session_pinned(a, False)
    assert [s["id"] for s in store.list_sessions()][:2] == [b, a]  # 取消置顶恢复原序


def test_two_conversations_isolated(tmp: Path):
    api = _api(tmp)
    a = api.active
    # 第二个对话：复用同一 Resources，但工作区不同
    b = api._make_conversation(None, [], str(tmp / "projB"))

    assert a is not b
    assert a.gate is not b.gate                 # gate 各自独立
    assert a.registry is not b.registry          # registry 各自独立
    assert a.workspace != b.workspace            # 工作区独立

    # 历史互不影响
    a.history.append(Message("user", "hi-a"))
    assert len(a.history) == 1 and b.history == []
    b.history.append(Message("user", "hi-b"))
    assert len(a.history) == 1 and len(b.history) == 1

    # 共享同一份 Resources（store/memory/账本）
    assert a.res is b.res


def test_gate_allow_all_not_shared(tmp: Path):
    """一个对话的「本会话全部允许」不应泄漏到另一个对话。"""
    api = _api(tmp)
    a = api.active
    b = api._make_conversation(None, [], str(tmp / "projB"))
    a.gate._allow_all = True
    assert a.gate.confirm("write_file", {}) is True
    assert b.gate._allow_all is False  # b 不受影响


def test_set_active_model_syncs(tmp: Path):
    api = _api(tmp)
    assert api.active_model == "m1" and api.active.active_model == "m1"
    r = api.set_active_model("m2")
    assert r["ok"] and api.active_model == "m2"
    assert api.active.active_model == "m2"  # 同步到当前对话
    assert api.set_active_model("nope")["ok"] is False


def test_set_subagent_model_runtime_and_validation(tmp: Path):
    """委派模型选择：内存即时生效（委派时读 cfg.agent.subagent_model）+ 校验 + 跟随主模型。"""
    api = _api(tmp)
    assert api.get_models()["subagent"] is None          # 默认跟随主模型
    r = api.set_subagent_model("m2")
    assert r["ok"] and api.config.agent.subagent_model == "m2"   # 即时生效
    assert api.get_models()["subagent"] == "m2"
    api.set_subagent_model("")                            # 空串 = 跟随主模型
    assert api.config.agent.subagent_model is None
    assert api.set_subagent_model("nope")["ok"] is False  # 未知模型拒绝


def test_system_prompt_injects_current_model(tmp: Path):
    """system 注入当前运行的真实模型身份，避免模型被问"你是什么模型"时瞎答（如 kimi 自称 Claude）。"""
    api = _api(tmp)
    conv = api.active
    sysmsg = conv._effective_system()
    assert "x" in sysmsg and "m1" in sysmsg     # _config 的 m1 档案 model="x"，注入了 id+档案名
    conv.active_model = "m2"                      # 切到 m2（model="y"）后注入应随之变
    assert "y" in conv._effective_system()


def test_run_test_command_pass_fail(tmp: Path):
    """_run_test_command 按退出码判通过/失败（shell 执行）。"""
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.test_command = "true"
    assert conv._run_test_command()[0] is True
    conv.res.config.agent.test_command = "false"
    assert conv._run_test_command()[0] is False


def test_auto_test_loop_iterates_then_stops(tmp: Path):
    """auto_test 失败：回灌修复提示 + 续跑，限 max_iters 次；每次 emit auto_test。"""
    api = _api(tmp)
    conv = api.active
    cfg = conv.res.config.agent
    cfg.auto_test = True; cfg.test_command = "false"; cfg.test_max_iters = 2
    events = []
    conv.emit = lambda e, d: events.append((e, d))
    conv._run_test_command = lambda: (False, "boom", 1)        # 断言失败（rc=1，非命令找不到）

    class FakeLoop:
        def run(self, *a, **k): pass                            # 续跑不真调模型

    result = [Message("user", "x"), Message("assistant", "y")]
    conv._auto_test_loop(FakeLoop(), result, None)
    at = [d for e, d in events if e == "auto_test"]
    assert len(at) == 2 and all(not d["ok"] for d in at)       # 跑满 2 次、都失败
    assert any(m.role == "user" and "未通过" in str(m.content) for m in result)  # 回灌了修复提示


def test_auto_test_loop_stops_on_pass(tmp: Path):
    """测试一通过就停，不再迭代。"""
    api = _api(tmp)
    conv = api.active
    cfg = conv.res.config.agent
    cfg.auto_test = True; cfg.test_command = "x"; cfg.test_max_iters = 3
    events = []
    conv.emit = lambda e, d: events.append((e, d))
    conv._run_test_command = lambda: (True, "ok", 0)

    class FakeLoop:
        def run(self, *a, **k): pass

    conv._auto_test_loop(FakeLoop(), [Message("user", "x")], None)
    at = [d for e, d in events if e == "auto_test"]
    assert len(at) == 1 and at[0]["ok"]                        # 只跑一次、通过


def test_auto_test_disabled_noop(tmp: Path):
    """auto_test 关 或 test_command 空 -> 不跑、不 emit。"""
    api = _api(tmp)
    conv = api.active
    events = []
    conv.emit = lambda e, d: events.append((e, d))
    conv.res.config.agent.auto_test = False

    class FakeLoop:
        def run(self, *a, **k): pass

    conv._auto_test_loop(FakeLoop(), [], None)
    assert not any(e == "auto_test" for e, _ in events)


def test_auto_test_command_error_no_fix_loop(tmp: Path):
    """命令没跑起来（127/not found）→ 判为配置错：不进修复循环，emit config_error + error 提示。"""
    api = _api(tmp)
    conv = api.active
    cfg = conv.res.config.agent
    cfg.auto_test = True; cfg.test_command = "nope-cmd"; cfg.test_max_iters = 3
    events = []
    conv.emit = lambda e, d: events.append((e, d))
    conv._run_test_command = lambda: (False, "sh: nope-cmd: command not found", 127)
    fix = []

    class FakeLoop:
        def run(self, *a, **k): fix.append(1)

    conv._auto_test_loop(FakeLoop(), [], None)
    at = [d for e, d in events if e == "auto_test"]
    assert len(at) == 1 and at[0].get("config_error")   # 只判一次、标记配置错
    assert any(e == "error" for e, _ in events)          # 提示了用户
    assert not fix                                       # 没进修复循环


def test_project_level_test_command_overrides_global(tmp: Path):
    """工作区 .hermes.yaml 的 test_command 项目级优先；无则回退全局。"""
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.test_command = "global-cmd"
    assert conv._effective_test_command() == "global-cmd"          # 无 .hermes.yaml -> 全局
    (conv.workspace / ".hermes.yaml").write_text("test_command: project-cmd\n", encoding="utf-8")
    assert conv._effective_test_command() == "project-cmd"         # 项目级覆盖
    (conv.workspace / ".hermes.yaml").write_text("other: 1\n", encoding="utf-8")  # 没配该项
    assert conv._effective_test_command() == "global-cmd"          # 回退全局


def test_new_session_switches_and_captures_nothing(tmp: Path):
    api = _api(tmp)
    a = api.active
    a.history.append(Message("user", "x"))
    r = api.new_session()
    assert r["ok"]
    assert api.active is not a              # 换了新对话
    assert api.active.history == []         # 新对话空历史
    assert api.active.session_id is None
    assert a.history == [Message("user", "x")]  # 旧对话历史未被清


def test_load_session_isolated(tmp: Path):
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("会话1", "m1", workspace=None)
    store.add_message(sid, "user", "hello")
    store.add_message(sid, "assistant", "hi there")

    r = api.load_session(sid)
    assert r["ok"] and len(r["messages"]) == 2
    assert api.active.session_id == sid
    assert [m.role for m in api.active.history] == ["user", "assistant"]
    # 该会话工作区 = workspaces_root/<sid>
    assert api.active.workspace.name == str(sid)


def test_delete_active_session_clears(tmp: Path):
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("会话1", "m1", workspace=None)
    api.load_session(sid)
    assert api.active.session_id == sid
    r = api.delete_session(sid)
    assert r["ok"]
    assert api.active.session_id is None     # 当前会话被删 -> 清空
    assert api.active.history == []


def test_list_sessions_reports_active(tmp: Path):
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("会话1", "m1", workspace=None)
    api.load_session(sid)
    out = api.list_sessions()
    assert out["active"] == sid
    assert any(s["id"] == sid for s in out["sessions"])


# ---- FR-8.2a：后台 worker / 非阻塞 / 事件 cid / evaluate_js 串行化 -------

class _FakeWindow:
    """记录 evaluate_js 调用，并检测是否被并发重入（验串行化）。"""
    def __init__(self):
        self.calls: list[str] = []
        self._in = False
        self.reentered = False
        self._lock = threading.Lock()

    def evaluate_js(self, s: str):
        if self._in:                 # 上一次还没返回就被重入 -> 串行化失败
            self.reentered = True
        self._in = True
        time.sleep(0.001)            # 放大窗口，便于探测重入
        with self._lock:
            self.calls.append(s)
        self._in = False


def _parse_payload(s: str) -> dict:
    # s 形如：window.__onAgentEvent({...})
    inner = s[len("window.__onAgentEvent("):-1]
    return json.loads(inner)


def test_each_conversation_has_unique_cid(tmp: Path):
    api = _api(tmp)
    a = api.active
    b = api._make_conversation(None, [], None)
    c = api._make_conversation(None, [], None)
    assert len({a.cid, b.cid, c.cid}) == 3


def test_enqueue_nonblocking_and_worker_runs(tmp: Path):
    api = _api(tmp)
    conv = api.active
    ran, ev = [], threading.Event()

    def fake(text, attachments=None):
        ran.append(text)
        ev.set()
    conv.send_message = fake  # 避开真实模型调用

    r = conv.enqueue("hello")
    assert r == {"ok": True, "queued": True}
    assert ev.wait(2.0), "worker 未在限时内执行任务"
    assert ran == ["hello"]


def test_worker_serial_order_and_survives_error(tmp: Path):
    api = _api(tmp)
    conv = api.active
    seen, done = [], threading.Event()

    def fake(text, attachments=None):
        if text == "boom":
            raise RuntimeError("故意出错")
        seen.append(text)
        if text == "c":
            done.set()
    conv.send_message = fake

    for t in ("a", "boom", "b", "c"):
        conv.enqueue(t)
    assert done.wait(2.0), "worker 未跑完队列"
    assert seen == ["a", "b", "c"]  # boom 抛错但 worker 没死、顺序保持


def test_enqueue_steering_injects_while_running(tmp: Path):
    """执行中（_running_turn 置位）发纯文本 = steering：进注入队列、不另起新一轮；take 拉取清空。"""
    api = _api(tmp)
    conv = api.active
    conv._running_turn.set()                   # 模拟"正有一轮在跑"
    r = conv.enqueue("顺便也看下错误处理")
    assert r.get("steering") is True
    assert conv._inject == ["顺便也看下错误处理"]
    assert conv._queue.empty()                 # 没进任务队列（不会当独立新事重跑）
    assert conv._take_injects() == ["顺便也看下错误处理"]   # AgentLoop 在工具回灌时拉取
    assert conv._inject == [] and conv._take_injects() == []  # 清空、再拉为空


def test_drain_leftover_injects_to_queue(tmp: Path):
    """任务收尾仍有未被注入消费的追加（如纯文本回答无工具往返）→ 作为新一轮排进任务队列（兜底）。"""
    api = _api(tmp)
    conv = api.active
    conv._running_turn.set()
    conv.enqueue("补充A")
    conv.enqueue("补充B")
    assert conv._inject == ["补充A", "补充B"]
    conv._running_turn.clear()
    conv._drain_injects_to_queue()
    assert conv._inject == []
    assert conv._queue.get_nowait()[0] == "补充A"   # 按序成新一轮
    assert conv._queue.get_nowait()[0] == "补充B"


def test_stop_clears_pending_injects(tmp: Path):
    """停止：连待注入的执行中追加一起清掉（不会停完当前又把追加当新一轮跑）。"""
    api = _api(tmp)
    conv = api.active
    conv._running_turn.set()
    conv.enqueue("追加X")
    assert conv._inject == ["追加X"]
    conv.stop()
    assert conv._inject == []


def test_idle_enqueue_does_not_falsely_report_queued(tmp: Path):
    """空闲时发消息 = 新任务，绝不能误报 enqueued。
    回归：旧逻辑在 put+启 worker *后* 才查 state，新 worker 抢先把 state 改成 running，
    导致任务已结束却仍提示「已排队，当前任务完成后处理」。现改用 _running_turn snapshot 判断。"""
    api = _api(tmp)
    conv = api.active
    events = []
    conv.emit = lambda e, d: events.append((e, d))
    conv.send_message = lambda text, attachments=None: time.sleep(0.05)
    conv.enqueue("全新任务")              # 空闲发送
    time.sleep(0.4)                        # 让 worker 跑完整轮
    assert not any(e == "enqueued" for e, _ in events), "空闲发送被误报 enqueued"
    assert any(e == "state" for e, _ in events)  # 走的是正常 state 流转


def test_api_send_message_enqueues_nonblocking(tmp: Path):
    api = _api(tmp)
    started, release, finished = threading.Event(), threading.Event(), []

    def fake(text, attachments=None):
        started.set()
        release.wait(2.0)
        finished.append(text)
    api.active.send_message = fake

    t0 = time.time()
    r = api.send_message("hi")
    assert r.get("queued") is True
    assert time.time() - t0 < 0.5          # 立即返回，不阻塞调用方
    assert started.wait(2.0)               # worker 已开始
    assert finished == []                  # 仍卡在 release -> 确属异步
    release.set()


def test_emit_carries_cid_and_serialized(tmp: Path):
    api = _api(tmp)
    win = _FakeWindow()
    api._window = win
    a = api.active
    b = api._make_conversation(None, [], None)

    def spam(conv, n):
        for i in range(n):
            conv.emit("tick", {"i": i})

    ts = [threading.Thread(target=spam, args=(a, 50)),
          threading.Thread(target=spam, args=(b, 50))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(win.calls) == 100
    assert win.reentered is False          # evaluate_js 串行化成功（无并发重入）
    cids = set()
    for s in win.calls:
        p = _parse_payload(s)
        assert "cid" in p and "event" in p and "data" in p
        cids.add(p["cid"])
    assert cids == {a.cid, b.cid}          # 事件按来源对话带对应 cid


# ---- FR-8.2b 后端：活动对话注册表 + 切换 + 复用后台运行时 ----------------

def test_active_registered_and_returns_cid(tmp: Path):
    api = _api(tmp)
    assert api.active.cid in api.conversations
    r = api.new_session()
    assert r["ok"] and r["cid"] == api.active.cid
    assert api.list_sessions()["active_cid"] == api.active.cid


def test_switch_conversation_reactivates_live(tmp: Path):
    api = _api(tmp)
    store = api.res.store
    s1 = store.create_session("会1", "m1", workspace=None)
    s2 = store.create_session("会2", "m1", workspace=None)
    a = api.load_session(s1)["cid"]      # active=A(会1)
    b = api.load_session(s2)["cid"]      # active=B(会2)，A 留在注册表（有 session）
    assert a != b and a in api.conversations and b in api.conversations
    r = api.switch_conversation(a)        # 切回 A，不重载
    assert r["ok"] and r["cid"] == a and api.active.cid == a
    assert api.switch_conversation(999999)["ok"] is False


def test_load_session_reuses_live_runtime(tmp: Path):
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("会1", "m1", workspace=None)
    cid1 = api.load_session(sid)["cid"]   # 冷加载，建运行时 A
    api.new_session()                      # 切走（A 有 session，保活）
    r = api.load_session(sid)              # 再次加载该会话
    assert r.get("live") is True           # 复用了活的运行时
    assert r["cid"] == cid1                # 同一个对话，没新建
    assert api.active.cid == cid1


def test_empty_draft_pruned_on_leave(tmp: Path):
    api = _api(tmp)
    d0 = api.active.cid                     # 空草稿
    api.new_session()                       # 离开空草稿 -> 应被回收
    assert d0 not in api.conversations
    assert api.active.cid in api.conversations


def test_delete_active_creates_new_draft(tmp: Path):
    api = _api(tmp)
    store = api.res.store
    sid = store.create_session("会1", "m1", workspace=None)
    old_cid = api.load_session(sid)["cid"]
    r = api.delete_session(sid)
    assert r["ok"] and r["active_cid"] != old_cid
    assert api.active.session_id is None and api.active.history == []
    assert old_cid not in api.conversations   # 被删会话的运行时已丢弃


# ---- FR-8.3：后台权限路由 + 取消/停止 + 优雅收尾 -----------------------

def test_resolve_permission_routes_by_cid(tmp: Path):
    """两个对话各自挂起权限请求（req_id 都从 1 起、会撞号）-> 必须按 cid 路由。"""
    api = _api(tmp)
    a = api.active
    b = api._make_conversation(None, [], None)
    res_a, res_b = [], []
    ta = threading.Thread(target=lambda: res_a.append(a.gate.confirm("write_file", {})))
    tb = threading.Thread(target=lambda: res_b.append(b.gate.confirm("write_file", {})))
    ta.start(); tb.start()
    time.sleep(0.05)  # 等两个 confirm 进入等待
    assert a.state == "awaiting" and b.state == "awaiting"
    # 同一个 req_id=1，按 cid 分别解给各自的 gate
    assert api.resolve_permission(1, "allow", a.cid)["ok"] is True
    assert api.resolve_permission(1, "deny", b.cid)["ok"] is True
    ta.join(1.0); tb.join(1.0)
    assert res_a == [True] and res_b == [False]   # a 允许、b 拒绝，未串
    assert a.state == "running"                    # 解决后回到 running
    assert api.resolve_permission(1, "allow", 999999)["ok"] is False  # 不存在的对话


def test_stop_drains_queue_and_sets_cancel(tmp: Path):
    """stop() 应清掉尚未开始的排队任务并置取消标志。"""
    api = _api(tmp)
    conv = api.active
    started, release, seen = threading.Event(), threading.Event(), []

    def fake(text, attachments=None):
        seen.append(text)
        if text == "first":
            started.set(); release.wait(2.0)
    conv.send_message = fake

    for t in ("first", "q1", "q2"):
        conv.enqueue(t)
    assert started.wait(2.0)        # first 已开始、q1/q2 在排队
    conv.stop()
    assert conv._cancel.is_set()
    release.set()
    time.sleep(0.2)
    assert seen == ["first"]         # q1/q2 被清掉、未执行


def test_loop_stops_on_cancel():
    """AgentLoop 在回合开始前检查取消标志：已置位则一回合都不跑。"""
    from agentcore.agent.gate import PermissionGate
    from agentcore.agent.loop import AgentLoop

    class _DummyReg:
        def to_schemas(self):
            return []

    cancel = threading.Event()
    cancel.set()
    loop = AgentLoop(None, _DummyReg(), PermissionGate(lambda r: None))
    out = loop.run([], None, lambda e, d: None, cancel=cancel)
    assert out == []                # 一进 run 即被取消拦下，未触碰 provider


def test_close_stops_workers_without_hang(tmp: Path):
    """有任务卡在运行中时 close() 也应在超时内返回、不无限等。"""
    api = _api(tmp)
    conv = api.active
    started, release = threading.Event(), threading.Event()

    def fake(text, attachments=None):
        started.set(); release.wait(5.0)
    conv.send_message = fake

    conv.enqueue("x")
    assert started.wait(2.0)
    t0 = time.time()
    api.close()                     # shutdown(timeout=2) join 超时即返回
    assert time.time() - t0 < 4.0
    assert conv._stop is True
    release.set()                   # 放行卡住的假任务，worker 随后收尾退出


# ---- FR-9.3：run_subagent 集成（假 provider，不触网）---------------------

class _FakeProvider:
    """最简流式：吐一段文本就结束（无工具调用）。"""
    def __init__(self, text):
        self._text = text

    def stream_chat(self, messages, system=None, tools=None):
        from agentcore.providers import StreamEvent
        yield StreamEvent("text", self._text)
        yield StreamEvent("done", meta={"stop_reason": "end_turn"})


def test_run_subagent_emits_and_returns_summary(tmp: Path):
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    events = []
    conv.emit = lambda e, d: events.append((e, d))      # 捕获事件
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _FakeProvider("子任务已完成：改了 a.py")
    try:
        summary = conv.run_subagent("重构 a.py", "在 src/ 下")
    finally:
        convmod.build_provider = orig
    assert summary == "子任务已完成：改了 a.py"
    kinds = [e for e, _ in events]
    assert "subagent_start" in kinds and "subagent_done" in kinds
    done = next(d for e, d in events if e == "subagent_done")
    assert done["ok"] is True and done["summary"] == summary
    # 子事件里带了流式文本
    sub_evs = [d for e, d in events if e == "subagent_event"]
    assert any(x["event"] == "chunk" for x in sub_evs)


def test_subagent_registry_has_no_delegate_or_tasks(tmp: Path):
    api = _api(tmp)
    reg = api.active._subagent_registry()
    assert "delegate" not in reg.names() and "update_tasks" not in reg.names()
    assert "read_file" in reg.names()          # 常规工具仍在


def test_researcher_drops_web_tools_when_browser_present(tmp: Path):
    """浏览器穿透开着时，researcher 去掉 web_fetch+web_search（逼它走浏览器、不跳回外部搜索绕路）。"""
    from agentcore.tools.base import Tool
    from agentcore.tools.delegate import resolve_role

    class BrowserStub(Tool):
        def __init__(self, name):
            self.name = name; self.description = "x"; self.input_schema = {"type": "object", "properties": {}}
        def run(self, p): return "ok"

    conv = _api(tmp).active
    researcher = resolve_role("researcher", conv._roles)
    r0 = conv._subagent_registry(researcher)              # 无浏览器
    assert "web_search" in r0.names() and "web_fetch" in r0.names()
    conv.res.mcp_tools = [BrowserStub("browser__browser_navigate"),
                          BrowserStub("browser__browser_snapshot")]
    r1 = conv._subagent_registry(researcher)              # 有浏览器穿透
    assert "web_search" not in r1.names() and "web_fetch" not in r1.names()
    assert any("browser" in n for n in r1.names())        # 浏览器工具仍在
    # general/无浏览能力的角色不受影响（仍有 web 工具）：reviewer 不 browse
    rev = conv._subagent_registry(resolve_role("reviewer", conv._roles))
    assert "web_search" in rev.names()


def test_main_agent_drops_web_tools_when_browser_present(tmp: Path):
    """主 agent 自己查时，浏览器穿透开着也去掉 web_fetch+web_search——否则它会在浏览器
    snapshot 一时读不出内容时误判「没加载/要登录」、跳回 web_search 绕路（真机暴露的回归）。"""
    from agentcore.tools.base import Tool

    class BrowserStub(Tool):
        def __init__(self, name):
            self.name = name; self.description = "x"; self.input_schema = {"type": "object", "properties": {}}
        def run(self, p): return "ok"

    conv = _api(tmp).active
    assert "web_search" in conv.registry.names() and "web_fetch" in conv.registry.names()  # 无浏览器
    conv.res.mcp_tools = [BrowserStub("browser__browser_navigate"),
                          BrowserStub("browser__browser_snapshot")]
    conv._build_registry()                                # 有浏览器穿透后重建主注册表
    assert "web_search" not in conv.registry.names() and "web_fetch" not in conv.registry.names()
    assert any("browser" in n for n in conv.registry.names())


# ---- FR-9.4a：改动台账接入对话/Api ----------------------------------------

def test_changes_tracked_and_reverted_via_api(tmp: Path):
    api = _api(tmp)
    conv = api.active
    # 经注册表写文件 -> 台账自动入账（write_file 挂了 tracker）
    conv.registry.get("write_file").run({"path": "demo.txt", "content": "hello"})
    out = api.get_changes()
    assert out["changes"] == [{"path": "demo.txt", "status": "added"}]
    d = api.get_file_diff("demo.txt")
    assert d["ok"] and "+hello" in d["diff"]
    # 回退：新增文件 -> 删除
    assert api.revert_file("demo.txt")["ok"] is True
    assert not (conv.workspace / "demo.txt").exists()
    assert api.get_changes()["changes"] == []


def test_ledger_resets_on_workspace_change(tmp: Path):
    api = _api(tmp)
    conv = api.active
    conv.registry.get("write_file").run({"path": "old.txt", "content": "x"})
    assert conv.get_changes()
    conv.set_workspace(tmp / "another")        # 换工作区 -> 新台账
    assert conv.get_changes() == []
    # 子 Agent 注册表与主台账共用：经其 write 也入账
    conv._subagent_registry().get("write_file").run({"path": "sub.txt", "content": "y"})
    assert conv.get_changes() == [{"path": "sub.txt", "status": "added"}]


def test_shutdown_kills_background_processes(tmp: Path):
    """FR-10.3：对话运行时收尾（关窗/删会话）应杀掉其后台进程，不残留。"""
    api = _api(tmp)
    conv = api.active
    e = conv.procs.start(["bash", "-lc", "sleep 30"], str(conv.workspace), "sleep 30")
    assert e.status() == "running"
    assert "list_processes" in conv.registry.names()       # 三工具已注册
    conv.shutdown(timeout=1.0)
    deadline = time.time() + 5
    while time.time() < deadline and e.status() == "running":
        time.sleep(0.05)
    assert "exited" in e.status()


def test_plan_mode_restricts_tools_and_injects_directive(tmp: Path):
    """FR-11.5：规划模式只放行只读 + update_tasks/notes，屏蔽写/命令/委派；system 带规划指令。"""
    from agentcore.bridge.conversation import _PLAN_TOOLS
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")  # 有 session 才会注册 notes/tasks 工具
    conv._build_registry()
    # 规划工具集：只读 + 计划工具在，写/命令/截图/委派不在
    plan_reg = conv.registry.filtered(lambda n: n in _PLAN_TOOLS)
    names = set(plan_reg.names())
    assert {"read_file", "grep_search", "update_tasks", "update_notes"} <= names
    assert not (names & {"write_file", "edit_file", "multi_edit", "run_bash",
                         "delegate", "git_commit", "take_screenshot"})
    # 切换 + system 注入
    assert api.set_plan_mode(True)["plan_mode"] is True and conv.plan_mode is True
    assert "[规划模式]" in (conv._effective_system() or "")
    assert api.set_plan_mode(False)["plan_mode"] is False
    assert "[规划模式]" not in (conv._effective_system() or "")


def test_auto_review_triggers_only_on_writes(tmp: Path):
    """FR-11.2b：开 auto_review 时，本轮改过文件才派 reviewer；纯对话/取消不触发。"""
    from agentcore.providers import Message
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.auto_review = True
    calls = []
    conv.run_subagent = lambda task, ctx=None, role="general": calls.append((role, task)) or "评审通过"

    # 本轮有 write_file 调用 -> 触发 reviewer
    conv.ledger.snapshot("a.py")
    (conv.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    new_msgs = [Message("assistant", [
        {"type": "tool_use", "id": "1", "name": "write_file", "input": {"path": "a.py"}}])]
    conv._maybe_auto_review(new_msgs)
    assert len(calls) == 1 and calls[0][0] == "reviewer"

    # 纯文本回合（无写工具）-> 不触发
    calls.clear()
    conv._maybe_auto_review([Message("assistant", "就是聊聊天")])
    assert calls == []

    # 取消态 -> 不触发
    conv._cancel.set()
    conv._maybe_auto_review(new_msgs)
    assert calls == []
    conv._cancel.clear()

    # 开关关 -> 不触发
    conv.res.config.agent.auto_review = False
    conv._maybe_auto_review(new_msgs)
    assert calls == []


def test_checkpoint_create_and_restore_via_api(tmp: Path):
    """FR-11.6：建检查点 + 用户经 Api 回退（文件+任务+笔记一起还原）。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")
    conv._build_registry()
    conv.res.config.agent.auto_checkpoint = False  # 本用例手动建，关自动避免干扰
    conv.registry.get("write_file").run({"path": "a.txt", "content": "v1"})
    api.res.store.set_tasks(conv.session_id, [{"content": "步骤A", "status": "completed"}])
    api.res.store.set_notes(conv.session_id, "已确认用 v1")
    cid = conv.create_checkpoint("里程碑1")
    assert cid is not None and api.get_checkpoints()["checkpoints"][0]["id"] == cid
    (conv.workspace / "a.txt").write_text("BROKEN", encoding="utf-8")
    api.res.store.set_tasks(conv.session_id, [])
    api.res.store.set_notes(conv.session_id, "")
    r = api.restore_checkpoint(cid)
    assert r["ok"] and (conv.workspace / "a.txt").read_text(encoding="utf-8") == "v1"
    assert api.res.store.get_tasks(conv.session_id)[0]["content"] == "步骤A"
    assert api.res.store.get_notes(conv.session_id) == "已确认用 v1"


def test_auto_checkpoint_on_first_write_per_turn(tmp: Path):
    """P12 方案A：每回合首次写文件前自动打点；回退它＝撤销本回合改动；模型无 checkpoint 工具。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("first")
    conv._build_registry()
    assert "checkpoint" not in conv.registry.names()   # 模型工具已删

    # 已存在的文件 b.txt（上一回合产物）+ 新建 m.py，同回合内改两个文件
    (conv.workspace / "b.txt").write_text("旧b", encoding="utf-8")
    conv._turn_snap = {}; conv._turn_ckpt_id = None; conv._turn_meta = None
    conv._turn_label = "加功能X"
    conv.registry.get("write_file").run({"path": "m.py", "content": "v1\n"})    # 新建
    conv.registry.get("write_file").run({"path": "m.py", "content": "v2\n"})    # 同文件再写
    conv.registry.get("write_file").run({"path": "b.txt", "content": "改了b"})  # 改已有
    cps = conv.list_checkpoints()
    assert len(cps) == 1 and cps[0]["label"].startswith("改动前")   # 一回合只一个检查点
    cid = cps[0]["id"]
    # 回退＝撤销整回合：m.py 删除（回合前不存在）、b.txt 恢复"旧b"
    r = api.restore_checkpoint(cid)
    assert r["ok"]
    assert not (conv.workspace / "m.py").exists()
    assert (conv.workspace / "b.txt").read_text(encoding="utf-8") == "旧b"


def test_subagent_retries_once_on_failure(tmp: Path):
    """FR-11.6b：子循环抛异常自动重试一次；第二次成功则返回其摘要。"""
    from agentcore.providers import Message
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")
    calls = {"n": 0}

    class FlakyLoop:
        def __init__(self, *a, **k): pass
        def run(self, messages, system, emit, cancel=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("第一次崩了")
            return list(messages) + [Message("assistant", "重试后完成：摘要OK")]

    import agentcore.bridge.conversation as cv
    orig_loop, orig_prov = cv.AgentLoop, cv.build_provider
    cv.AgentLoop = FlakyLoop
    cv.build_provider = lambda cfg, model: object()   # 跳过真实 SDK/密钥
    try:
        summary = conv.run_subagent("调研一下", role="researcher")
    finally:
        cv.AgentLoop, cv.build_provider = orig_loop, orig_prov
    assert calls["n"] == 2 and "摘要OK" in summary


def test_open_external_validates_scheme(tmp: Path):
    """FR-11.1 反馈修复：外链统一走系统浏览器；仅放行 http(s)。"""
    import webbrowser
    opened = []
    orig = webbrowser.open
    webbrowser.open = lambda u: opened.append(u) or True
    try:
        api = _api(tmp)
        assert api.open_external("https://example.com")["ok"] is True
        assert opened == ["https://example.com"]
        for bad in ("javascript:alert(1)", "file:///etc/passwd", ""):
            assert api.open_external(bad)["ok"] is False
    finally:
        webbrowser.open = orig


def test_changes_mode_routes_git_vs_ledger(tmp: Path):
    """FR-10.1：git 工作区面板走 git 语义（mode=git，动态判定），非 git 沿用台账。"""
    import subprocess
    api = _api(tmp)
    conv = api.active
    assert conv.changes_mode() == "ledger"
    assert api.get_changes()["mode"] == "ledger"
    # 工作区中途变成 git 仓库 -> 面板动态切到 git 模式
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=conv.workspace, check=True, capture_output=True)
    assert conv.changes_mode() == "git"
    conv.registry.get("write_file").run({"path": "demo.txt", "content": "hello"})
    out = api.get_changes()
    assert out["mode"] == "git"
    assert {"path": "demo.txt", "status": "added"} in out["changes"]
    d = api.get_file_diff("demo.txt")
    assert d["ok"] and "+hello" in d["diff"]
    assert api.revert_file("demo.txt")["ok"] is True   # git 模式回退：未跟踪新增 -> 删除
    assert not (conv.workspace / "demo.txt").exists()
    assert api.get_changes()["changes"] == []
    # git 工具已注册且权限属性正确（只读免 gate / 写过 gate）
    assert conv.registry.is_dangerous("git_commit")
    assert not conv.registry.is_dangerous("git_status")


def test_rename_renames_auto_workspace_dir(tmp: Path):
    """改标题 → 自动工作区文件夹（workspaces_root/<id>）同步改成标题名，内容跟着搬，DB+live 更新。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("初始")
    sid = conv.session_id
    root = api.res.workspaces_root
    old_dir = root / str(sid)
    assert old_dir.exists()
    (old_dir / "f.txt").write_text("x")

    api.rename_session(sid, "修复登录bug")
    new_dir = root / "修复登录bug"
    assert new_dir.exists() and not old_dir.exists()
    assert (new_dir / "f.txt").read_text() == "x"          # 内容跟着搬
    assert api.res.store.get_session_workspace(sid) == str(new_dir)  # DB 写回
    assert conv.workspace == new_dir                        # live conv 更新


def test_rename_workspace_collision_appends_id(tmp: Path):
    """两个会话标题撞名 → 第二个文件夹加 id 后缀兜底（纯标题不够用时）。"""
    api = _api(tmp)
    c1 = api.active
    c1._ensure_session("s1"); sid1 = c1.session_id
    api.rename_session(sid1, "项目A")
    api.new_session()
    c2 = api.active
    c2._ensure_session("s2"); sid2 = c2.session_id
    api.rename_session(sid2, "项目A")
    assert (api.res.workspaces_root / "项目A").exists()
    assert (api.res.workspaces_root / f"项目A-{sid2}").exists()


def test_rename_keeps_external_bound_workspace(tmp: Path):
    """用户手动绑定的外部真实项目目录：改标题绝不重命名它。"""
    ext = tmp / "real_project"; ext.mkdir()
    api = _api(tmp)
    conv = api.active
    conv._pending_workspace = str(ext)       # 模拟打开了外部项目
    conv._ensure_session("x"); sid = conv.session_id
    api.rename_session(sid, "随便改个名")
    assert ext.exists()                                      # 外部目录原地不动
    assert api.res.store.get_session_workspace(sid) == str(ext)  # 仍绑定原路径


def test_rename_sanitizes_illegal_chars(tmp: Path):
    """标题含 Windows 非法字符 → 转义成 _ 再作文件夹名。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x"); sid = conv.session_id
    api.rename_session(sid, 'a/b:c*?')
    assert (api.res.workspaces_root / "a_b_c__").exists()


def test_rename_skips_dir_move_while_running(tmp: Path):
    """会话正在运行 → 不移动其工作区（避免占用冲突），仅改标题。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x"); sid = conv.session_id
    old_dir = api.res.workspaces_root / str(sid)
    assert old_dir.exists()                  # ensure_session 已建
    conv._running_turn.set()                 # 模拟正在执行一轮（精确避让，不用宽泛的 is_busy）
    api.rename_session(sid, "新标题")
    assert old_dir.exists()                                 # 执行中不移动文件夹
    assert api.res.store.get_session_title(sid) == "新标题"  # 但标题照常改


def test_rename_skips_dir_move_in_crazy_mode(tmp: Path):
    """crazy 自主模式运行中 → 整个期间锁住工作区，改标题不搬目录（防后台多轮自主任务丢文件）。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x"); sid = conv.session_id
    old_dir = api.res.workspaces_root / str(sid)
    assert old_dir.exists()
    conv.set_crazy_mode(True)                  # 进入 crazy；_running_turn 此刻为空（模拟两轮间空隙）
    assert not conv._running_turn.is_set()
    api.rename_session(sid, "新标题2")
    assert old_dir.exists()                                   # crazy 期间即便轮间也不移动文件夹
    assert api.res.store.get_session_title(sid) == "新标题2"   # 标题照改
    conv.set_crazy_mode(False)


def test_pending_rename_synced_after_idle(tmp: Path):
    """运行中改标题被跳过 → 记 pending；空闲后（ws_settle）自动补改文件夹名。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x"); sid = conv.session_id
    old_dir = api.res.workspaces_root / str(sid)
    assert old_dir.exists()
    conv._running_turn.set()                              # 模拟正在执行一轮
    api.rename_session(sid, "晚点改")
    assert old_dir.exists()                               # 运行中不搬目录
    assert api._pending_ws_renames.get(sid) == "晚点改"   # 但记下了 pending
    conv._running_turn.clear()                            # 空闲
    api._sync_pending_ws_rename(conv.cid)                 # 模拟 ws_settle 触发补同步
    assert not old_dir.exists() and (api.res.workspaces_root / "晚点改").exists()  # 文件夹已补改名
    assert sid not in api._pending_ws_renames             # pending 已清


def test_add_remove_extra_dir(tmp: Path):
    """add_dir/remove_dir 改 _extra_dirs；工具共享引用、授权后实时可读外部目录。"""
    api = _api(tmp)
    conv = api.active
    ext = tmp / "ext"; ext.mkdir(); (ext / "f.txt").write_text("HI")
    r = conv.add_dir(str(ext))
    assert r["ok"] and ext.resolve() in conv._extra_dirs
    assert "HI" in conv.registry.get("read_file").run({"path": str(ext / "f.txt")})  # 实时生效
    conv.remove_dir(str(ext))
    assert ext.resolve() not in conv._extra_dirs
    assert not conv.add_dir(str(ext / "nope"))["ok"]   # 非目录拒绝


def test_crazy_verify_gate(tmp: Path):
    """块2 验收门：DONE 前强制验收（无产物放行 / 无命令逼一次实跑 / 有命令红不放行·绿放行）。
    返回三元组 (指令or None, forced, 是否真红)。"""
    conv = _api(tmp).active
    conv.ledger.changes = lambda: []
    assert conv._crazy_verify_gate(False) == (None, False, False)   # 无产物 → 放行
    conv.ledger.changes = lambda: [{"path": "a.py", "status": "added"}]
    conv._effective_test_command = lambda: ""
    g, forced, failed = conv._crazy_verify_gate(False)
    assert g and "实际跑" in g and forced is True and failed is False   # 逼一次实跑，不算失败
    assert conv._crazy_verify_gate(True) == (None, True, False)      # 已逼过 → 放行
    conv._effective_test_command = lambda: "pytest -q"
    conv._run_test_command = lambda: (False, "FAILED test_x", 1)
    g2, _, failed2 = conv._crazy_verify_gate(False)
    assert g2 and "没通过" in g2 and failed2 is True                 # 真红 → 算失败（块3 据此计数）
    conv._run_test_command = lambda: (True, "ok", 0)
    assert conv._crazy_verify_gate(False) == (None, False, False)   # 绿 → 放行


def test_crazy_verdict_parse_need_user():
    """块3：[[NEED_USER]] 解析（优先于 DONE/CONTINUE），停下问用户。"""
    from agentcore.bridge.conversation import _parse_crazy_verdict
    assert _parse_crazy_verdict("...\n[[NEED_USER: 用 SQLite 还是 Postgres？]]") == \
        ("need_user", "用 SQLite 还是 Postgres？")
    assert _parse_crazy_verdict("done\n[[DONE]]") == ("done", "")
    assert _parse_crazy_verdict("[[CONTINUE: 写 P2 的测试]]") == ("continue", "写 P2 的测试")
    # need_user 与 done 同现时 need_user 优先（先停下问）
    assert _parse_crazy_verdict("[[NEED_USER: 范围?]] [[DONE]]")[0] == "need_user"


def test_crazy_verdict_parse_phase_done():
    """块4：[[PHASE_DONE]] 解析（单阶段完成）+ 优先级 need_user > done > phase_done > continue。"""
    from agentcore.bridge.conversation import _parse_crazy_verdict
    assert _parse_crazy_verdict("...\n[[PHASE_DONE: P1完成，下一步P2]]") == \
        ("phase_done", "P1完成，下一步P2")
    assert _parse_crazy_verdict("[[PHASE_DONE：中文冒号 开始P2]]") == ("phase_done", "中文冒号 开始P2")
    # 全部完成（done）优先于单阶段完成（phase_done）：避免最后一阶段误触重规划而不收尾
    assert _parse_crazy_verdict("[[PHASE_DONE: x]] [[DONE]]")[0] == "done"
    # need_user 仍最高优先（撞岔路先停下问）
    assert _parse_crazy_verdict("[[NEED_USER: q]] [[PHASE_DONE: x]]")[0] == "need_user"
    # phase_done 优先于普通 continue（阶段边界 vs 阶段中途）
    assert _parse_crazy_verdict("[[PHASE_DONE: x]] [[CONTINUE: y]]")[0] == "phase_done"


def _scripted_round(conv, scripted: list, calls: list):
    """构造一个把 scripted[i] 当本轮 assistant 输出的假 _run_crazy_round（crazy 外层测试复用）。"""
    def fake_round(prompt):
        with conv.lock:
            conv.history.append(Message("assistant", scripted[len(calls)]))
        calls.append(prompt)
        return {"ok": True}
    return fake_round


def test_run_autonomous_replans_after_phase(tmp: Path):
    """块4：阶段过验收（PHASE_DONE）→ 下一轮 prompt 注入"重规划剩余阶段"指令 + 发 crazy_replan 事件。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99
    scripted = ["P1 done [[PHASE_DONE: 完成P1，开始P2 写存储引擎]]", "全部完成 [[DONE]]"]
    calls: list[str] = []
    conv._run_crazy_round = _scripted_round(conv, scripted, calls)
    conv._crazy_verify_gate = lambda forced: (None, forced, False)  # 放行收尾，只测重规划注入
    events: list[str] = []
    base = conv.emit
    conv.emit = lambda ev, data: (events.append(ev), base(ev, data))[-1]
    r = conv.run_autonomous("做个东西", max_rounds=5)
    assert r["reason"] == "goal_reached" and len(calls) == 2
    assert "重规划" in calls[1] and "尚未完成" in calls[1]   # 第2轮带重规划指令
    assert "开始P2 写存储引擎" in calls[1]                    # 模型自报的下一阶段被接上
    assert "crazy_replan" in events                          # 发了 crazy_replan 事件（前端可展示）


def test_run_autonomous_replan_off_continues_plainly(tmp: Path):
    """crazy_replan=False：PHASE_DONE 退化成普通续命——不注入重规划，仍带模型自报的下一步。"""
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99
    conv.res.config.agent.crazy_replan = False
    scripted = ["P1 done [[PHASE_DONE: 开始P2 写存储引擎]]", "全部完成 [[DONE]]"]
    calls: list[str] = []
    conv._run_crazy_round = _scripted_round(conv, scripted, calls)
    conv._crazy_verify_gate = lambda forced: (None, forced, False)
    events: list[str] = []
    base = conv.emit
    conv.emit = lambda ev, data: (events.append(ev), base(ev, data))[-1]
    r = conv.run_autonomous("做个东西", max_rounds=5)
    assert r["reason"] == "goal_reached" and len(calls) == 2
    assert "重规划" not in calls[1]                          # 关掉：不注入重规划指令
    assert "开始P2 写存储引擎" in calls[1]                    # 仍带模型自报的下一步继续
    assert "crazy_replan" not in events                      # 关掉：不发事件


class _ReviewProvider:
    """假 provider：拆方案时吐 Decision JSON；扮 reviewer 时按角色吐评审 JSON（不碰网络）。"""
    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        from agentcore.providers import StreamEvent
        prompt = str(messages[0].content)
        if "重新排序" in prompt or "落成一份" in prompt:        # 重排任务清单 → 任务数组
            txt = '[{"content":"建 SQLite schema"},{"content":"接会话存取"}]'
        elif "拆成" in prompt:                                 # 拆方案 → Decision 列表
            txt = '[{"id":"db","title":"数据库","current_choice":"SQLite","status":"Open"},' \
                  '{"id":"idx","title":"全文检索","current_choice":"先不做","status":"Open"}]'
        elif "产品评审" in prompt:                              # 产品/市场镜头
            txt = '[{"id":"idx","status":"Accepted"}]'
        elif "技术评审" in prompt:                              # 技术镜头：把 db 升级待拍板
            txt = '[{"id":"db","status":"NeedUser","add_blocking":["SQLite vs DuckDB 需拍板"]}]'
        else:
            txt = "[]"
        yield StreamEvent("text", txt)
        yield StreamEvent("done", meta={"stop_reason": "end_turn"})


def test_design_review_end_to_end_wiring(tmp: Path):
    """ADR 0019 接线：start→评审→gate 锁→resolve 拍板→sign→gate 开（假 provider，不触网）。"""
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")                                 # 落库，拿到 session_id（apply 要写 notes/tasks）
    conv.res.config.agent.design_review = True                # 开 opt-in 开关
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _ReviewProvider()
    try:
        # 第一阶段：瞬时校验（零模型调用），面板就绪、决策尚未抽取
        r0 = conv.start_design_review("方案：用 SQLite 存会话，先不做全文检索")
        assert r0["ok"] and r0.get("ready") is True and r0["decisions"] == []
        # 第二阶段：评审模型直接读原文抽决策 + 多角色评审，回填四态
        r = conv._run_design_review_worker()
        assert r["ok"] and r["reviewed"] is True and len(r["decisions"]) == 2
        ids = {d["id"]: d for d in r["decisions"]}
        assert ids["idx"]["status"] == "Accepted"             # Product 采纳
        assert ids["db"]["status"] == "NeedUser"              # Technical 升级待拍板
        assert r["gate"]["can_start"] is False                # 有 NeedUser → 锁
        assert "%" not in r["gate"]["reason"]                 # 守禁百分比
        # 用户拍板 db=SQLite → 清未决 → 签字 → 开工
        r2 = conv.resolve_decision("db", "Accepted", "SQLite")
        assert r2["ok"] and r2["gate"]["blocking_count"] == 0
        assert conv.can_start_coding() is False               # 还没签字
        r3 = conv.sign_off_design_review()
        assert r3["can_start"] is True and conv.can_start_coding() is True
        # 落回规划/任务：共识写入 notes（不破坏原文）、Accepted 决策成待办
        conv.res.store.set_notes(conv.session_id, "原方案正文")
        ap = conv.apply_review_to_plan()
        assert ap["ok"] and ap["tasks_added"] >= 1 and ap["replanned"] is True   # 模型重排整份清单
        notes = conv.get_notes()
        assert "原方案正文" in notes and conv._REVIEW_SECTION_MARK in notes   # 原文保留 + 追加共识段
        tasks = conv.res.store.get_tasks(conv.session_id)
        assert any(t["status"] == "pending" and "SQLite" in t["content"] for t in tasks)  # 采纳项进待办
        n1 = len(tasks)
        conv.apply_review_to_plan()                                    # 幂等：再应用不重复
        assert len(conv.res.store.get_tasks(conv.session_id)) == n1
        assert conv.get_notes().count(conv._REVIEW_SECTION_MARK) == 1
        # bug#4：应用并开工后进入终态——切走再切回（=重取状态）不重现面板、不可重复开工
        gs = conv.get_design_review()
        assert gs["ok"] is False and gs.get("applied") is True
        # ↻ 重跑评审 → 撤销终态、面板复活
        conv._run_design_review_worker()
        assert conv.get_design_review()["ok"] is True
    finally:
        convmod.build_provider = orig


def test_run_design_review_is_threaded_and_streams(tmp: Path):
    """ADR 0019 v4 修：run_design_review 立即返回 started（后台线程跑），事件流式推、线程完成后回填 session。
    根因＝同步跑在 JS-API 调用里时 WebView2 的 evaluate_js 分屏事件要等整轮完才渲染。"""
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")
    conv.res.config.agent.design_review = True
    seen = []
    conv.emit = lambda event, data: seen.append(event)   # 截获事件序列
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _ReviewProvider()
    try:
        conv.start_design_review("方案：用 SQLite 存会话，先不做全文检索")
        r = conv.run_design_review()
        assert r.get("started") is True and r["ok"] is True   # 立即返回、不阻塞
        conv._review_thread.join(timeout=30)                  # 等后台线程跑完
        assert not conv._review_thread.is_alive()
        # 流式事件序列：先 seed，中途 delta（逐 token）与逐轮/逐角色，末尾 done
        assert "review_seed" in seen and "review_delta" in seen and "review_done" in seen
        assert seen[-1] == "review_done"
        assert conv.get_design_review()["ok"] is True         # 线程回填了 session
    finally:
        convmod.build_provider = orig


def test_apply_review_to_plan_blocked_before_signoff(tmp: Path):
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.design_review = True
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _ReviewProvider()
    try:
        conv.start_design_review("方案：用 SQLite 存会话，先不做全文检索")
        conv._run_design_review_worker()
        r = conv.apply_review_to_plan()          # 有 NeedUser 未拍板 → gate 未放行 → 拒绝落回
        assert r["ok"] is False and "未放行" in r["error"]
    finally:
        convmod.build_provider = orig


def test_apply_review_guards_against_double_replan(tmp: Path):
    """bug（二次重排）：开工重排耗时里 get_design_review 即转终态、重入 apply 被拒——堵住切走再切回二次开工。"""
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")
    conv.res.config.agent.design_review = True
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _ReviewProvider()
    try:
        conv.start_design_review("方案：用 SQLite 存会话，先不做全文检索")
        conv._run_design_review_worker()
        conv.resolve_decision("db", "Accepted", "SQLite")   # 清未决
        conv.sign_off_design_review()                       # 签字 → gate 放行
        conv.res.store.set_notes(conv.session_id, "原方案正文")
        checks = {}

        def slow_replan(base, consensus, done):             # 模拟重排耗时里"切走再切回"重取状态 + 重入点击
            checks["applying_terminal"] = conv.get_design_review().get("applied") is True
            checks["reentry_rejected"] = conv.apply_review_to_plan().get("ok") is False
            return [{"content": "落实：SQLite", "status": "pending"}]
        conv._replan_tasks_from_review = slow_replan
        r = conv.apply_review_to_plan()
        assert r["ok"] is True and r["replanned"] is True
        assert checks["applying_terminal"] is True          # 重排中即终态：切回不重现面板
        assert checks["reentry_rejected"] is True           # 重排中重入被拒：不会二次重排
        assert conv.get_design_review().get("applied") is True   # 结束后仍终态
        assert conv.apply_review_to_plan().get("ok") is False
    finally:
        convmod.build_provider = orig


def test_design_review_disabled_returns_error(tmp: Path):
    api = _api(tmp)
    api.active.res.config.agent.design_review = False
    r = api.active.start_design_review("方案")
    assert r["ok"] is False and "design_review" in r["error"]


class _EmptyDecomposeProvider:
    """假 provider：拆解时吐合法空数组（方案无架构级取舍，纯执行清单）。"""
    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        from agentcore.providers import StreamEvent
        yield StreamEvent("text", "这份清单没有架构级取舍：[]")
        yield StreamEvent("done", meta={"stop_reason": "end_turn"})


class _FlakyDecomposeProvider:
    """假 provider：第一次拆解吐大白话(nojson)，收紧重试(strict)时才吐合法 JSON。"""
    def __init__(self):
        self.decompose_calls = 0
    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        from agentcore.providers import StreamEvent
        prompt = str(messages[0].content)
        if "拆成" in prompt:
            self.decompose_calls += 1
            if self.decompose_calls == 1:
                txt = "好的，这个方案我理解了，主要有几个技术选型……"   # 没有 JSON
            else:
                txt = '[{"id":"fw","title":"桌面框架","current_choice":"Tauri","status":"Open"}]'
        else:
            txt = "[]"
        yield StreamEvent("text", txt)
        yield StreamEvent("done", meta={"stop_reason": "end_turn"})


def test_start_design_review_retries_once_on_nojson(tmp: Path):
    """模型第一次没吐 JSON → 收紧措辞重试一次即成功（不误报失败）。"""
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.design_review = True
    prov = _FlakyDecomposeProvider()
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: prov
    try:
        assert conv.start_design_review("方案：桌面框架用 Tauri……")["ok"] is True   # 瞬时校验
        r = conv._run_design_review_worker()                             # 抽取在此，含收紧重试
        assert r["ok"] is True and len(r["decisions"]) == 1
        assert prov.decompose_calls == 2                          # 确有一次重试
    finally:
        convmod.build_provider = orig


def test_start_design_review_falls_back_to_last_assistant_message(tmp: Path):
    """notes 为空但方案已产在对话里 → 回退取最后一条 assistant 消息，不再误报"没有可评审的方案"。"""
    api = _api(tmp)
    conv = api.active
    conv._ensure_session("x")
    conv.res.config.agent.design_review = True
    conv.history.append(Message("user", "帮我规划一个待办应用"))
    conv.history.append(Message("assistant", "## 规划：技术栈用 React + IndexedDB，先做 MVP……"))
    r = conv.start_design_review()                                # 不传 proposal_text、notes 也空
    assert r["ok"] is True and r.get("ready") is True
    assert "React" in (conv._pending_review_plan or "")          # 确用了对话里的方案原文


class _TruncatedDecomposeProvider:
    """假 provider：拆解时吐没闭合的 JSON 且 stop_reason=max_tokens（超模型上限被截断）。"""
    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        from agentcore.providers import StreamEvent
        prompt = str(messages[0].content)
        txt = '[{"id":"a","title":"框架","current_choice":"Tauri"' if "拆成" in prompt else "[]"
        yield StreamEvent("text", txt)
        yield StreamEvent("done", meta={"stop_reason": "max_tokens"})   # 截断信号


def test_run_design_review_reports_truncation_honestly(tmp: Path):
    """抽取被 max_tokens 截断 → 如实报"被截断/换高上限模型"，不误导成"没吐 JSON"、也不空转重试。"""
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.design_review = True
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _TruncatedDecomposeProvider()
    try:
        conv.start_design_review("方案：一份很大的方案……")
        r = conv._run_design_review_worker()
        assert r["ok"] is False and r.get("no_decisions") is not True
        assert "截断" in r["error"] and "没吐" not in r["error"]
    finally:
        convmod.build_provider = orig


def test_start_design_review_empty_is_no_decisions_not_error(tmp: Path):
    """纯执行清单拆不出决策 → no_decisions 诚实提示，而非"模型输出非预期"。"""
    import agentcore.bridge.conversation as convmod
    api = _api(tmp)
    conv = api.active
    conv.res.config.agent.design_review = True
    orig = convmod.build_provider
    convmod.build_provider = lambda cfg, model: _EmptyDecomposeProvider()
    try:
        assert conv.start_design_review("方案：1. 建表 2. 写接口 3. 加测试")["ok"] is True   # 瞬时校验
        r = conv._run_design_review_worker()                             # 抽取在此 → 空数组
        assert r["ok"] is False and r.get("no_decisions") is True
        assert "关键决策" in r["error"] and "非预期" not in r["error"]
        assert "raw" not in r                                     # 空数组不是解析失败，不回原文
    finally:
        convmod.build_provider = orig


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                if "tmp" in inspect.signature(fn).parameters:
                    fn(Path(d))
                else:
                    fn()
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
