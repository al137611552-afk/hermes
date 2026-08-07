"""FR-11.1 联网检索：解析器/真链还原/正文提取/工具注册（离线，不碰网络）。

运行：python tests/test_web.py
"""
from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import WebConfig  # noqa: E402
from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import Tool, ToolError  # noqa: E402
from agentcore.tools.web import (  # noqa: E402
    WebFetchTool, WebSearchTool, _clip, bing_real_url, canonical_url, excerpt_for_query,
    extract_main_text, extract_text, fuse_results, looks_blocked, parse_bing, parse_bing_rss,
    parse_ddg_lite, rerank_results, score_node,
)


def test_looks_blocked_detects_anticrawl():
    assert looks_blocked("Just a moment... checking your browser", True)        # Cloudflare
    assert looks_blocked("请登录后查看完整内容", True)                          # 登录墙
    assert looks_blocked("您的访问存在异常，请完成安全验证", True)              # 人机验证
    assert looks_blocked("<div id=app></div>", True)                            # JS 空壳（短）
    assert looks_blocked("normal long article text " * 30, True) is None        # 正常正文放行
    assert looks_blocked("{json:1}" * 30, False) is None                        # 非 HTML 不按空判

# ---- 金标准 HTML 片段（按实测页面结构裁剪） -----------------------------------

_B64 = base64.urlsafe_b64encode("https://docs.python.org/3/".encode()).decode().rstrip("=")
BING_HTML = f'''<ol id="b_results">
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=xx&amp;u=a1{_B64}&amp;ntb=1">
Python <b>Docs</b></a></h2><div><p>Official <b>documentation</b> for Python.</p></div></li>
<li class="b_algo"><h2><a href="https://example.com/direct">Direct Link</a></h2>
<p>No redirect here.</p></li>
</ol>'''

DDG_HTML = '''<table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=x">Python Docs</a></td></tr>
<tr><td class='result-snippet'>Official <b>docs</b>.</td></tr>
<tr><td><a rel="nofollow" href="https://example.com/plain">Plain</a></td></tr>
<tr><td class='result-snippet'>second snippet</td></tr>
</table>'''

PAGE_HTML = '''<html><head><title> 测试页 </title><style>.x{color:red}</style>
<script>var hidden = "不该出现";</script></head>
<body><h1>标题一</h1><p>第一段 内容。</p><div>第二段</div>
<noscript>也不该出现</noscript></body></html>'''


def test_parse_bing_and_real_url():
    rs = parse_bing(BING_HTML)
    assert len(rs) == 2
    assert rs[0]["url"] == "https://docs.python.org/3/"      # a1+base64 真链还原
    assert rs[0]["title"] == "Python Docs" and "documentation" in rs[0]["snippet"]
    assert rs[1]["url"] == "https://example.com/direct"      # 非跳转链原样保留
    assert bing_real_url("https://normal.example/x") == "https://normal.example/x"
    assert bing_real_url("https://www.bing.com/ck/a?u=a1!!!bad") .startswith("https://www.bing.com")


def test_parse_ddg_lite():
    rs = parse_ddg_lite(DDG_HTML)
    assert rs[0]["url"] == "https://docs.python.org/3/"      # uddg= 真链还原
    assert rs[0]["title"] == "Python Docs" and rs[0]["snippet"] == "Official docs."
    assert rs[1]["url"] == "https://example.com/plain" and rs[1]["snippet"] == "second snippet"


def test_extract_text_strips_script_keeps_title():
    title, text = extract_text(PAGE_HTML)
    assert title == "测试页"
    assert "标题一" in text and "第一段 内容。" in text and "第二段" in text
    assert "不该出现" not in text and "color:red" not in text


def test_rerank_coverage_lifts_multi_term_match():
    # "苹果 水果"：同时含两词的营养页应排到只含"苹果"的 Apple 公司页前面（治排序跑偏）
    cands = [
        {"title": "Apple 苹果官网 iPhone", "url": "https://apple.com/cn", "snippet": "Apple 公司产品"},
        {"title": "苹果新品发布", "url": "https://apple.com.cn/news", "snippet": "苹果 iPhone 发布会"},
        {"title": "苹果的营养价值", "url": "https://jiankang.com/apple", "snippet": "苹果这种水果富含维生素"},
    ]
    out = rerank_results("苹果 水果", cands, top_n=3)
    assert out[0]["url"] == "https://jiankang.com/apple"   # 覆盖"苹果"+"水果" → 居首


