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
    WebSearchTool, bing_real_url, extract_text, looks_blocked, parse_bing, parse_ddg_lite,
    rerank_results,
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
