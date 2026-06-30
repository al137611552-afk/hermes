"""P6.3 长期记忆自测（临时 db，无 GUI、无网络）。

覆盖：MemoryStore CRUD + 去重 + 持久化；longmem 注入块 / 转录 / 抽取请求 / 解析；
记忆工具 remember/recall/forget。抽取的模型调用不在此测（属网络），只测其纯逻辑。

运行：python tests/test_p6_memory.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.longmem import (  # noqa: E402
    build_extract_request,
    build_memory_block,
    build_transcript,
    parse_memories,
)
from agentcore.providers import Message  # noqa: E402
from agentcore.store.memory import MemoryStore  # noqa: E402
from agentcore.tools.memory import ForgetTool, RecallTool, RememberTool  # noqa: E402


def _store(tmp: Path) -> MemoryStore:
    return MemoryStore(tmp / "memory.db")


def test_add_list_search_delete(tmp: Path):
    s = _store(tmp)
    a = s.add("用户名叫 Al，偏好中文回答", "user")
    b = s.add("用户常用 hermes 系列工具，偏好命令行", "skill")
    assert a and b
    items = s.list()
    assert {m["id"] for m in items} == {a, b}
    assert items[0]["kind"] in ("user", "skill")
    hits = s.search("hermes")
    assert len(hits) == 1 and hits[0]["id"] == b
    assert s.delete(a) is True and s.delete(9999) is False
    assert {m["id"] for m in s.list()} == {b}


def test_dedup_and_kind_normalize(tmp: Path):
    s = _store(tmp)
    first = s.add("  用户 喜欢   分段交付  ", "preference")
    dup = s.add("用户 喜欢 分段交付", "preference")  # 折叠空白后等价
    assert first is not None and dup is None
    weird = s.add("某条事实", "随便写的类别")  # 非法 kind -> fact
    assert s.search("某条事实")[0]["kind"] == "fact"
    assert s.add("", "fact") is None  # 空内容不记


def test_persistence_across_reopen(tmp: Path):
    s = _store(tmp)
    s.add("跨重启应保留的记忆", "fact")
    s.close()
    s2 = MemoryStore(tmp / "memory.db")
    assert any("跨重启" in m["content"] for m in s2.list())


def test_build_memory_block_budget():
    assert build_memory_block([]) == ""
    mems = [{"content": "事实一", "kind": "fact"}, {"content": "偏好二", "kind": "preference"}]
    block = build_memory_block(mems)
    assert "长期记忆" in block and "事实一" in block and "[偏好]" in block
    # 字符预算：很小的预算只留第一条
    tiny = build_memory_block(mems, max_chars=1)
    assert "事实一" in tiny and "偏好二" not in tiny


def test_build_transcript_flattens_blocks():
    msgs = [
        Message("user", "帮我看下目录"),
        Message("assistant", [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "name": "list_dir", "input": {"path": "."}},
        ]),
        Message("user", [{"type": "tool_result", "content": "a.txt"}]),
        Message("assistant", "里面有 a.txt"),
    ]
    t = build_transcript(msgs)
    assert "用户: 帮我看下目录" in t
    assert "list_dir" in t and "a.txt" in t
    assert "助手: 里面有 a.txt" in t


def test_build_extract_request_shape():
    system, messages = build_extract_request("用户: 你好", ["已有的一条记忆"])
    assert "JSON" in system
    assert len(messages) == 1 and messages[0].role == "user"
    assert "已有的一条记忆" in messages[0].content and "你好" in messages[0].content


def test_parse_memories_variants():
    # 纯 JSON
    out = parse_memories('{"memories":[{"content":"a","kind":"user"}]}')
    assert out == [{"content": "a", "kind": "user"}]
    # 代码围栏 + 非法 kind 归一
    fenced = "```json\n{\"memories\":[{\"content\":\"b\",\"kind\":\"xxx\"}]}\n```"
    assert parse_memories(fenced) == [{"content": "b", "kind": "fact"}]
    # 前后有多余文字
    # 前后有多余文字；旧 kind "project" 现已不在 KINDS，归一为 fact
    noisy = '说明：以下是结果 {"memories":[{"content":"c","kind":"project"}]} 完毕'
    assert parse_memories(noisy) == [{"content": "c", "kind": "fact"}]
    # 空 / 垃圾 -> []
    assert parse_memories('{"memories":[]}') == []
    assert parse_memories("not json at all") == []
    assert parse_memories("") == []


def test_memory_tools(tmp: Path):
    s = _store(tmp)
    remember, recall, forget = RememberTool(s), RecallTool(s), ForgetTool(s)
    out = remember.run({"content": "记住这条", "kind": "fact"})
    assert "已记住" in out
    assert remember.run({"content": "记住这条"}) == "已存在等价的记忆，未重复记录。"
    listing = recall.run({})
    assert "记住这条" in listing
    # forget 需要 id；从 recall 拿
    mid = s.list()[0]["id"]
    assert forget.run({"id": mid}).startswith(f"已删除记忆 #{mid}")
    assert recall.run({"query": "记住"}) == "（没有匹配的长期记忆）"


def test_forget_tombstones_auto_recapture(tmp: Path):
    """forget 后，自动抽取不再把同一内容学回来；显式 remember 仍可再记。"""
    s = _store(tmp)
    mid = s.add("用户叫 Al", "user", source="tool")
    assert mid and s.delete(mid) is True and s.list() == []
    # 自动抽取想重新记同一内容 -> 被墓碑挡住
    assert s.add("用户叫 Al", "user", source="auto:1") is None
    assert s.list() == []
    # 用户显式 remember 仍可再记（并解除墓碑）
    assert s.add("用户叫 Al", "user", source="tool") is not None
    assert len(s.list()) == 1


def test_forget_by_query(tmp: Path):
    """按关键词删除所有匹配记忆，并墓碑化。"""
    s = _store(tmp)
    s.add("用户名叫 Al", "user", source="tool")
    s.add("用户的名字是 Al", "user", source="tool")
    s.add("用户偏好中文", "preference", source="tool")
    deleted = s.forget_by_query("名")  # 含"名"的两条
    assert len(deleted) == 2 and len(s.list()) == 1
    assert s.add("用户名叫 Al", "user", source="auto:1") is None  # 已忘记，不再自动记回


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
