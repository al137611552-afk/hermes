"""同回合共享检索缓存（webcache）：不触网、不起线程池以外的东西。

运行：python tests/test_webcache.py

盯的两个硬不变量：
① **不同的东西绝不能串味**（不同 query/URL/focus 拿到彼此的结果 = 静默给错答案，比慢危险得多）；
② **绝不挂死**（单飞的等待必须有出口：leader 失败、leader 超时都要能自己跑）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.webcache import (RetrievalCache, fetch_key,  # noqa: E402
                                search_key)


def test_same_query_hits_once():
    c = RetrievalCache()
    calls = []

    def produce():
        calls.append(1)
        return "结果"

    v1, hit1 = c.get_or_call(search_key("70B 本地部署", 5), produce)
    v2, hit2 = c.get_or_call(search_key("70B 本地部署", 5), produce)
    assert (v1, hit1) == ("结果", False)
    assert (v2, hit2) == ("结果", True)
    assert len(calls) == 1, "同一个 query 出网了两次"


def test_query_normalized_but_not_fuzzy():
    """归一化只碰空白与大小写：意思相近的两个查询**必须**各搜各的。"""
    c = RetrievalCache()
    assert search_key("  Local  LLM ", 5) == search_key("local llm", 5)
    assert search_key("70B 显卡", 5) != search_key("70B 显卡推荐", 5)
    assert search_key("a", 5) != search_key("a", 10)          # 条数不同 = 不同结果


def test_fetch_key_separates_focus_and_cap():
    """同一页不同 focus 摘出来的是不同片段，合并键就会把 A 的答案给 B。"""
    u = "https://example.com/x"
    assert fetch_key(u, "显存要求", 2000) != fetch_key(u, "价格", 2000)
    assert fetch_key(u, "显存要求", 2000) != fetch_key(u, "显存要求", 8000)
    assert fetch_key(u, "  显存  要求 ", 2000) == fetch_key(u, "显存 要求", 2000)


def test_failure_is_not_cached():
    """网络抽风是瞬时的：把失败钉进整个回合，模型第二次试同一条路会拿到看不懂的旧错误。"""
    c = RetrievalCache()
    n = []

    def flaky():
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("超时")
        return "第二次成功"

    key = search_key("q", 5)
    try:
        c.get_or_call(key, flaky)
        raise AssertionError("异常没有向上抛")
    except RuntimeError:
        pass
    assert c.get_or_call(key, flaky) == ("第二次成功", False)


def test_new_turn_clears():
    """跨回合复用＝用户说「再搜一下最新的」却拿到旧结果。陈旧比多搜一次危险得多。"""
    c = RetrievalCache()
    key = search_key("今天的新闻", 5)
    c.get_or_call(key, lambda: "旧的")
    c.new_turn()
    assert c.get_or_call(key, lambda: "新的") == ("新的", False)


def test_eviction_bounds_memory():
    c = RetrievalCache(max_entries=3)
    for i in range(5):
        c.get_or_call(search_key(f"q{i}", 5), lambda i=i: f"r{i}")
    st = c.stats()
    assert st["entries"] == 3, st
    assert c.get_or_call(search_key("q0", 5), lambda: "重来")[1] is False   # 最老的已淘汰
    assert c.get_or_call(search_key("q4", 5), lambda: "不该跑")[1] is True  # 最新的还在


def test_oversized_value_not_cached():
    c = RetrievalCache(max_bytes=100)
    key = fetch_key("https://e.com", "", 100_000)
    big = "x" * 500
    assert c.get_or_call(key, lambda: big) == (big, False)
    assert c.stats()["entries"] == 0, "超大单条不该占着缓存"


def test_single_flight_second_caller_waits_and_reuses():
    """并行委派的典型形态：几个子 Agent 几乎同时发同一个查询——这时缓存还是空的。
    单飞让只有一个人出网，其余的等它。"""
    c = RetrievalCache()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return "唯一一次真跑"

    key = search_key("并发同题", 5)
    out = {}
    t1 = threading.Thread(target=lambda: out.update(a=c.get_or_call(key, slow)))
    t1.start()
    assert started.wait(5), "leader 没起来"
    t2 = threading.Thread(target=lambda: out.update(b=c.get_or_call(key, slow)))
    t2.start()
    time.sleep(0.05)          # 让跟随者确实进入等待
    release.set()
    t1.join(5); t2.join(5)
    assert out["a"] == ("唯一一次真跑", False)
    assert out["b"] == ("唯一一次真跑", True), out
    assert len(calls) == 1, f"出网了 {len(calls)} 次，单飞没生效"


def test_single_flight_follower_runs_itself_when_leader_fails():
    """leader 失败不能把跟随者一起拖死——它得自己跑一次。"""
    c = RetrievalCache()
    started = threading.Event()
    release = threading.Event()

    def leader():
        started.set()
        release.wait(5)
        raise RuntimeError("leader 挂了")

    key = search_key("leader 会挂", 5)
    err = []
    t1 = threading.Thread(target=lambda: _catch(err, c.get_or_call, key, leader))
    t1.start()
    assert started.wait(5)
    out = {}
    t2 = threading.Thread(target=lambda: out.update(b=c.get_or_call(key, lambda: "自己跑的")))
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(5); t2.join(5)
    assert isinstance(err[0], RuntimeError)
    assert out["b"] == ("自己跑的", False), out


def test_single_flight_wait_has_a_timeout():
    """leader 卡住时跟随者必须能自己走：等待有上限，不留挂死点。"""
    c = RetrievalCache(wait_timeout=0.1)
    started = threading.Event()
    release = threading.Event()

    def stuck():
        started.set()
        release.wait(5)
        return "leader 的"

    key = search_key("卡住的 leader", 5)
    t1 = threading.Thread(target=lambda: c.get_or_call(key, stuck))
    t1.start()
    assert started.wait(5)
    t0 = time.time()
    val, hit = c.get_or_call(key, lambda: "跟随者自己跑的")
    waited = time.time() - t0
    release.set(); t1.join(5)
    assert (val, hit) == ("跟随者自己跑的", False)
    assert waited < 3, f"等了 {waited:.1f}s，超时上限没生效"


def test_registry_shares_one_cache_between_main_and_sub():
    """**本功能的要害**：主 Agent 与子 Agent 的两套工具必须拿到同一个缓存实例。
    各拿一份 = 三个 researcher 照样各搜各的，等于没做。"""
    import tempfile

    from agentcore.config import WebConfig
    from agentcore.tools.registry import build_registry

    shared = RetrievalCache()
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        web = WebConfig(turn_cache=True)      # 默认已关（见 test_cache_is_off_by_default），这里验接线
        main = build_registry(ws, web=web, retrieval_cache=shared)
        sub = build_registry(ws, web=web, retrieval_cache=shared)
        assert main.get("web_search")._cache is shared
        assert sub.get("web_search")._cache is shared, "子 Agent 拿到了另一份缓存"
        assert main.get("web_fetch")._cache is shared
        # 搜索与抓取共用同一个实例（键前缀区分，互不串味）
        assert main.get("web_search")._cache is main.get("web_fetch")._cache


def test_turn_cache_off_disables_it():
    """开关关掉＝行为回到加缓存之前（传 None，工具里那条分支直接走原路）。"""
    import tempfile

    from agentcore.config import WebConfig
    from agentcore.tools.registry import build_registry

    with tempfile.TemporaryDirectory() as d:
        web = WebConfig(turn_cache=False)
        reg = build_registry(Path(d), web=web, retrieval_cache=RetrievalCache())
        assert reg.get("web_search")._cache is None
        assert reg.get("web_fetch")._cache is None


def test_cache_is_off_by_default():
    """**默认关**（2026-08-26 用户拍板）：三轮真跑命中 0 次，它治的病经查询词证伪并不存在。
    关掉后搜索路径与 v3.76.1 逐字节一致——工具拿到的是 None，走的是原来那条路。"""
    import tempfile

    from agentcore.config import WebConfig
    from agentcore.tools.registry import build_registry

    assert WebConfig().turn_cache is False
    with tempfile.TemporaryDirectory() as d:
        reg = build_registry(Path(d), web=WebConfig(), retrieval_cache=RetrievalCache())
        assert reg.get("web_search")._cache is None
        assert reg.get("web_fetch")._cache is None


def test_hit_result_tells_the_model_it_was_cached():
    """命中必须**说出来**：模型不知道是复用的，就会以为"搜两次都这样"，继续拿同样的词打转。"""
    from agentcore.tools.web import WebSearchTool

    c = RetrievalCache()
    t = WebSearchTool(cache=c)
    t._search = lambda q, n: "原始结果"          # 不触网：只验缓存这一层
    first = t.run({"query": "70B 装机", "max_results": 5})
    second = t.run({"query": "70B  装机 ", "max_results": 5})
    assert first == "原始结果"
    assert second.startswith("[缓存命中]") and "原始结果" in second
    assert "别再用同样" in second


def _catch(sink, fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        sink.append(e)


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