def test_rerank_cjk_phrase_bigram_beats_dictionary_junk():
    # 真实 bug：整句短语「怎么挑选甜苹果」整体匹配不到任何页 → 全 0 分 → 退化成引擎原序（吐百科）。
    # 切 2-gram 后"苹果/挑选/颜色/手感"可匹配；"怎么"是停用词不给百科页加分。
    cands = [
        {"title": "怎么（汉语词语）_百度百科", "url": "https://baike.baidu.com/item/怎么",
         "snippet": "“怎么”一词最早见于南唐文献，疑问代词……"},
        {"title": "如何（汉语词语）_百度百科", "url": "https://baike.baidu.com/item/如何",
         "snippet": "作为疑问代词具有双重含义……"},
        {"title": "怎么挑选甜苹果？看果脐条纹颜色手感", "url": "https://guonong.com/apple",
         "snippet": "挑甜苹果看果脐深、条纹明显、颜色红、手感沉……"},
    ]
    out = rerank_results("怎么挑选甜苹果 看果脐 条纹 颜色 手感", cands, top_n=3)
    assert out[0]["url"] == "https://guonong.com/apple"      # 内容页居首，不再是百科
    assert "baike.baidu.com" not in out[0]["url"]


def test_rerank_per_domain_cap_and_dedup():
    cands = [
        {"title": "A1", "url": "https://x.com/1", "snippet": "苹果 水果 甜"},
        {"title": "A2", "url": "https://x.com/2", "snippet": "苹果 水果 脆"},
        {"title": "A3", "url": "https://x.com/3", "snippet": "苹果 水果 香"},
        {"title": "B1", "url": "https://y.com/1", "snippet": "苹果 水果"},
        {"title": "dup", "url": "https://x.com/1", "snippet": "苹果 水果 甜"},  # 完全重复 URL
    ]
    out = rerank_results("苹果 水果", cands, top_n=3, per_domain_cap=2)
    urls = [r["url"] for r in out]
    assert urls.count("https://x.com/1") == 1                       # 去重
    assert sum(1 for u in urls if u.startswith("https://x.com")) == 2  # 单域封顶 2（名额够时严格）
    assert "https://y.com/1" in urls                                # 多样性纳入别的域


def test_rerank_keeps_results_when_no_term_match():
    # 无词命中也不能把结果清空（保证 auto_chain 等存量行为不被重排吃掉）
    out = rerank_results("zzz", [{"title": "T", "url": "https://u", "snippet": "S"}], top_n=3)
    assert len(out) == 1 and out[0]["url"] == "https://u"


def test_rerank_overflow_fills_when_diversity_short():
    # 全同域、top_n>cap：配额只放 cap 条会不足 top_n → 用溢出高分项补足
    cands = [{"title": f"T{i}", "url": f"https://x.com/{i}", "snippet": "苹果"} for i in range(5)]
    out = rerank_results("苹果", cands, top_n=4, per_domain_cap=2)
    assert len(out) == 4   # 不因单域封顶而少给


def test_search_tool_validation_and_auto_chain():
    t = WebSearchTool(engine="auto", timeout=5, max_results=3)
    try:
        t.run({"query": "  "})
        assert False, "空 query 应报错"
    except ToolError as e:
        assert "query" in str(e)
    # auto 链路：两个引擎都失败时聚合可读错误（打桩 _search_one，不碰网络）
    t._search_one = lambda eng, q: (_ for _ in ()).throw(ToolError(f"{eng} down"))
    try:
        t.run({"query": "x"})
        assert False
    except ToolError as e:
        assert "bing" in str(e) and "duckduckgo" in str(e)
    # 第一个引擎空结果、第二个有结果 -> 用第二个
    def fake(eng, q):
        return [] if eng == "bing" else [{"title": "T", "url": "https://u", "snippet": "S"}]
    t._search_one = fake
    out = t.run({"query": "x"})
    assert "duckduckgo" in out and "https://u" in out


# ---- FR-11.1b：结构化搜索源 / 跨引擎融合 / 主正文抽取 / 片段摘录 / 浏览器自动升级 ----

BING_RSS = '''<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel>
<title>必应：python docs</title><link>http://www.bing.com/search?q=python+docs</link>
<item><title><![CDATA[Python Docs]]></title><link>https://docs.python.org/3/</link>
<description><![CDATA[Official <b>documentation</b>.]]></description></item>
<item><title>Second</title><link>https://example.com/second</link>
<description>plain description</description></item>
<item><title>坏条目没有链接</title><description>x</description></item>
</channel></rss>'''


def test_parse_bing_rss():
    rs = parse_bing_rss(BING_RSS)
    assert len(rs) == 2                                   # 无链接的坏条目被跳过，不炸
    assert rs[0]["url"] == "https://docs.python.org/3/"
    assert rs[0]["title"] == "Python Docs"                # CDATA 剥掉
    assert rs[0]["snippet"] == "Official documentation."  # CDATA + 标签都剥掉
    assert rs[1]["title"] == "Second" and rs[1]["snippet"] == "plain description"
    assert parse_bing_rss("<html>不是 RSS</html>") == []   # 拿到 HTML 时返回空 → 上层降级


