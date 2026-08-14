"""ADR 0023 换手（request_handoff）：结果文案 / 阻塞桥 / 无人值守不放行 / 自动重读现场。

不碰网络、不起 GUI；阻塞用真线程（这条链路的价值恰恰在并发行为，mock 掉就白测了）。

运行：python tests/test_handoff.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.delegate import _READ_ONLY_TOOLS  # noqa: E402
from agentcore.tools.handoff import (  # noqa: E402
    OBSERVE_MAX_CHARS, HandoffBinding, RequestHandoffTool, compose_result,
)
from agentcore.tools.registry import build_registry  # noqa: E402


def _bg(fn):
    """把阻塞调用丢到后台线程，返回 (thread, box)；box[0] 是返回值。"""
    box: list = [None]

    def run():
        box[0] = fn()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _wait_until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


# ---- 结果文案（纯函数）---------------------------------------------------

def test_compose_done_forces_verification():
    out = compose_result("done", "重新 snapshot 看是否出现用户名")
    assert "[换手已交回]" in out
    assert "不代表真的成了" in out                     # 用户点完成 ≠ 真成了
    assert "重新 snapshot 看是否出现用户名" in out       # verify 原样带回，模型照它验


def test_compose_skipped_and_blocked_forbid_faking():
    sk = compose_result("skipped", "看页面")
    assert "[换手未完成]" in sk and "不要假装它已完成" in sk
    bl = compose_result("blocked", "看页面")
    assert "无人值守" in bl and "不要据此判定任务完成" in bl.replace("更", "")


def test_compose_defaults_and_extras():
    out = compose_result("done", "   ")                # verify 空 → 兜底成"重读现场"
    assert "重新读取一次现场状态确认" in out
    out2 = compose_result("done", "看页面", note="登录了但没权限", observed="x" * (OBSERVE_MAX_CHARS + 50))
    assert "用户补充：登录了但没权限" in out2
    assert "未经模型加工" in out2
    assert out2.count("x") == OBSERVE_MAX_CHARS        # 现场状态截断，防吃满上下文


# ---- 工具入参：三个必填都是结构性约束，不靠提示词自觉 ----------------------

def test_tool_requires_reason_target_verify():
    tool = RequestHandoffTool(HandoffBinding(lambda req: None))
    for params in ({"target": "https://a", "verify": "看"},
                   {"reason": "要登录", "verify": "看"},
                   {"reason": "要登录", "target": "https://a"}):
        try:
            tool.run(params)
        except ToolError:
            continue
        raise AssertionError(f"缺字段却没报错：{params}")


# ---- 阻塞桥：emit → 阻塞 → resolve 唤醒 ----------------------------------

def test_request_blocks_until_resolved_and_observes():
    events: list = []
    b = HandoffBinding(events.append, observer=lambda: "页面上出现了 用户名:alice")
    t, box = _bg(lambda: b.request("请登录", "https://example.com/login", "看页面是否有用户名"))
    assert _wait_until(lambda: bool(events)), "换手请求没 emit 给前端"
    req = events[0]
    assert req["target"] == "https://example.com/login" and req["unattended"] is False
    assert box[0] is None, "resolve 之前不该返回（必须真的挡住模型）"
    assert b.resolve(req["id"], "done", "登录好了")
    t.join(2)
    out = box[0]
    assert "[换手已交回]" in out and "登录好了" in out
    assert "用户名:alice" in out                      # 换手后自动重读的现场，原样回灌


def test_observer_failure_does_not_break_handoff():
    def boom():
        raise RuntimeError("浏览器没连上")

    events: list = []
    b = HandoffBinding(events.append, observer=boom)
    t, box = _bg(lambda: b.request("请登录", "https://a", "看页面"))
    assert _wait_until(lambda: bool(events))
    b.resolve(events[0]["id"], "done")
    t.join(2)
    assert "自动重读现场失败" in box[0] and "请自己动手验证" in box[0]


def test_skipped_does_not_observe():
    observed: list = []
    events: list = []
    b = HandoffBinding(events.append, observer=lambda: observed.append(1) or "现场")
    t, box = _bg(lambda: b.request("请登录", "https://a", "看页面"))
    assert _wait_until(lambda: bool(events))
    b.resolve(events[0]["id"], "skipped", "我没这个账号")
    t.join(2)
    assert "[换手未完成]" in box[0] and "我没这个账号" in box[0]
    assert not observed, "没做成就别重读现场（没意义，且会误导模型）"


def test_resolve_unknown_id_is_noop():
    b = HandoffBinding(lambda req: None)
    assert b.resolve(999, "done") is False


# ---- 无人值守：绝不放行（与 ask_user 相反）-------------------------------

def test_unattended_times_out_into_blocked():
    events: list = []
    b = HandoffBinding(events.append, wait_seconds=0.05)
    b.set_unattended(True)
    out = b.request("请登录", "https://a", "看页面")     # 无人接管：等一小会儿就收成阻塞
    assert events[0]["unattended"] is True              # 面板据此提示"没人接管会超时"
    assert "[换手未完成·无人值守]" in out
    assert "任务在此阻塞" in out
    assert b.blocked is True                            # crazy 外层据此收在「阻塞：待人工换手」
    b.clear_blocked()
    assert b.blocked is False


def test_unattended_still_accepts_a_human_who_shows_up():
    """无人值守只是把"一直等"改成"有限等待"——人真来接管了照样算数。"""
    events: list = []
    b = HandoffBinding(events.append, wait_seconds=2.0)
    b.set_unattended(True)
    t, box = _bg(lambda: b.request("请登录", "https://a", "看页面"))
    assert _wait_until(lambda: bool(events))
    b.resolve(events[0]["id"], "done")
    t.join(3)
    assert "[换手已交回]" in box[0]
    assert b.blocked is False                           # 有人接管就不是阻塞


def test_reset_wakes_waiters_without_faking_success():
    events: list = []
    b = HandoffBinding(events.append)
    t, box = _bg(lambda: b.request("请登录", "https://a", "看页面"))
    assert _wait_until(lambda: bool(events))
    b.reset()                                           # 用户点了「停止」
    t.join(2)
    assert "[换手未完成]" in box[0] and "对话被停止" in box[0]


# ---- 接线：注册表 + 子 Agent 白名单 --------------------------------------

def test_registry_wires_tool_only_when_bound(tmp: Path):
    assert "request_handoff" not in build_registry(tmp).names()   # 没绑定就没这工具（同 ask_user）
    reg = build_registry(tmp, handoff_binding=HandoffBinding(lambda req: None))
    tool = reg.get("request_handoff")
    assert reg.is_dangerous("request_handoff") is False
    assert tool.dangerous is False                      # 交还控制权是降风险动作，不过权限 gate
    assert set(tool.input_schema["required"]) == {"reason", "target", "verify"}


def test_subagent_can_hand_off():
    assert "request_handoff" in _READ_ONLY_TOOLS        # 子 Agent 撞登录墙也能换手


# ---- crazy 外层：没人接管 = 阻塞收尾，不是完成（ADR 0023 决策 2）-----------

def _api(tmp: Path):
    from agentcore.bridge import Api
    from agentcore.config import (AgentConfig, AppConfig, MCPConfig, MemoryConfig,
                                  ModelConfig, StorageConfig)
    return Api(AppConfig(
        active_model="m1",
        models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
        agent=AgentConfig(workspaces_root=str(tmp / "ws"), auto_conventions=False),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
        memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False)))


def test_crazy_mode_toggles_unattended_but_never_auto_releases(tmp: Path):
    conv = _api(tmp).active
    conv.set_crazy_mode(True)
    assert conv._ask._auto is True and conv._handoff._unattended is True
    conv.set_crazy_mode(False)
    assert conv._ask._auto is False and conv._handoff._unattended is False


# ---- FR-17 T1：三种"等你"都要冒泡成 awaiting -------------------------------
# 回归价值：换手请求只是会话内的一条消息。它不置 awaiting 的话，多会话并行时顶部计数与
# 指挥中心都看不到"有个会话停在那儿等人"——FR-15 在并发下等于失效。


def _events(conv) -> list:
    """劫持 emit 收事件（保留原 emit 的其它副作用不必要，这里只看状态广播）。"""
    got: list = []
    orig = conv.emit
    conv.emit = lambda ev, data=None: (got.append((ev, data)), orig(ev, data))[0]
    return got


def test_handoff_request_enters_awaiting_with_reason(tmp: Path):
    conv = _api(tmp).active
    got = _events(conv)
    t, box = _bg(lambda: conv._handoff.request("要登录", "https://x.example", "看到用户名"))
    assert _wait_until(lambda: conv.state == "awaiting")
    assert conv.wait_reason == "handoff"
    assert ("state", {"state": "awaiting", "reason": "handoff"}) in got
    assert conv.resolve_handoff(conv._handoff._seq, "done")["ok"] is True
    t.join(timeout=2)
    assert conv.state == "running" and conv.wait_reason is None   # 处理完回到 running


def test_ask_user_enters_awaiting_with_reason(tmp: Path):
    conv = _api(tmp).active
    t, box = _bg(lambda: conv._ask.ask("选哪个？", ["A", "B"]))
    assert _wait_until(lambda: conv.state == "awaiting")
    assert conv.wait_reason == "ask"          # 与 handoff 区分：面板要报"等什么"
    assert conv.resolve_ask_user(conv._ask._seq, "A")["ok"] is True
    t.join(timeout=2)
    assert conv.state == "running" and conv.wait_reason is None


def test_permission_request_still_reports_its_own_reason(tmp: Path):
    """权限确认本来就置 awaiting；改造后必须仍然是它自己的 reason，不被新逻辑串味。"""
    conv = _api(tmp).active
    got = _events(conv)
    conv._on_permission_request({"id": 1, "tool": "run_bash", "command": "ls"})
    assert conv.state == "awaiting" and conv.wait_reason == "permission"
    assert ("state", {"state": "awaiting", "reason": "permission"}) in got


def test_flash_window_never_raises_and_always_returns_dict(tmp: Path):
    """T3 任务栏闪烁：调用方**永远拿得到一个 dict**、绝不抛——提醒不该搞崩主流程。

    **两个平台分支都要断言**：这条原来叫 `..._off_windows`、只写了非 Windows 的期望，
    结果在 Windows CI 上必红（headless runner 没有窗口，走的是 `error` 分支而不是 `skipped`）。
    测试名里写"off windows"不会让它只在非 Windows 上跑——**得真的加平台守卫**。
    """
    api = _api(tmp)
    r = api.flash_window()
    assert isinstance(r, dict) and isinstance(r.get("ok"), bool)
    if sys.platform.startswith("win"):
        assert r["ok"] is True or "error" in r      # 有窗口才真闪；无窗口（CI）如实报错
    else:
        assert r["ok"] is False and "skipped" in r


def test_set_window_title_without_window_does_not_raise(tmp: Path):
    """没有窗口时（headless / 测试）改标题要如实返回失败，不抛。"""
    api = _api(tmp)
    r = api.set_window_title("(1 等你) Hermes")
    assert isinstance(r, dict) and r.get("ok") is False


def test_stop_clears_wait_reason(tmp: Path):
    """停止会解除三种等待——"等你"就不该再留在计数里（否则 chip 永远挂着 ✋）。"""
    conv = _api(tmp).active
    conv._enter_awaiting("handoff")
    conv.stop()
    assert conv.wait_reason is None


def test_crazy_stops_as_blocked_even_when_model_claims_done(tmp: Path):
    """本轮发生过"没人接管"→ 当轮挂起，收尾原因 handoff_blocked，**不是** goal_reached。"""
    from agentcore.providers import Message
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99

    def fake_round(prompt):
        conv._handoff.blocked = True          # 模拟：这一轮里换手等超时了
        with conv.lock:
            conv.history.append(Message("assistant", "都干完了 [[DONE]]"))
        return {"ok": True}

    conv._run_crazy_round = fake_round
    r = conv.run_autonomous("去某站取数", max_rounds=5)
    assert r["reason"] == "handoff_blocked" and r["rounds"] == 1
    assert conv._handoff.blocked is True       # 结论留给外层读，run 完再清


def test_crazy_clears_stale_blocked_from_last_run(tmp: Path):
    from agentcore.providers import Message
    conv = _api(tmp).active
    conv.res.config.agent.crazy_stall_rounds = 99
    conv._handoff.blocked = True               # 上一轮自主任务留下的结论

    def fake_round(prompt):
        with conv.lock:
            conv.history.append(Message("assistant", "做完了 [[DONE]]"))
        return {"ok": True}

    conv._run_crazy_round = fake_round
    assert conv.run_autonomous("换个任务", max_rounds=3)["reason"] == "goal_reached"


def _run_all():
    import inspect
    import tempfile
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        # ignore_cleanup_errors：Windows 上 sqlite 连接还开着就删不掉 .db（WinError 32），
        # 而清理失败发生在断言全过之后，不该把测试判红（Linux 允许删已打开的文件，故只在 Windows 现形）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
