"""ADR 0023 决策 4~8 轨迹固化：归并 / 参数化 / 提示词拼装 / 录制器 / 与对话的接线。

纯逻辑就地验；对话接线用 test_conversation 那套不触网的最小 Api 构造（不跑真实 agent 循环）。

运行：python tests/test_trajectory.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.bridge import Api  # noqa: E402
from agentcore.config import (  # noqa: E402
    AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
)
from agentcore.trajectory import (  # noqa: E402
    MAX_STEPS, MERGE_TARGETS, Step, TrajectoryRecorder, build_skill_prompt,
    describe_tool, digest_snapshot, merge_steps, param_candidates, steps_from_dicts,
)


class _Clock:
    """可控时钟：录制时长/步序都不该靠 sleep 来测。"""

    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float = 1.0) -> None:
        self.t += dt


def _tool(name, at=0.0, label=None, ok=True):
    return Step("tool", at, label or describe_tool(name, {}), tool=name, ok=ok)


# ---- 摘要与归并 -----------------------------------------------------------

def test_describe_tool_picks_the_telling_param():
    assert describe_tool("read_file", {"path": "src/app.py"}) == "read_file(path=src/app.py)"
    assert describe_tool("web_search", {"query": "年报 2025"}) == "web_search(query=年报 2025)"
    # 关键参数按优先级取第一个命中的（path 先于 text）
    assert describe_tool("write_file", {"text": "xx", "path": "a.md"}) == "write_file(path=a.md)"
    assert describe_tool("git_status", {}) == "git_status()"
    assert describe_tool("x", None) == "x()"
    long = describe_tool("read_file", {"path": "d/" * 100 + "a.py"})
    assert long.endswith("…)") and len(long) < 120        # 长参数截断，别把摘要撑成日志


def test_merge_only_collapses_adjacent_same_tool():
    steps = [_tool("read_file", label="read_file(path=a.py)"),
             _tool("read_file", label="read_file(path=b.py)"),
             _tool("grep_search", label="grep_search(pattern=foo)"),
             _tool("read_file", label="read_file(path=c.py)")]
    out = merge_steps(steps)
    assert [s.tool for s in out] == ["read_file", "grep_search", "read_file"]
    assert out[0].count == 2 and "a.py" in out[0].label and "b.py" in out[0].label
    # **不跨段合并**：中间插了 grep 就是新的一段——步骤顺序本身是 SOP 的信息
    assert out[2].count == 1 and out[2].label == "read_file(path=c.py)"


def test_merge_truncates_long_runs_and_carries_failure():
    steps = [_tool("read_file", label=f"read_file(path={i}.py)", ok=(i != 3))
             for i in range(MERGE_TARGETS + 3)]
    out = merge_steps(steps)
    assert len(out) == 1 and out[0].count == MERGE_TARGETS + 3
    assert out[0].label.endswith("；…")                    # 只列前几个目标
    assert out[0].ok is False                              # 有一次失败 → 整段标红（试错也是经验）


def test_merge_does_not_swallow_notes_and_says():
    steps = [_tool("web_fetch", label="web_fetch(url=https://a)"),
             Step("note", 1.0, "这个站的年报比二手媒体准"),
             _tool("web_fetch", label="web_fetch(url=https://b)")]
    assert [s.kind for s in merge_steps(steps)] == ["tool", "note", "tool"]


def test_digest_snapshot():
    snap = "- Page URL: https://example.com/reports\n- Page Title: 年度报告\n- 其余快照…"
    assert digest_snapshot(snap) == ("https://example.com/reports", "年度报告")
    assert digest_snapshot("完全认不出的快照") == ("", "")   # 认不出也不该丢掉这一步
    assert digest_snapshot(None) == ("", "")


# ---- 参数化（决策 7）------------------------------------------------------

def test_param_candidates_extract_and_rank():
    steps = [Step("tool", 0, "web_fetch(url=https://sec.example.com/2025/report.pdf)"),
             Step("say", 1, "只信 https://sec.example.com/2025/report.pdf 这种一手年报"),
             Step("tool", 2, "write_file(path=out/summary.md)"),
             Step("note", 3, "截止日期 2026-03-31，联系人 ir@example.com")]
    got = {p["value"]: p for p in param_candidates(steps)}
    assert "https://sec.example.com/2025/report.pdf" in got
    assert got["https://sec.example.com/2025/report.pdf"]["name"] == "{{网址}}"
    assert got["https://sec.example.com/2025/report.pdf"]["occurrences"] == 2
    assert got["out/summary.md"]["name"] == "{{文件}}"
    assert got["2026-03-31"]["name"] == "{{日期}}"
    assert got["ir@example.com"]["name"] == "{{账号}}"
    # URL 里的 report.pdf 不该再被单独挖成"文件"（同一个值挖两次，复用时要填两遍）
    assert "report.pdf" not in got
    # 出现两次的排在只出现一次的前面
    assert param_candidates(steps)[0]["value"] == "https://sec.example.com/2025/report.pdf"


def test_param_candidates_unique_names_and_limit():
    steps = [Step("tool", 0, "web_fetch(url=https://a.com/x)"),
             Step("tool", 1, "web_fetch(url=https://b.com/y)")]
    names = [p["name"] for p in param_candidates(steps)]
    assert names == ["{{网址}}", "{{网址2}}"]              # 同类多个 → 编号，不撞名
    many = [Step("tool", i, f"read_file(path=f{i}.py)") for i in range(20)]
    assert len(param_candidates(many, limit=3)) == 3


# ---- 提示词（决策 6+8）----------------------------------------------------

def test_build_skill_prompt_carries_the_three_musts():
    steps = [_tool("web_search", label="web_search(query=年报)"),
             Step("say", 1, "只信一手年报"), Step("note", 2, "数据在这页", detail="https://a.com")]
    prompt = build_skill_prompt(goal="查公司年报", steps=steps,
                                params=[{"name": "{{网址}}", "value": "https://a.com"}],
                                skill_name="annual-report", description="查年报的做法",
                                scope="global")
    assert "skill-creator" in prompt                        # 出口复用 /技能化 的流水线
    assert "查公司年报" in prompt
    assert "1. · web_search(query=年报)" in prompt
    assert "我说：只信一手年报" in prompt                    # 旁白权重最高，必须进提示词
    assert "https://a.com" in prompt and "{{网址}}" in prompt
    assert "不要写死坐标" in prompt                          # 决策 6：SOP 不是回放脚本
    assert "参数化是必做步骤" in prompt                      # 决策 7
    assert "可执行的验收" in prompt
    assert "全局" in prompt and "annual-report" in prompt and "查年报的做法" in prompt


def test_build_skill_prompt_survives_empty_input():
    p = build_skill_prompt(goal="", steps=[], params=[])
    assert "skill-creator" in p and "本项目" in p           # 默认落项目级


def test_steps_from_dicts_roundtrip():
    src = [_tool("read_file", label="read_file(path=a.py)"), Step("say", 1, "别改配置")]
    back = steps_from_dicts([s.as_dict() for s in src] + [{"label": ""}, "垃圾", None])
    assert [s.kind for s in back] == ["tool", "say"]        # 空 label / 非字典一律丢掉
    assert back[0].tool == "read_file" and back[1].label == "别改配置"


# ---- 录制器：人手动开关，不录时零采集 --------------------------------------

def test_recorder_only_records_between_start_and_stop():
    clock = _Clock()
    r = TrajectoryRecorder(clock)
    r.observe("tool_use", {"name": "read_file", "input": {"path": "a.py"}})
    assert r.recording is False and r.state()["steps"] == 0   # 没开就不录（决策 4）
    r.start("查年报")
    assert r.start("再来")["ok"] is False                     # 重复开始＝不合法
    r.observe("tool_use", {"name": "read_file", "input": {"path": "a.py"}})
    clock.tick(5)
    r.say("不对，应该先看年报")
    out = r.stop()
    assert out["ok"] and out["goal"] == "查年报" and out["seconds"] == 5
    assert [s["kind"] for s in out["steps"]] == ["tool", "say"]
    assert out["steps"][1]["at"] == 5.0
    assert r.recording is False
    assert r.stop()["ok"] is False                            # 停完再停：明确报错，不返回空轨迹


def test_recorder_skips_process_management_tools():
    r = TrajectoryRecorder(_Clock())
    r.start()
    r.observe("tool_use", {"name": "update_tasks", "input": {"tasks": []}})
    r.observe("tool_use", {"name": "update_notes", "input": {"text": "x"}})
    r.observe("state", {"state": "running"})                  # 非工具事件一律忽略
    r.observe("tool_use", {"name": "read_file", "input": {"path": "a.py"}})
    assert [s["tool"] for s in r.stop()["steps"]] == ["read_file"]


def test_recorder_marks_failed_tool_red():
    r = TrajectoryRecorder(_Clock())
    r.start()
    r.observe("tool_use", {"name": "web_fetch", "input": {"url": "https://a"}})
    r.observe("tool_result", {"name": "web_fetch", "ok": False, "output": "403"})
    steps = r.stop()["steps"]
    assert len(steps) == 1 and steps[0]["ok"] is False         # 失败不新增一步，只标红那步


def test_mark_captures_scene_without_periodic_snapshots():
    clock = _Clock()
    r = TrajectoryRecorder(clock)
    r.start()
    snap = "- Page URL: https://data.example.com/q3\n- Page Title: Q3 数据"
    got = r.mark("这页的表才是原始数据", snap)
    assert got["ok"] and got["steps"] == 1
    step = r.stop()["steps"][0]
    assert step["kind"] == "note" and step["label"] == "这页的表才是原始数据"
    assert "https://data.example.com/q3" in step["detail"] and "Q3 数据" in step["detail"]


def test_mark_without_note_or_browser_still_works():
    r = TrajectoryRecorder(_Clock())
    r.start()
    r.mark("", "- Page URL: https://a.com/x")
    r.mark("", "")
    labels = [s["label"] for s in r.stop()["steps"]]
    assert labels == ["记一步：https://a.com/x", "记一步"]


def test_recorder_caps_steps():
    r = TrajectoryRecorder(_Clock())
    r.start()
    for i in range(MAX_STEPS + 10):
        r.observe("tool_use", {"name": f"t{i}", "input": {}})
    st = r.state()
    assert st["steps"] == MAX_STEPS and st["full"] is True     # 到顶停手，状态条会显示已满
    assert r.stop()["truncated"] is True


def test_discard_leaves_nothing_behind():
    r = TrajectoryRecorder(_Clock())
    r.start("秘密操作")
    r.say("我的密码是 hunter2")
    r.discard()
    assert r.recording is False and r.state()["steps"] == 0
    assert r.stop()["ok"] is False                             # 丢弃即没有，取不回来


# ---- 与对话的接线（不触网）------------------------------------------------

def _api(tmp: Path) -> Api:
    cfg = AppConfig(
        active_model="m1",
        models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
        agent=AgentConfig(workspaces_root=str(tmp / "ws"), auto_conventions=False),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
        memory=MemoryConfig(enabled=False),
        mcp=MCPConfig(enabled=False),
    )
    return Api(cfg)


def test_conversation_records_tools_and_steering_notes(tmp: Path):
    """工具事件走 emit 咽喉、用户补充走 enqueue——两条路都要被录到。"""
    conv = _api(tmp).active
    conv.emit("tool_use", {"name": "read_file", "input": {"path": "a.py"}})
    assert conv.trajectory_state()["steps"] == 0               # 没开录：零采集
    conv.trajectory_start("把年报整理成表")
    conv.emit("tool_use", {"name": "web_search", "input": {"query": "年报"}})
    conv._running_turn.set()                                   # 制造"正在跑"→ 走 steering 注入路径
    conv.enqueue("不对，先看一手年报")
    conv._running_turn.clear()
    out = conv.trajectory_stop()
    kinds = [(s["kind"], s["label"]) for s in out["steps"]]
    assert kinds[0] == ("tool", "web_search(query=年报)")
    assert kinds[1] == ("say", "不对，先看一手年报")            # 中途纠正是信息密度最高的一类
    assert out["goal"] == "把年报整理成表"


def test_conversation_emits_lifecycle_events(tmp: Path):
    api = _api(tmp)
    seen: list = []
    conv = api.active
    base = conv.emit
    conv.emit = lambda e, d: (seen.append(e), base(e, d))[1]
    conv.trajectory_start("x")
    conv.trajectory_mark("记一步")
    conv.trajectory_stop()
    conv.trajectory_start("y")
    conv.trajectory_discard()
    assert seen.count("trajectory_started") == 2
    assert "trajectory_step" in seen and seen.count("trajectory_stopped") == 2


def test_conversation_compose_uses_edited_trajectory(tmp: Path):
    """固化面板里人改过的轨迹（勾掉步骤/改变量名）要能原样进提示词。"""
    conv = _api(tmp).active
    r = conv.trajectory_compose({
        "goal": "查年报", "skill_name": "annual-report", "scope": "global",
        "steps": [{"kind": "tool", "label": "web_search(query=年报)", "tool": "web_search"},
                  {"kind": "say", "label": "只信一手"}],
        "params": [{"name": "{{公司}}", "value": "示例公司"}],
    })
    assert r["ok"] and "annual-report" in r["prompt"] and "{{公司}}" in r["prompt"]
    assert "我说：只信一手" in r["prompt"] and "全局" in r["prompt"]


def test_recording_conversation_is_not_garbage_collected(tmp: Path):
    """在空白草稿里开录 → 新建会话切走：**那个对话不能被回收**（轨迹只在内存里，丢了就是丢了）。

    真机踩到的路径：草稿会话里点开录制、转头新建会话，回来发现录制"结束"了。
    """
    api = _api(tmp)
    draft = api.active
    draft.trajectory_start("录点东西")
    draft.emit("tool_use", {"name": "read_file", "input": {"path": "a.py"}})
    api.new_session()                            # 切走：草稿本该被当垃圾回收，但它在录
    assert draft.cid in api.conversations, "正在录轨迹的对话被回收了＝直接丢数据"
    assert draft.is_recording() is True
    assert api.active.trajectory_state()["recording"] is False   # 新对话自己没在录
    api.switch_conversation(draft.cid)            # 切回来：状态还在
    st = api.trajectory_state()
    assert st["recording"] is True and st["steps"] == 1
    assert api.trajectory_stop()["steps"][0]["tool"] == "read_file"


def test_empty_draft_without_recording_is_still_collected(tmp: Path):
    """反面：没在录的空草稿照旧回收（别为了修上面那条把防堆积也一起关了）。"""
    api = _api(tmp)
    draft = api.active
    api.new_session()
    assert draft.cid not in api.conversations


def test_mark_survives_a_broken_browser(tmp: Path):
    """现场抓不到（没接浏览器 / snapshot 抛错）也不该让打点失败。"""
    conv = _api(tmp).active
    conv._observe_scene = lambda: (_ for _ in ()).throw(RuntimeError("没连上"))
    conv.trajectory_start()
    assert conv.trajectory_mark("人肉步骤")["ok"] is True
    assert conv.trajectory_stop()["steps"][0]["label"] == "人肉步骤"


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
