"""P6.1 会话持久化自测（临时 db，无 GUI、无网络）。

运行：python tests/test_p6_store.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.store import Store, make_title  # noqa: E402


def _store(tmp: Path) -> Store:
    return Store(tmp / "t.db")


def test_create_and_list(tmp: Path):
    s = _store(tmp)
    a = s.create_session("会话A", "minimax")
    b = s.create_session("会话B", "claude-sonnet")
    sessions = s.list_sessions()
    assert {x["id"] for x in sessions} == {a, b}
    assert sessions[0]["title"] in ("会话A", "会话B")
    assert s.session_exists(a) and not s.session_exists(9999)


def test_messages_roundtrip_with_blocks(tmp: Path):
    s = _store(tmp)
    sid = s.create_session("t", "minimax")
    s.add_message(sid, "user", "你好")
    blocks = [
        {"type": "text", "text": "我来看看"},
        {"type": "tool_use", "id": "c1", "name": "list_dir", "input": {"path": "."}},
    ]
    s.add_message(sid, "assistant", blocks)
    s.add_message(sid, "user", [{"type": "tool_result", "tool_use_id": "c1", "content": "a.txt"}])

    msgs = s.get_messages(sid)
    assert msgs[0] == {"role": "user", "content": "你好"}
    assert msgs[1]["content"] == blocks  # list[dict] 原样还原
    assert msgs[2]["content"][0]["tool_use_id"] == "c1"


def test_rename_and_touch(tmp: Path):
    s = _store(tmp)
    sid = s.create_session("旧", "m")
    s.rename_session(sid, "新标题")
    assert s.list_sessions()[0]["title"] == "新标题"


def test_delete_cascades(tmp: Path):
    s = _store(tmp)
    sid = s.create_session("t", "m")
    s.add_message(sid, "user", "x")
    s.delete_session(sid)
    assert not s.session_exists(sid)
    assert s.get_messages(sid) == []  # 消息也被删


def test_make_title():
    assert make_title("") == "新会话"
    assert make_title("  \n ") == "新会话"
    assert make_title("短标题") == "短标题"
    long = "这是一段很长很长很长很长很长很长很长的用户输入会被截断处理掉的"
    t = make_title(long, limit=10)
    assert len(t) == 11 and t.endswith("…")  # 10 字 + 省略号
    assert make_title("第一行\n第二行") == "第一行 第二行"


def test_workspace_column(tmp: Path):
    s = _store(tmp)
    sid = s.create_session("绑项目", "m", workspace="/proj/my-app")
    assert s.get_session_workspace(sid) == "/proj/my-app"
    sid2 = s.create_session("默认", "m")  # 不绑 -> NULL（按 id 推导）
    assert s.get_session_workspace(sid2) is None
    ws = {r["id"]: r["workspace"] for r in s.list_sessions()}
    assert ws[sid] == "/proj/my-app" and ws[sid2] is None


def test_session_model_get_set(tmp: Path):
    s = _store(tmp)
    sid = s.create_session("会话", "ark-kimi")          # 创建即存初始模型
    assert s.get_session_model(sid) == "ark-kimi"
    s.set_session_model(sid, "claude-sonnet")            # 为该会话改模型
    assert s.get_session_model(sid) == "claude-sonnet"
    # 改模型不应顶动 updated_at 排序（轻量更新）
    s.set_session_model(sid, None)
    assert s.get_session_model(sid) is None


def test_migrate_adds_workspace_column(tmp: Path):
    import sqlite3
    db = tmp / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "model TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id INTEGER, role TEXT, "
        "content TEXT, created_at REAL);"
    )
    conn.execute("INSERT INTO sessions(title,model,created_at,updated_at) VALUES('旧','m',0,0)")
    conn.commit()
    conn.close()
    s = Store(db)  # 打开旧库 -> 自动补 workspace 列
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(sessions)")}
    assert "workspace" in cols
    assert s.get_session_workspace(1) is None  # 旧行新列为 NULL


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
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