def test_canonical_url_dedup_key():
    a = canonical_url("https://WWW.Example.com/a/b/?utm_source=x&id=1#frag")
    b = canonical_url("http://example.com/a/b?id=1")
    assert a.split("://", 1)[1] == b.split("://", 1)[1]   # host/path/参数归一后同键
    assert canonical_url("https://x.com") == canonical_url("https://x.com/")


def test_fuse_results_rrf_lifts_cross_engine_agreement():
    # 两个引擎都给的第 3 名，应该压过只有一个引擎给的第 1 名（交叉验证优先）
    bing = [{"title": "只有 bing 有", "url": "https://a.com/1", "snippet": ""},
            {"title": "x", "url": "https://b.com/2", "snippet": ""},
            {"title": "两家都有", "url": "https://c.com/3", "snippet": "短"}]
    ddg = [{"title": "只有 ddg 有", "url": "https://d.com/1", "snippet": ""},
           {"title": "y", "url": "https://e.com/2", "snippet": ""},
           {"title": "两家都有", "url": "https://c.com/3/?utm_source=ddg", "snippet": "更长的摘要"}]
    fused = fuse_results([("bing", bing), ("duckduckgo", ddg)])
    assert fused[0]["url"].startswith("https://c.com/3")        # 交叉命中上浮到第一
    assert fused[0]["sources"] == ["bing", "duckduckgo"]        # 记录来源
    assert fused[0]["snippet"] == "更长的摘要"                   # 摘要取信息量大的那份
    assert len(fused) == 5                                      # 跟踪参数不同也判为同一条
    assert fuse_results([]) == []


MAIN_HTML = '''<html><head><title>文章页</title></head><body>
<header><a href="/">站点首页</a><a href="/about">关于</a></header>
<nav class="sidebar"><a href="/1">导航一</a><a href="/2">导航二</a><a href="/3">导航三</a></nav>
<div id="main-content"><article class="post-body">
<h1>正文标题</h1><p>''' + "这是正文的第一段，讲的是苹果的营养价值。" * 6 + '''</p>
<p>''' + "第二段继续讲苹果怎么挑选和保存。" * 6 + '''</p></article></div>
<aside class="related"><a href="/r1">相关推荐一</a><a href="/r2">相关推荐二</a></aside>
<footer>版权所有 © 示例站 · <a href="/tos">服务条款</a></footer></body></html>'''


def test_extract_main_text_drops_boilerplate():
    title, main = extract_main_text(MAIN_HTML)
    assert title == "文章页"
    assert "正文标题" in main and "营养价值" in main and "怎么挑选" in main
    for junk in ("导航一", "相关推荐一", "服务条款", "关于"):
        assert junk not in main, junk
    _, full = extract_text(MAIN_HTML)
    assert len(main) < len(full)                           # 确实瘦身了


def test_extract_main_text_falls_back_when_no_good_candidate():
    # 短页/结构扁平：选不出可信主正文时**宁可全给**，不能返回空
    tiny = "<html><body><p>就一句话。</p></body></html>"
    _, out = extract_main_text(tiny)
    assert "就一句话。" in out
    # 残缺 HTML（标签不闭合）也不能抛
    _, out2 = extract_main_text("<html><body><div class=content><p>没闭合" + "内容 " * 100)
    assert "没闭合" in out2


def test_score_node_penalizes_boilerplate_and_link_density():
    body = score_node("div", "post-content", 1000, 50)
    nav = score_node("div", "sidebar-nav", 1000, 50)
    linky = score_node("div", "post-content", 1000, 900)
    assert nav < body                       # 边角料类名重罚
    assert linky < body                     # 链接占比高（＝导航）分低
    assert score_node("article", "", 1000, 0) > score_node("div", "", 1000, 0)


def test_excerpt_for_query_picks_relevant_and_keeps_order():
    paras = [f"无关段落{i}，讲的是别的东西。" for i in range(10)]
    paras[2] = "这一段讲 user-agent 参数怎么设置。"
    paras[7] = "这一段讲 user-agent 的默认值。"
    text = "\n".join(paras)
    out = excerpt_for_query(text, "user-agent 设置", 300)
    assert "怎么设置" in out and "默认值" in out
    assert out.index("怎么设置") < out.index("默认值")     # 保持原文顺序
    assert "…" in out                                      # 不连续处有省略标记
    assert len(out) <= 300 + 40
    # 一个词都不命中 → 退回取开头（不能返回空）
    assert excerpt_for_query(text, "完全无关的量子色动力学", 100).startswith("无关段落0")


