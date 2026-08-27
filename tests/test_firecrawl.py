"""Firecrawl 托管检索源（FR-11.1d）自检：解析、升级闸、降级、渲染复用。

不联网、不用 key。真网链路由评测的 `net_shopping_budget`（network=True）覆盖。

这一层要守住的底线只有一条：**免 key 链路必须永远可用**。
没有 key、key 失效、Firecrawl 抽风——都只能让搜索"少一个源"，绝不能让搜索失败。
不带凭据也能搜是 hermes 的底线能力（同 v3.56「开箱不预设 provider」的立场）。

运行：python tests/test_firecrawl.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.web import (  # noqa: E402
    FIRECRAWL_KEY_ENV, FIRECRAWL_MODES, FIRECRAWL_READ_BUDGET, FirecrawlQuotaError, WebFetchTool,
    WebSearchTool, firecrawl_gain, firecrawl_key, firecrawl_quota_exhausted, parse_firecrawl,
    parse_firecrawl_page, render_items, reset_firecrawl_quota, upgrade_reason,
)
from agentcore.tools import web as web_mod  # noqa: E402


def _items(n, prefix="a"):
    return [{"title": f"{prefix}{i}", "url": f"https://{prefix}{i}.example.org/x",
             "snippet": "s"} for i in range(n)]


class _NoKey:
    """临时清掉环境里的 key（本机 .env 里有真 key，测试绝不能依赖它）。"""

    def __enter__(self):
        self._old = os.environ.pop(FIRECRAWL_KEY_ENV, None)
        return self

    def __exit__(self, *a):
        if self._old is not None:
            os.environ[FIRECRAWL_KEY_ENV] = self._old


class _FakeKey:
    """装一把假 key：让"有 key"的分支跑起来，但**绝不联网**（scrape 一律打桩）。"""

    def __enter__(self):
        self._old = os.environ.get(FIRECRAWL_KEY_ENV)
        os.environ[FIRECRAWL_KEY_ENV] = "fc-test-not-a-real-key"
        return self

    def __exit__(self, *a):
        if self._old is None:
            os.environ.pop(FIRECRAWL_KEY_ENV, None)
        else:
            os.environ[FIRECRAWL_KEY_ENV] = self._old


class _Patch:
    """临时替换 web 模块里的函数（_http_get / firecrawl_scrape）。"""

    def __init__(self, **kw):
        self._kw = kw

    def __enter__(self):
        self._old = {k: getattr(web_mod, k) for k in self._kw}
        for k, v in self._kw.items():
            setattr(web_mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self._old.items():
            setattr(web_mod, k, v)


# 真网实测到的两段样本（2026-08-20），拿来当判据的固定语料：
# 知乎 403 页 Firecrawl 拿回来的拦截插页 / app.slack.com/help 的 JS 空壳与渲染后正文。
_ZHIHU_INTERSTITIAL = ("![ZhiHu logo](https://static.zhihu.com/x.png)\n\n"
                       "# 你似乎来到了没有知识存在的荒原\n\n5 秒后自动跳转至回答所在的问题页\n" + "。" * 180)
_SHELL_TEXT = "Slack Help Center" + " " * 100        # 直读只有百来字符
_RENDERED = "Slack 帮助中心：如何创建频道……" * 80      # 渲染后一千多字符


def _blocked_html(text="短"):
    return f"<html><body><div>{text}</div></body></html>"


def _fake_get(text=None, ctype="text/html; charset=utf-8", raises=None):
    def _get(url, timeout):
        if raises:
            raise ToolError(raises)
        return url, _blocked_html(text if text is not None else "短"), ctype
    return _get


# ---- 解析：与别的引擎同构 ------------------------------------------------------

def test_parse_maps_onto_the_shared_result_shape():
    """同构是关键——同构才能直接进 `fuse_results` 的 RRF，跟 bing/ddg 一视同仁。"""
    payload = {"success": True, "data": {"web": [
        {"url": "https://docs.python.org/3/library/functools.html",
         "title": "functools", "description": "lru_cache(maxsize=128)", "position": 1},
    ]}}
    out = parse_firecrawl(payload)
    assert out == [{"title": "functools",
                    "url": "https://docs.python.org/3/library/functools.html",
                    "snippet": "lru_cache(maxsize=128)"}]


def test_parse_is_forgiving_about_shape_changes():
    """一个源的抽风不该拖垮整次搜索：字段缺失/形状变了就跳过该条，绝不抛异常。"""
    assert parse_firecrawl({}) == []
    assert parse_firecrawl({"data": None}) == []
    assert parse_firecrawl({"data": {"web": [{"url": "ftp://x"}, {"nope": 1}, "字符串"]}}) == []
    # 顶层直接给数组（形状变了）也认
    got = parse_firecrawl({"data": [{"url": "https://a.org", "title": "T"}]})
    assert got and got[0]["url"] == "https://a.org" and got[0]["snippet"] == ""


def test_parse_falls_back_to_url_as_title():
    got = parse_firecrawl({"data": {"web": [{"url": "https://a.org/p"}]}})
    assert got[0]["title"] == "https://a.org/p"


# ---- 升级闸：判据必须可证伪 ----------------------------------------------------

def test_zero_and_thin_recall_trigger_upgrade():
    assert upgrade_reason("q", [], 5) == "免 key 链路零结果"
    assert "只召回 2 条" in (upgrade_reason("q", _items(2), 5) or "")
    assert upgrade_reason("q", _items(5), 5) is None


def test_thin_recall_threshold_is_half():
    """低于要的一半才算没召回住。定得太松会把正常搜索也升级掉（白烧配额）。"""
    assert upgrade_reason("q", _items(3), 5) is None      # 3/5 够了
    assert upgrade_reason("q", _items(2), 5) is not None  # 2/5 不够
    assert upgrade_reason("q", _items(1), 1) is None      # want=1 时别永远升级


def test_quality_gate_reuses_block_h1_facts():
    """质量判据直接复用块H1 `ResearchEvaluator` 的 blocker，不另造一套。

    这里用真评估器跑：query 有预算上限、结果有标价却无一在预算内 → 可证伪的硬事实。
    """
    from agentcore.agent.evaluators import evaluate

    pricey = [{"title": f"键盘{i}", "url": f"https://shop{i}.example.cn/x",
               "snippet": f"售价 ¥{899 + i}"} for i in range(5)]
    why = upgrade_reason("机械键盘 500元以内", pricey, 5, evaluate)
    assert why and "预算" in why, why
    cheap = [{"title": f"键盘{i}", "url": f"https://shop{i}.example.cn/x",
              "snippet": f"售价 ¥{199 + i}"} for i in range(5)]
    assert upgrade_reason("机械键盘 500元以内", cheap, 5, evaluate) is None


def test_quality_gate_failure_never_blocks_search():
    """评估器炸了只能放行，不能把搜索也带走。"""
    def boom(*a, **k):
        raise RuntimeError("evaluator 炸了")

    assert upgrade_reason("q", _items(5), 5, boom) is None


# ---- 没 key / 坏 key：只能少一个源，不能让搜索失败 -----------------------------

def test_no_key_means_off():
    with _NoKey():
        assert firecrawl_key() == ""
        tool = WebSearchTool(firecrawl="always")
        try:
            tool._search_firecrawl("q", 5)
        except ToolError as e:
            assert FIRECRAWL_KEY_ENV in str(e)
        else:
            raise AssertionError("没 key 却没报错")


def test_unknown_mode_degrades_to_off():
    """配置写错字（拼错、写成 true）不许变成"每次都计费"——脏值一律归 off。"""
    for bad in ("", None, "yes", "true", "FALLBACK ", "Always"):
        mode = WebSearchTool(firecrawl=bad)._firecrawl
        assert mode in FIRECRAWL_MODES, (bad, mode)
        if not str(bad or "").strip().lower() in FIRECRAWL_MODES:
            assert mode == "off", (bad, mode)


def test_modes_are_exactly_four():
    assert FIRECRAWL_MODES == ("off", "fallback", "primary", "always")
    assert WebSearchTool()._firecrawl == "off"          # 构造器默认＝老行为，零变化


# ================= primary：主搜档（2026-08-20 起的默认） ========================
# 改默认的理由是**实测**：fallback 那三条判据在真机上几乎从不触发，于是用户给的 key
# 什么也没买到。既然给了更好的源，就该默认用它——而不是等免 key 链路先失败。

def test_primary_uses_firecrawl_first_and_skips_free_chain():
    """主源够用时，免 key 链路一次都不该跑（省时间；它本来也不花钱，省的是延迟）。"""
    free = []
    with _FakeKey(), _Patch(_http_get=_fake_get()):
        tool = WebSearchTool(firecrawl="primary", max_results=5)
        tool._search_firecrawl = lambda q, n: _items(5, "fc")
        tool._gather = lambda engines, q: (free.append(engines) or ([], []))
        out = tool.run({"query": "q"})
    assert not free, "主源够用还去跑免 key 链路"
    assert "[搜索结果·firecrawl]" in out, out[:120]


def test_primary_tops_up_from_free_chain_when_thin():
    """主源有货但不够 n 条 → 免 key 链路补齐一起进 RRF（补这趟不额外花钱）。"""
    with _FakeKey(), _Patch(_http_get=_fake_get()):
        tool = WebSearchTool(firecrawl="primary", max_results=5)
        tool._search_firecrawl = lambda q, n: _items(1, "fc")
        tool._gather = lambda engines, q: ([("bing", _items(5, "b"))], [])
        out = tool.run({"query": "q"})
    assert "firecrawl" in out and "bing" in out, out[:160]


def test_primary_without_key_is_plain_free_chain():
    """没 key＝这档不存在。不带凭据也能搜是底线能力，绝不能因此失败。"""
    with _NoKey(), _Patch(_http_get=_fake_get()):
        tool = WebSearchTool(firecrawl="primary")
        tool._gather = lambda engines, q: ([("bing", _items(5, "b"))], [])
        out = tool.run({"query": "q"})
    assert "[搜索结果·bing]" in out and "已退回" not in out


def test_quota_exhausted_degrades_and_says_so():
    """402＝配额用尽：本次退回免 key 链路，且**说出来**——否则"结果变差"会被归到模型头上。"""
    reset_firecrawl_quota()
    def boom(q, n):
        raise FirecrawlQuotaError("Firecrawl 配额用尽（HTTP 402）")

    try:
        with _FakeKey(), _Patch(_http_get=_fake_get()):
            tool = WebSearchTool(firecrawl="primary")
            tool._search_firecrawl = boom
            tool._gather = lambda engines, q: ([("bing", _items(5, "b"))], [])
            out = tool.run({"query": "q"})
        assert "[已退回]" in out and "配额用尽" in out, out[:200]
        assert firecrawl_quota_exhausted(), "配额用尽必须粘住"
    finally:
        reset_firecrawl_quota()


def test_quota_exhausted_is_sticky_no_second_attempt():
    """粘住之后不再重试：每次都先撞一次 402 才退回，白等一个往返、日志还刷屏。"""
    reset_firecrawl_quota()
    calls = []
    def boom(q, n):
        calls.append(q)
        raise FirecrawlQuotaError("Firecrawl 配额用尽（HTTP 402）")

    try:
        with _FakeKey(), _Patch(_http_get=_fake_get()):
            tool = WebSearchTool(firecrawl="primary")
            tool._search_firecrawl = boom
            tool._gather = lambda engines, q: ([("bing", _items(5, "b"))], [])
            tool.run({"query": "一"})
            tool.run({"query": "二"})
        assert len(calls) == 1, f"配额用尽后又打了 {len(calls)} 次"
    finally:
        reset_firecrawl_quota()


def test_rate_limit_is_not_treated_as_quota():
    """429 是限流（瞬时，退避后能恢复）。把它当配额用尽会**永久关掉**付费链路——
    误报比漏报贵，这条与块V 决策 6 同一条立场。"""
    from agentcore.tools.web import _looks_like_quota
    assert _looks_like_quota(402, "") is True
    assert _looks_like_quota(429, "Rate limit exceeded") is False
    assert _looks_like_quota(500, "") is False
    assert _looks_like_quota(400, "insufficient credits") is True


# ---- 渲染：两处共用一份 -------------------------------------------------------

def test_render_items_matches_the_wire_format():
    """升级闸要先"看一眼"这批结果才能判质量，而块H1 的评估器吃的就是这个格式。
    渲染写两份迟早漂（本项目已因"两处写"吃过亏）。"""
    ranked = [{"title": "T", "url": "https://a.org/x", "snippet": "S", "sources": ["bing", "ddg"]}]
    out = render_items(ranked, {"https://a.org/x": "正文摘录"})
    assert out.startswith("1. T  [bing+ddg]\n   https://a.org/x\n   S")
    assert "   ↳ 正文摘录" in out
    assert render_items([]) == ""


def test_render_items_is_used_by_the_tool():
    import inspect
    # 看**整个类**而不是 run：搜索主体已挪进 _search（run 只剩解析参数 + 查同回合缓存）。
    # 盯的是"工具没有自己另写一份渲染"，不是它写在哪个方法里。
    assert "render_items(" in inspect.getsource(WebSearchTool)



# ================= 段 2：读页兜底（scrape） =====================================
# 真网实测（2026-08-20）：scrape = 1 credit（search 是 2）；它的适用面是 **JS 空壳**
# （app.slack.com/help 直读 141 字符 → 渲染 1874 字符），**打不穿强反爬/登录墙**
# （知乎 403 页拿回来的是"你似乎来到了没有知识存在的荒原"拦截插页，加 stealth 也一样）。
# 下面每条判据都对着这两个事实写。

def test_parse_page_prefers_markdown_and_never_falls_back_to_html():
    assert parse_firecrawl_page({"data": {"markdown": "正文", "html": "<div>x</div>"}}) == "正文"
    assert parse_firecrawl_page({"data": {"content": "正文2"}}) == "正文2"
    # 只有 html：**宁可空手**——原始 HTML 当正文交出去会把导航侧栏一起喂给模型
    assert parse_firecrawl_page({"data": {"html": "<div>x</div>"}}) == ""
    for junk in ({}, {"data": None}, {"data": []}, "字符串", None):
        assert parse_firecrawl_page(junk) == ""


def test_gain_rejects_the_zhihu_interstitial():
    """付费源产出必须再判一次，否则花了 credit 还把拦截页当正文喂给模型。"""
    why = firecrawl_gain("", _ZHIHU_INTERSTITIAL)
    assert why and "跳转" in why, why


def test_gain_rejects_empty_and_still_blocked():
    assert firecrawl_gain("", "") is not None
    assert firecrawl_gain("", "   ") is not None
    assert "仍是" in (firecrawl_gain("", "请登录后查看" + "x" * 300) or "")
    assert firecrawl_gain("", "太短") is not None          # 空壳口径：短正文也不收


def test_gain_requires_real_increment_over_the_baseline():
    """换源的全部意义是买增量；读回来跟原来差不多＝这 1 credit 什么也没买到。"""
    base = "正" * 400
    assert firecrawl_gain(base, "正" * 450) is not None     # 才多 12%
    assert firecrawl_gain(base, "正" * 800) is None         # 翻倍，收
    assert firecrawl_gain(_SHELL_TEXT, _RENDERED) is None   # 实测那一档：141 → 1874


def test_web_fetch_uses_firecrawl_before_browser():
    """空壳页：托管源先上（更轻、且不带登录态），**浏览器一次都不该被叫到**。"""
    called = []
    with _FakeKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT),
                            firecrawl_scrape=lambda u, t=30: _RENDERED):
        tool = WebFetchTool(firecrawl="fallback",
                            browser_reader=lambda u: called.append(u) or "浏览器读的")
        out = tool.run({"url": "https://app.example.com/help"})
    assert "已自动改用 Firecrawl 读取" in out
    assert "Slack 帮助中心" in out
    assert not called, "浏览器不该被叫到"


def test_web_fetch_falls_through_to_browser_when_firecrawl_also_blocked():
    """Firecrawl 拿回拦截页 → 继续降级到浏览器（它有登录态，是最后一档）。"""
    with _FakeKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT),
                            firecrawl_scrape=lambda u, t=30: _ZHIHU_INTERSTITIAL):
        tool = WebFetchTool(firecrawl="fallback", browser_reader=lambda u: "浏览器读到的真正文" * 20)
        out = tool.run({"url": "https://www.zhihu.com/question/1"})
    assert "已自动改用浏览器读取" in out
    assert "浏览器读到的真正文" in out


def test_web_fetch_skips_firecrawl_on_login_walls():
    """登录墙在 Firecrawl 那里是稳定的必然失败——试一次＝白烧 1 credit，直接交给浏览器。"""
    calls = []
    with _FakeKey(), _Patch(_http_get=_fake_get("请登录后查看全文"),
                            firecrawl_scrape=lambda u, t=30: calls.append(u) or "不该被调用"):
        tool = WebFetchTool(firecrawl="always", browser_reader=lambda u: "登录态读到的正文" * 20)
        out = tool.run({"url": "https://x.example.com/p"})
    assert not calls, "登录墙不该动用 Firecrawl"
    assert "已自动改用浏览器读取" in out


def test_web_fetch_rescues_hard_http_failures():
    """403/429 此前直接抛，浏览器兜底根本够不着——web_search 那句"用 web_fetch 读它"是空头支票。"""
    with _FakeKey(), _Patch(_http_get=_fake_get(raises="请求失败（url）：HTTPError: HTTP Error 403"),
                            firecrawl_scrape=lambda u, t=30: _RENDERED):
        out = WebFetchTool(firecrawl="fallback").run({"url": "https://www.zhihu.com/question/1"})
    assert "HTTP 直读失败" in out and "Firecrawl" in out
    assert "Slack 帮助中心" in out


def test_web_fetch_error_carries_every_layer_reason():
    """三条路都没走通时，**每一层的原因都要带上**，否则排查只看得到最后一层。"""
    def boom(url, t=30):
        raise ToolError("Firecrawl 抓取失败：HTTPError: 402 配额用尽")

    with _FakeKey(), _Patch(_http_get=_fake_get(raises="请求失败（url）：HTTPError: HTTP Error 403"),
                            firecrawl_scrape=boom):
        try:
            WebFetchTool(firecrawl="fallback").run({"url": "https://a.example.org/p"})
        except ToolError as e:
            msg = str(e)
        else:
            raise AssertionError("三路皆失败却没报错")
    assert "403" in msg and "配额用尽" in msg and "浏览器" in msg, msg


def test_web_fetch_without_key_behaves_exactly_as_before():
    """没 key＝这条能力不存在：一路照旧走浏览器兜底，绝不因此失败。"""
    with _NoKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT)):
        out = WebFetchTool(firecrawl="always",
                           browser_reader=lambda u: "浏览器读的正文" * 30).run({"url": "https://a.org/p"})
    assert "已自动改用浏览器读取" in out


def test_web_fetch_off_never_calls_firecrawl():
    calls = []
    with _FakeKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT),
                            firecrawl_scrape=lambda u, t=30: calls.append(u) or _RENDERED):
        WebFetchTool(firecrawl="off", browser_reader=lambda u: "浏览器" * 60).run(
            {"url": "https://a.org/p"})
    assert not calls


def test_search_read_marks_the_switched_source():
    """动用了付费源就要说出来（同段 1 head 里的 [已换源]）。"""
    import threading
    with _FakeKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT),
                            firecrawl_scrape=lambda u, t=30: _RENDERED):
        tool = WebSearchTool(firecrawl="fallback", read_top_n=3)
        out = tool._read_one("https://a.org/p", "频道", threading.Semaphore(FIRECRAWL_READ_BUDGET))
    assert out.startswith("[已换源·Firecrawl] "), out[:60]


def test_search_read_budget_is_capped_per_call():
    """read_top_n=3 全受阻也只能烧 FIRECRAWL_READ_BUDGET 次——最坏情况必须可预期。"""
    calls = []
    with _FakeKey(), _Patch(_http_get=_fake_get(raises="请求失败（url）：HTTPError: HTTP Error 403"),
                            firecrawl_scrape=lambda u, t=30: calls.append(u) or _RENDERED):
        tool = WebSearchTool(firecrawl="fallback", read_top_n=3)
        bodies = tool._read_bodies([{"url": f"https://a{i}.org/p"} for i in range(3)], "q")
    assert len(calls) == FIRECRAWL_READ_BUDGET, calls
    assert sum(1 for v in bodies.values() if v.startswith("[已换源")) == FIRECRAWL_READ_BUDGET


def test_search_read_never_fails_when_firecrawl_throws():
    """兜底失败只让这一条少读一页，绝不能把搜索/读正文带崩。"""
    def boom(u, t=30):
        raise ToolError("Firecrawl 抓取失败：TimeoutError")

    import threading
    with _FakeKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT), firecrawl_scrape=boom):
        tool = WebSearchTool(firecrawl="fallback")
        out = tool._read_one("https://a.org/p", "q", threading.Semaphore(2))
    assert out.startswith("[未读到正文：")   # 退回原来的"指路"文案，不抛


def test_search_read_off_and_nokey_never_call_firecrawl():
    import threading
    calls = []
    fake = lambda u, t=30: calls.append(u) or _RENDERED     # noqa: E731
    with _FakeKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT), firecrawl_scrape=fake):
        WebSearchTool(firecrawl="off")._read_one("https://a.org/p", "q", threading.Semaphore(2))
    with _NoKey(), _Patch(_http_get=_fake_get(_SHELL_TEXT), firecrawl_scrape=fake):
        WebSearchTool(firecrawl="always")._read_one("https://a.org/p", "q", threading.Semaphore(2))
    assert not calls


def test_read_fallback_shares_the_one_switch():
    """共用 `web.firecrawl` 一个开关：registry 两处都透传，别再长出第二个旋钮。"""
    import inspect

    from agentcore.tools import registry
    src = inspect.getsource(registry.build_registry)
    assert src.count('firecrawl=getattr(web, "firecrawl", "off")') == 2, "两个工具都要透传同一个开关"


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
