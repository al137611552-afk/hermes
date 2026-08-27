"""回合内边跑边落库 + 断在半路的历史修复（纯逻辑，不连模型、不起窗口）。

运行：python tests/test_durable_turn.py

治的是 2026-08-27 用户报的：**应用中途退出，这一轮已经搜到的内容全丢**。
根因是落库时机——回合内的消息此前跑完才一次性写库。对照 Claude Code 与 Codex 的
真实存档（本机 `~/.claude/projects/*.jsonl`、`~/.codex/sessions/**/rollout-*.jsonl`）：
两家都是**一条一行、发生即写**，tool_call 与它的 output 相隔几十毫秒各写各的。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.loop import _LiveList  # noqa: E402
from agentcore.context import INTERRUPTED_NOTE, repair_interrupted_tail  # noqa: E402
from agentcore.providers import Message  # noqa: E402


def test_every_append_is_reported_immediately():
    """一条一行、发生即写——不是跑完再一次性交出去。"""
    seen = []
    msgs = _LiveList([Message("user", "开工")], seen.append)
    msgs.append(Message("assistant", [{"type": "tool_use", "id": "t1", "name": "web_search"}]))
    msgs.append(Message("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "搜到了"}]))
    assert [m.role for m in seen] == ["assistant", "user"], "喂进去的那条不该重复上报"
    assert len(msgs) == 3


def test_hook_failure_never_takes_down_the_turn():
    """落库失败绝不能把正在跑的这一轮带走——写不进去是次要问题，任务白跑不是。"""
    def boom(_m):
        raise RuntimeError("磁盘满了")

    msgs = _LiveList([], boom)
    msgs.append(Message("assistant", "还是要留在列表里"))
    assert len(msgs) == 1


def test_slicing_still_behaves_like_a_list():
    """调用方拿 result[n_in:] 取本轮新增——包装后这个用法必须一字不改照常好使。"""
    msgs = _LiveList([Message("user", "a")], lambda m: None)
    msgs.append(Message("assistant", "b"))
    assert [m.content for m in msgs[1:]] == ["b"]
    assert isinstance(msgs[1:], list)


def test_dangling_tool_use_gets_a_tool_result():
    """断在"assistant 发了 tool_use、tool_result 还没落库"时，直接喂回模型会被 API 打回
    （tool_use 没有配对的 tool_result）——那就从"丢内容"变成"这会话再也发不出消息"。

    Claude Code 的存档里对应的是 `[Request interrupted by user for tool use]`：
    同样是**补一条 tool_result**，不是把那条 assistant 删掉。
    """
    history = [
        Message("user", "查一下"),
        Message("assistant", [{"type": "tool_use", "id": "t1", "name": "web_search"},
                              {"type": "tool_use", "id": "t2", "name": "web_fetch"}]),
    ]
    fixed, n = repair_interrupted_tail(history)
    assert n == 2, "两个 tool_use 都要补上，缺一个照样报错"
    assert len(fixed) == 3 and fixed[-1].role == "user"
    ids = [b["tool_use_id"] for b in fixed[-1].content]
    assert ids == ["t1", "t2"]
    assert INTERRUPTED_NOTE in fixed[-1].content[0]["content"]
    assert history[1] in fixed, "别把 assistant 那条删掉——它是「当时打算干什么」的唯一线索"


def test_healthy_history_is_left_alone():
    """正常收尾的历史一个字都不该动。"""
    for h in (
        [],
        [Message("user", "在吗")],
        [Message("user", "查"), Message("assistant", "查完了")],
        [Message("user", "查"),
         Message("assistant", [{"type": "tool_use", "id": "t1", "name": "web_search"}]),
         Message("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "结果"}])],
    ):
        fixed, n = repair_interrupted_tail(h)
        assert n == 0 and fixed is h


def test_text_only_assistant_tail_is_not_touched():
    """末尾是纯文本 assistant（正常说完话结束）不是中断，别乱补。"""
    h = [Message("user", "在吗"), Message("assistant", [{"type": "text", "text": "在"}])]
    fixed, n = repair_interrupted_tail(h)
    assert n == 0 and fixed is h


# ---- 端到端：真跑一轮 AgentLoop，中途炸掉，看库里还剩什么 ----------------

def test_crash_mid_turn_keeps_what_was_already_done():
    """**这条才是用户报的那件事**：一轮跑到一半应用没了，已经搜到的内容还在不在。

    用"模型第二次调用直接抛异常"模拟非正常退出——它走的是 `_run_turn` 的 except 分支，
    和崩溃一样**到不了回合末那段落库代码**。改之前这里一条都不剩。
    """
    import tempfile as _tf

    from agentcore.bridge import Api
    from agentcore.config import (AgentConfig, AppConfig, MCPConfig, MemoryConfig,
                                  ModelConfig, StorageConfig)
    from agentcore.providers.base import StreamEvent, ToolCall
    from agentcore.tools.base import Tool
    import agentcore.bridge.api as _apimod
    import agentcore.bridge.conversation as _convmod

    _apimod.persist_model_selection = lambda **k: None

    with _tf.TemporaryDirectory() as d:
        tmp = Path(d)
        api = Api(AppConfig(
            active_model="m1",
            models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
            # per_session_workspace 关掉：开着的话 _ensure_session 会换工作区、顺手重建
            # registry，把下面塞进去的替身工具冲掉（测试自己的坑，与本次改动无关）
            agent=AgentConfig(workspaces_root=str(tmp / "ws"), auto_conventions=False,
                              per_session_workspace=False, workspace=str(tmp / "ws")),
            storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
            memory=MemoryConfig(enabled=False),
            mcp=MCPConfig(enabled=False),
        ))
        conv = api.active

        class _Search(Tool):        # 只读、免确认，替身用
            name = "fake_search"
            description = "查东西"
            input_schema = {"type": "object", "properties": {"q": {"type": "string"}}}

            def run(self, params):
                return "搜到了：uv 比 pip 快 10 倍"

        conv.registry._tools["fake_search"] = _Search(conv.workspace)

        calls = {"n": 0}

        class _Provider:
            def stream_chat(self, messages, system=None, tools=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    yield StreamEvent("tool_use", meta={
                        "call": ToolCall(id="t1", name="fake_search", input={"q": "uv"})})
                    yield StreamEvent("done", meta={"stop_reason": "tool_use"})
                else:                # 第二次调用＝应用在这一步没了
                    raise RuntimeError("模拟：应用被强杀")

        _convmod.build_provider = lambda cfg, model=None: _Provider()

        r = conv.send_message("查一下 uv 和 pip 哪个快")
        assert r.get("ok") is False, "这一轮本就该以失败告终"

        rows = api.res.store.get_messages(conv.session_id)
        dumped = json.dumps(rows, ensure_ascii=False)
        assert "查一下 uv 和 pip 哪个快" in dumped, "用户那条消息本来就落库了"
        assert "fake_search" in dumped, "**模型发起的工具调用没落库**"
        assert "uv 比 pip 快 10 倍" in dumped, "**已经搜到的内容丢了——就是用户报的那件事**"

        # 重开这个会话时，断在半路的尾巴要能被修好（否则下一条消息发不出去）
        history = [Message(m["role"], m["content"]) for m in rows]
        fixed, n = repair_interrupted_tail(history)
        assert n == 0, "这次断在 tool_result 之后，配对是完整的，不该乱补"


# ---- 旁链（子 Agent）存档：留得下，但绝不回到主上下文 ----------------------

def _store(tmp):
    from agentcore.store import Store
    return Store(Path(tmp) / "h.db", externalize_images=False)


def test_sidechain_is_archived_but_never_read_back_into_history():
    """委派型调研里搜索量最大的是子 Agent，不落库＝中途退出它白搜；
    但混进主历史就把它的中间过程塞回了主模型上下文——委派的意义当场没了。"""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as d:
        st = _store(d)
        sid = st.create_session("调研", None)
        st.add_message(sid, "user", "分别调研 A B C")
        st.add_message(sid, "assistant", [{"type": "tool_use", "id": "d1", "name": "delegate"}])
        st.add_message(sid, "user", "子 Agent 搜到：A 的价格是 3999", sidechain=True)
        st.add_message(sid, "user", [{"type": "tool_result", "tool_use_id": "d1",
                                      "content": "摘要：A 3999"}])

        main = st.get_messages(sid)
        assert len(main) == 3, "主历史里不该出现子 Agent 的消息"
        assert "3999" not in json.dumps(main, ensure_ascii=False).replace("A 3999", "")

        full = st.get_messages(sid, include_sidechain=True)
        assert len(full) == 4, "存档里必须留着——这正是它的全部意义"
        assert "子 Agent 搜到" in json.dumps(full, ensure_ascii=False)

        # 落了库就能被 recall_history 捞回来（免得重复搜一遍）
        hits = st.search_messages("3999")
        assert any("3999" in h["text"] for h in hits), "存了却搜不到等于没存"


def test_truncate_counts_main_chain_and_takes_its_sidechains_along():
    """`keep` 是调用方按主历史下标给的。sidechain 行与主链交错躺在同一张表里，
    直接按行号切会砍错位置。"""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as d:
        st = _store(d)
        sid = st.create_session("s", None)
        st.add_message(sid, "user", "第1条")
        st.add_message(sid, "assistant", "第2条")
        st.add_message(sid, "user", "旁链A", sidechain=True)
        st.add_message(sid, "user", "旁链B", sidechain=True)
        st.add_message(sid, "user", "第3条")
        st.add_message(sid, "assistant", "第4条")

        st.truncate_messages_after(sid, 2)          # 只留主链前两条
        main = [m["content"] for m in st.get_messages(sid)]
        assert main == ["第1条", "第2条"], main
        full = [m["content"] for m in st.get_messages(sid, include_sidechain=True)]
        assert full == ["第1条", "第2条"], "被丢弃那几轮的旁链要一起走，别留孤儿"

        st.truncate_messages_after(sid, 0)
        assert st.get_messages(sid, include_sidechain=True) == []


def test_old_db_without_the_column_still_opens():
    """旧库没有 sidechain 列——迁移必须就地补上，不能让用户的历史打不开。"""
    import sqlite3
    import tempfile as _tf

    with _tf.TemporaryDirectory() as d:
        path = Path(d) / "old.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                model TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                created_at REAL NOT NULL);
            INSERT INTO sessions(title, created_at, updated_at) VALUES ('旧会话', 1, 1);
            INSERT INTO messages(session_id, role, content, created_at)
                VALUES (1, 'user', '"以前说的话"', 1);
        """)
        con.commit(); con.close()

        from agentcore.store import Store
        st = Store(path, externalize_images=False)
        assert [m["content"] for m in st.get_messages(1)] == ["以前说的话"]
        st.add_message(1, "user", "新的旁链", sidechain=True)
        assert len(st.get_messages(1)) == 1
        assert len(st.get_messages(1, include_sidechain=True)) == 2


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