def test_clip_uses_focus_excerpt_only_when_over_budget():
    short = "短正文"
    assert _clip(short, 100, "随便") == short              # 没超预算就原样，不摘录
    long_text = "\n".join(["苹果的营养价值很高。"] + ["别的内容。" for _ in range(200)])
    out = _clip(long_text, 200, "营养价值")
    assert "营养价值" in out and "已按 focus" in out
    out2 = _clip(long_text, 200, "")
    assert "已截断至" in out2                              # 没给 focus 就照旧截断


class _FakeHTTP:
    """打桩 _http_get：不碰网络，按 URL 返回预设 (final_url, body, ctype)。"""

    def __init__(self, body, ctype="text/html"):
        self.body, self.ctype, self.calls = body, ctype, []

    def __call__(self, url, timeout):
        self.calls.append(url)
        return url, self.body, self.ctype


def test_fetch_auto_upgrades_to_browser_when_blocked(monkey=None):
    from agentcore.tools import web as webmod
    orig = webmod._http_get
    seen = []
    try:
        webmod._http_get = _FakeHTTP("<html><body>请完成安全验证</body></html>")
        # ① 接了浏览器 → 自动改用浏览器读同一 URL，且**明确标注读取方式与登录态**
        t = WebFetchTool(browser_reader=lambda u: (seen.append(u) or "浏览器读到的真正正文" * 5))
        out = t.run({"url": "https://blocked.example/x"})
        assert seen == ["https://blocked.example/x"]
        assert "已自动改用浏览器读取" in out and "登录态" in out
        assert "浏览器读到的真正正文" in out
        # ② 没接浏览器 → 如实报受阻，不假装成功
        out2 = WebFetchTool().run({"url": "https://blocked.example/x"})
        assert "抓取受阻" in out2 and "未接浏览器穿透" in out2
        # ③ 浏览器侧自己炸了 → 不能把 web_fetch 带崩，降级成受阻提示
        t3 = WebFetchTool(browser_reader=lambda u: (_ for _ in ()).throw(RuntimeError("boom")))
        out3 = t3.run({"url": "https://blocked.example/x"})
        assert "抓取受阻" in out3 and "boom" in out3
    finally:
        webmod._http_get = orig


def test_fetch_normal_page_does_not_touch_browser():
    from agentcore.tools import web as webmod
    orig = webmod._http_get
    called = []
    try:
        webmod._http_get = _FakeHTTP(MAIN_HTML)
        t = WebFetchTool(browser_reader=lambda u: called.append(u))
        out = t.run({"url": "https://ok.example/x"})
        assert not called                       # 正常页不该惊动浏览器（贵且慢）
        assert "营养价值" in out and "导航一" not in out
    finally:
        webmod._http_get = orig


def test_search_concurrent_fuses_two_engines():
    t = WebSearchTool(engine="auto", timeout=5, max_results=5)
    t._search_one = lambda eng, q: (
        [{"title": "共同", "url": "https://same.com/a", "snippet": "苹果 水果"},
         {"title": f"{eng} 独有", "url": f"https://{eng}.com/x", "snippet": "苹果"}])
    out = t.run({"query": "苹果 水果"})
    assert "bing+duckduckgo" in out                 # 两个引擎都用上了
    assert out.count("https://same.com/a") == 1     # 跨引擎去重
    assert "[bing+duckduckgo]" in out               # 交叉命中标注来源
    # 一个引擎挂了：另一个照常出结果，且如实说明哪家没用上
    def half(eng, q):
        if eng == "bing":
            raise ToolError("bing down")
        return [{"title": "T", "url": "https://u.com/1", "snippet": "S"}]
    t._search_one = half
    out2 = t.run({"query": "x"})
    assert "https://u.com/1" in out2 and "bing down" in out2 and "部分来源未用上" in out2


def test_registry_and_flags(tmp: Path):
    reg = build_registry(tmp, web=WebConfig())
    assert "web_search" in reg.names() and "web_fetch" in reg.names()
    assert not reg.is_dangerous("web_search") and not reg.is_dangerous("web_fetch")
    # enabled:false / 不传 -> 不注册（行为同 3.0.0）
    assert "web_search" not in build_registry(tmp, web=WebConfig(enabled=False)).names()
    assert "web_search" not in build_registry(tmp).names()
    # 只读角色白名单
    from agentcore.tools.delegate import ROLES
    assert ROLES["researcher"].allows("web_search") and ROLES["reviewer"].allows("web_fetch")


def test_fetch_url_validation(tmp: Path):
    reg = build_registry(tmp, web=WebConfig())
    for bad in ("", "ftp://x", "file:///etc/passwd"):
        try:
            reg.get("web_fetch").run({"url": bad})
            assert False, bad
        except ToolError:
            pass


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
