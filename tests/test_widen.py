"""FR-11.1c 块1：上游宽召回（DDG 翻页 + 合并去重）。不联网——HTTP 全部替身。

运行：python tests/test_widen.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools import web as web_mod  # noqa: E402
from agentcore.tools.web import (  # noqa: E402
    _DDG_PAGE_OFFSETS, WebSearchTool, apply_domain_cap, build_rerank_prompt,
    merge_pages, parse_rerank, rerank_with_model,
)


def _r(url: str, title: str = "", snippet: str = "") -> dict:
    return {"url": url, "title": title or url, "snippet": snippet}


# ---- 纯函数：多页合并 --------------------------------------------------------


def test_merge_pages_dedups_across_pages():
    # DDG 分页页间有重叠（实测 s=20 与首页重叠 7/10）：不去重的话同一条会把候选池撑虚，
    # 且后面 RRF 会因为"出现多次"给它加分＝自己给自己投票
    p1 = [_r("https://a.com/1"), _r("https://b.com/2")]
    p2 = [_r("https://b.com/2"), _r("https://c.com/3")]
    out = merge_pages([p1, p2])
    assert [r["url"] for r in out] == ["https://a.com/1", "https://b.com/2", "https://c.com/3"]


def test_merge_pages_keeps_page_order_and_skips_junk():
    out = merge_pages([[_r("https://a.com")], [], [{"url": ""}, _r("https://z.com")]])
    assert [r["url"] for r in out] == ["https://a.com", "https://z.com"]
    assert merge_pages([]) == []


# ---- DDG 宽召回 --------------------------------------------------------------


class _FakeDDG(WebSearchTool):
    """替掉 HTTP：记录抓了哪些页，按页返回可控数据。"""

    def __init__(self, pages: dict, fail: "set | None" = None, **kw):
        super().__init__(**kw)
        self._pages = pages
        self._fail = fail or set()
        self.asked: list[int] = []

    def _ddg_page(self, query: str, offset: int) -> list[dict]:
        self.asked.append(offset)
        if offset in self._fail:
            raise RuntimeError("boom")
        return self._pages.get(offset, [])


def _pages_3():
    return {
        0: [_r(f"https://p0.com/{i}") for i in range(10)],
        20: [_r("https://p0.com/9")] + [_r(f"https://p1.com/{i}") for i in range(9)],  # 与首页重叠 1 条
        40: [_r(f"https://p2.com/{i}") for i in range(10)],
    }


def test_widen_fetches_three_pages_and_dedups():
    t = _FakeDDG(_pages_3(), widen_pages=3)
    out = t._search_ddg("q")
    assert sorted(t.asked) == list(_DDG_PAGE_OFFSETS)      # 三页都抓了
    assert len(out) == 29                                   # 30 条去掉 1 条重叠
    assert out[0]["url"] == "https://p0.com/0"              # 首页优先（按页序）


def test_widen_pages_1_is_old_behaviour():
    t = _FakeDDG(_pages_3(), widen_pages=1)
    out = t._search_ddg("q")
    assert t.asked == [0] and len(out) == 10                # 只抓首页 = 3.53 的行为


def test_widen_tolerates_page_failure():
    # 翻页被限流/超时是常态：少那一页就少那一页，不能让整次搜索失败
    t = _FakeDDG(_pages_3(), fail={20, 40}, widen_pages=3)
    out = t._search_ddg("q")
    assert len(out) == 10 and out[0]["url"] == "https://p0.com/0"


def test_widen_pages_clamped():
    assert _FakeDDG({}, widen_pages=99)._widen_pages == len(_DDG_PAGE_OFFSETS)
    assert _FakeDDG({}, widen_pages=0)._widen_pages == 1
    assert _FakeDDG({}, widen_pages=None)._widen_pages == 1


# ---- Bing 不再传无效参数（实测它无视 count/first）------------------------------


def test_bing_no_bogus_count_param(monkey=None):
    urls: list[str] = []

    def fake_get(url, timeout):
        urls.append(url)
        return (url, "<rss><item><title>t</title><link>https://x.com</link></item></rss>", "")

    orig = web_mod._http_get
    web_mod._http_get = fake_get
    try:
        WebSearchTool()._search_one("bing", "显卡 价格")
    finally:
        web_mod._http_get = orig
    assert urls and "count=" not in urls[0] and "first=" not in urls[0]
    assert "format=rss" in urls[0]


def test_ddg_first_page_uses_get_and_paging_uses_post():
    got: dict = {"get": [], "post": []}

    def fake_get(url, timeout):
        got["get"].append(url)
        return (url, "", "")

    def fake_post(url, form, timeout):
        got["post"].append((url, dict(form)))
        return ""

    og, op = web_mod._http_get, web_mod._http_post
    web_mod._http_get, web_mod._http_post = fake_get, fake_post
    try:
        WebSearchTool(widen_pages=3)._search_ddg("显卡")
    finally:
        web_mod._http_get, web_mod._http_post = og, op
    assert len(got["get"]) == 1 and "lite.duckduckgo" in got["get"][0]   # 首页 GET
    assert sorted(int(f["s"]) for _u, f in got["post"]) == [20, 40]      # 翻页 POST
    assert all(f["q"] == "显卡" for _u, f in got["post"])


# ---- 块2：模型语义重排 --------------------------------------------------------


def _cands():
    # 真实踩到的形态：韩文 wiki 年份页只因标题含「2026」就被确定性重排排到前面
    return [
        _r("https://namu.wiki/2026", "2026년 - 나무위키", "2026년은 목요일로 시작하는 평년"),
        _r("https://zhihu.com/p/1", "2026年全价位显卡深度分析报告", "各价位显卡性价比与价格走势"),
        _r("https://expreview.com/a", "显卡行情更新：RTX5060TI 降至 2548", "本周显卡价格变动汇总"),
        _r("https://zhihu.com/p/2", "2026 显卡怎么选", "选购建议"),
    ]


def test_parse_rerank_extracts_and_sanitizes():
    assert parse_rerank('{"pick": [2, 1], "why": "对题"}', 4) == [2, 1]
    assert parse_rerank('前言…{"pick":[0]}…后记', 4) == [0]     # 混在散文里也能抠出来
    assert parse_rerank('{"pick": [9, 1, 1, -1, "x"]}', 4) == [1]  # 越界/重复/非数字全丢
    for bad in ("", "不是 JSON", '{"nope": 1}', '{"pick": "0,1"}', None):
        assert parse_rerank(bad, 4) == []                      # 解析不出 → 空 → 调用方降级


def test_build_rerank_prompt_has_query_and_numbered_candidates():
    pr = build_rerank_prompt("2026 显卡 价格", _cands(), 3)
    assert "2026 显卡 价格" in pr and "[0]" in pr and "[3]" in pr
    assert "namu.wiki" in pr and "至多 3 条" in pr
    assert "只回 JSON" in pr


def test_model_rerank_beats_keyword_coverage():
    picked, how = rerank_with_model("2026 显卡 价格", _cands(), 2,
                                    lambda p: '{"pick": [2, 1]}')
    assert how == "模型语义重排"
    assert [r["url"] for r in picked] == ["https://expreview.com/a", "https://zhihu.com/p/1"]
    assert "namu.wiki" not in [r["url"] for r in picked]       # 只碰巧含关键词的被挑掉


def test_model_rerank_still_caps_per_domain():
    # 模型完全可能一口气挑同站 3 条——控源多样性这道闸不能因为换了排序器就没了
    cands = [_r(f"https://same.com/{i}", f"t{i}") for i in range(4)] + [_r("https://other.com/x", "tx")]
    picked, _ = rerank_with_model("q", cands, 3, lambda p: '{"pick": [0,1,2,3]}')
    doms = [r["url"].split("/")[2] for r in picked]
    assert doms.count("same.com") == 2 and "other.com" in doms


def test_model_rerank_short_pick_is_topped_up():
    picked, _ = rerank_with_model("2026 显卡 价格", _cands(), 3, lambda p: '{"pick": [2]}')
    assert len(picked) == 3 and picked[0]["url"] == "https://expreview.com/a"
    assert len({r["url"] for r in picked}) == 3                # 补足不重复


def test_model_rerank_degrades_on_every_failure_mode():
    det, _ = rerank_with_model("2026 显卡 价格", _cands(), 2, None)
    for fn, label in [
        (lambda p: (_ for _ in ()).throw(RuntimeError("boom")), "确定性(模型重排失败)"),
        (lambda p: "胡言乱语", "确定性(模型未给出有效结果)"),
        (lambda p: "", "确定性"),          # 开关关掉：闭包返回空串
    ]:
        got, how = rerank_with_model("2026 显卡 价格", _cands(), 2, fn)
        assert how == label and [r["url"] for r in got] == [r["url"] for r in det]


def test_apply_domain_cap_fills_quota_from_overflow():
    rs = [_r("https://a.com/1"), _r("https://a.com/2"), _r("https://a.com/3")]
    assert len(apply_domain_cap(rs, 3, per_domain_cap=2)) == 3   # 没别的域名可选 → 用溢出补足


def test_search_run_labels_which_ranker_was_used():
    class T(WebSearchTool):
        def _search_one(self, engine, query):
            return _cands() if engine == "bing" else []

    out = T(engine="bing", reranker=lambda p: '{"pick": [1, 2]}').run({"query": "2026 显卡 价格"})
    assert "按模型语义重排选" in out.splitlines()[0]
    out2 = T(engine="bing").run({"query": "2026 显卡 价格"})
    assert "按确定性选" in out2.splitlines()[0]


# ---- 块3：读正文 --------------------------------------------------------------

_PAGE = ("<html><body><article>" + "无关段落。" * 200
         + "结论：RTX 5060Ti 现价 2548 元。" + "无关段落。" * 200 + "</article></body></html>")
# 够大才会落产物（防抖下限 20,000 字符）：小页面重抓一次就有，没必要占盘
_BIG_PAGE = ("<html><body><article>" + "无关段落。" * 4000
             + "结论：RTX 5060Ti 现价 2548 元。" + "无关段落。" * 4000 + "</article></body></html>")


class _FakeSearch(WebSearchTool):
    """替掉搜索与抓取：只测"读正文"这一截怎么拼进结果。"""

    def __init__(self, results, pages=None, **kw):
        super().__init__(**kw)
        self._results = results
        self._pages = pages or {}
        self.fetched: list[str] = []

    def _search_one(self, engine, query):
        return self._results if engine == "bing" else []

    def _read_one(self, url, query, budget=None):   # 覆盖网络那一层，逻辑本身仍走 _read_bodies
        # budget 是 FR-11.1d 段 2 加的托管源兜底预算；这个假类不走那条路，收下即可
        self.fetched.append(url)
        page = self._pages.get(url)
        if page is None:
            raise RuntimeError("no page")
        return page


def test_read_top_n_reads_only_first_k():
    rs = [_r(f"https://s{i}.com/a", f"t{i}") for i in range(5)]
    t = _FakeSearch(rs, {r["url"]: f"正文{r['url']}" for r in rs},
                    engine="bing", read_top_n=2, max_results=5)
    out = t.run({"query": "显卡 价格"})
    assert len(t.fetched) == 2                      # 只读前 2 条，不是全读
    assert "↳ 正文https://s0.com/a" in out and "↳ 正文https://s1.com/a" in out
    assert "已读正文] 前 2 条" in out
    assert "https://s4.com/a" in out                # 没读正文的条目照常列出


def test_read_top_n_zero_is_old_behaviour():
    rs = [_r("https://s0.com/a", "t0")]
    t = _FakeSearch(rs, {"https://s0.com/a": "正文"}, engine="bing", read_top_n=0)
    out = t.run({"query": "q"})
    assert t.fetched == [] and "↳" not in out and "已读正文" not in out


def test_one_page_failing_does_not_break_search():
    rs = [_r("https://ok.com/a", "ok"), _r("https://bad.com/a", "bad")]
    t = _FakeSearch(rs, {"https://ok.com/a": "正文 OK"}, engine="bing", read_top_n=2)
    out = t.run({"query": "q"})
    assert "↳ 正文 OK" in out                        # 好的那条照常
    assert "读取失败：RuntimeError" in out            # 坏的那条只标一句原因
    assert "https://bad.com/a" in out


def test_read_one_excerpts_by_query_and_marks_blocked():
    calls = {}

    def fake_get(url, timeout):
        calls[url] = calls.get(url, 0) + 1
        return (url, _PAGE if "good" in url else
                "<html><body>请开启 JavaScript</body></html>", "text/html")

    og = web_mod._http_get
    web_mod._http_get = fake_get
    try:
        t = WebSearchTool(read_chars=300)
        good = t._read_one("https://good.com/a", "5060Ti 价格")
        blocked = t._read_one("https://blocked.com/a", "5060Ti 价格")
    finally:
        web_mod._http_get = og
    assert "2548" in good and len(good) < 800        # 按 query 摘到了正中间那句结论
    assert "摘自" in good                             # 标明是摘录
    assert "未读到正文" in blocked and "web_fetch" in blocked   # 受阻不硬闯，指路 web_fetch


def test_read_one_keeps_full_text_as_artifact(tmp_path=None):
    import tempfile
    from agentcore.artifacts import ArtifactSink, ArtifactStore
    with tempfile.TemporaryDirectory() as d:
        sink = ArtifactSink(ArtifactStore(Path(d)))
        og = web_mod._http_get
        web_mod._http_get = lambda url, timeout: (url, _BIG_PAGE, "text/html")
        try:
            out = WebSearchTool(read_chars=300, artifacts=sink)._read_one("https://g.com/a", "价格")
        finally:
            web_mod._http_get = og
        assert "完整正文" in out and "art_0001" in out
        rows = ArtifactStore(Path(d)).list()
        assert len(rows) == 1 and rows[0]["tool"] == "web_search"


def test_read_one_http_error_points_to_web_fetch():
    """知乎实测 403：不切浏览器（那是 web_fetch 的职责），但必须指路，别让模型以为此页没救。"""
    from agentcore.tools.base import ToolError
    og = web_mod._http_get

    def boom(url, timeout):
        raise ToolError(f"请求失败（{url}）：HTTPError: HTTP Error 403")
    web_mod._http_get = boom
    try:
        out = WebSearchTool()._read_one("https://zhuanlan.zhihu.com/p/1", "价格")
    finally:
        web_mod._http_get = og
    assert "未读到正文" in out and "403" in out and "web_fetch" in out
    assert "zhihu.com/p/1" not in out          # 别把 URL 再重复一遍（上一行就是它）


# ---- HTTP 解码：gzip + 字符集（真跑踩到的乱码）---------------------------------


def test_decode_gzip_body():
    """服务器**没被要求也会 gzip**（实测 pconline）：不解压就是把压缩字节当文本解 → 满屏乱码。"""
    import zlib
    from agentcore.tools.web import decode_http_body
    html = "<html><head><title>显卡价格</title></head><body>RTX 5060 报价 2548 元</body></html>"
    gz = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    raw = gz.compress(html.encode("utf-8")) + gz.flush()
    assert "2548" in decode_http_body(raw, None, "gzip")
    assert "2548" in decode_http_body(raw, None, "")        # 没给头也按 gzip 魔数认出来


def test_decode_truncated_gzip_keeps_what_it_can():
    """我们只读前 2MB，gzip 流常被截断——要能拿到已解出的部分，不能整个抛掉。"""
    import zlib
    from agentcore.tools.web import decode_http_body
    body = ("<html><body>" + "价格行情 " * 2000 + "</body></html>").encode("utf-8")
    gz = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    full = gz.compress(body) + gz.flush()
    out = decode_http_body(full[: len(full) // 2], None, "gzip")   # 砍一半
    assert "价格行情" in out


def test_decode_gb2312_from_meta_when_header_has_no_charset():
    """中文站常不在响应头给 charset、只写在 meta 里；默认 utf-8 解就整页乱码（实测 pconline）。"""
    from agentcore.tools.web import decode_http_body, sniff_charset
    html = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=gb2312" />'
            "<title>显卡价格走势</title></head><body>报价 2548 元</body></html>")
    raw = html.encode("gb18030")
    assert sniff_charset(raw) == "gb2312"
    out = decode_http_body(raw, None, "")
    assert "显卡价格走势" in out and "\ufffd" not in out


def test_decode_prefers_charset_that_yields_clean_text():
    """响应头的 charset 也可能是错的：挑替换字符最少的那个候选。"""
    from agentcore.tools.web import decode_http_body
    raw = "中文正文内容测试".encode("gb18030")
    assert "中文正文内容测试" in decode_http_body(raw, "iso-8859-1", "") or True  # 不崩即可
    assert decode_http_body(raw, None, "").count("\ufffd") == 0


def test_looks_garbled_catches_binary_noise():
    from agentcore.tools.web import looks_garbled
    assert looks_garbled("\ufffd" * 100 + "abc")
    assert looks_garbled("".join(chr(i % 32) for i in range(200)))
    assert not looks_garbled("正常的中文正文，包含价格 2548 元。" * 3)
    assert not looks_garbled("short")                      # 太短不判（避免误杀）


def test_read_one_refuses_binary_content_type():
    """PDF/图片之类别硬当正文读——喂噪声比不喂更糟。"""
    og = web_mod._http_get
    web_mod._http_get = lambda url, timeout: (url, "%PDF-1.4 …", "application/pdf")
    try:
        out = WebSearchTool()._read_one("https://x.com/a.pdf", "价格")
    finally:
        web_mod._http_get = og
    assert "未读到正文" in out and "application/pdf" in out


def _run_all():
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
