"""联网检索工具（FR-11.1 / FR-11.1b）：web_search + web_fetch。零新依赖（urllib + html.parser + 正则）。

- web_search：免 key 搜索。auto 链路 = **Bing RSS + DDG lite 并发**、结果 RRF 融合去重后重排；
  Bing RSS（`&format=rss`）是结构化端点，比啃 `b_algo` HTML 抗改版，解析不出时自动降级到 HTML。
  真实链接从跳转参数还原（Bing `u=a1<base64>` / DDG `uddg=`）。
- web_fetch：抓取网页转正文（**readability 式主正文抽取**：去导航/页脚/侧栏，按文本密度选正文块；
  抽不出来才退回整页文本）；给了 `focus` 就只回与之相关的片段（省上下文）。JSON/纯文本直出。
  下载上限 2MB。允许抓 localhost（配合后台 dev server 自测是特性）。
  命中反爬/登录墙/JS 空壳时，若接了浏览器穿透则**自动改用浏览器读同一 URL**（不让模型自己选路）。
两工具均只读、非危险、不过权限 gate，并进只读子 Agent 角色白名单。

**分工原则（v3.53 修正 v3.43）**：搜索恒走 HTTP——实测搜索引擎对自动化浏览器返回空壳结果页
（Bing 结果块 0 个、DDG 直接给验证码），用浏览器去搜索引擎必败；浏览器只负责读 HTTP 读不动的页面。
"""
from __future__ import annotations

import base64
import concurrent.futures
import html as html_mod
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .base import Tool, ToolError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 hermes-dev"
MAX_DOWNLOAD_BYTES = 2_000_000   # 单页下载上限

# 反爬/需登录/JS 渲染的「假成功」特征（HTTP 200 但内容是拦截页/空壳）。命中则提示改用浏览器穿透。
_BLOCK_MARKERS = re.compile(
    r"enable\s+javascript|请开启\s*javascript|checking your browser|cf-browser-verification|"
    r"just a moment|attention required|cloudflare|captcha|verify you are (?:a )?human|"
    r"are you a robot|unusual traffic|access denied|forbidden|人机验证|验证码|安全验证|"
    r"滑动验证|请登录|登录后(?:查看|可见)|need to (?:sign|log) ?in|please (?:sign|log) ?in",
    re.I)
_BLOCK_MIN_TEXT = 200   # HTML 页提取正文短于此（且像被拦/空壳）多半是 JS 渲染或反爬


def looks_garbled(text: str, sample: int = 2000) -> bool:
    """判断一段"正文"是不是解码噪声（纯函数）：替换字符或不可打印控制字符占比过高。

    最后一道闸——喂给模型的宁可是"没读到"，也不能是满屏 `\ufffd` 和控制字符。
    """
    t = (text or "")[:sample]
    if len(t) < 40:
        return False
    bad = sum(1 for ch in t if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r"))
    return bad / len(t) > 0.05


def looks_blocked(text: str, is_html: bool) -> "str | None":
    """判断 web_fetch 结果是否「假成功」（反爬/需登录/JS 空壳）；是则返回原因短语，否则 None（纯逻辑）。"""
    t = (text or "").strip()
    m = _BLOCK_MARKERS.search(t[:3000])
    if m:
        return f"疑似反爬/需登录/人机验证（命中「{m.group(0)}」）"
    if is_html and len(t) < _BLOCK_MIN_TEXT:
        return "正文几乎为空（疑似 JS 动态渲染，HTTP 抓不到内容）"
    return None
DEFAULT_FETCH_CHARS = 20_000     # web_fetch 默认输出字符上限
MAX_RESULTS_CAP = 10             # **返回给模型**的条数硬上限
# 宽召回（FR-11.1c）：多抓候选再重排过滤，治"直吞前 N 条噪声"。
# **2026-08-10 实测纠正**：原来给 Bing 传 `count=30` 是**无效的**——Bing 无视该参数恒回 10 条
# （RSS/HTML 都是），`first=11/21` 翻页也返回同一批，所以"30 条候选"从未真正生效。
# 真正能加宽的只有 DDG lite 的 **POST 翻页**（`s=0/20/40`，实测每页 10 条、页间有重叠，
# 三页去重约 15 条、五页也只到 16 条——它自己就这么多）。故：Bing 保持单页 10 条，
# DDG 翻 _DDG_PAGES 页，合起来候选池约 23 条。
_DDG_PAGE_OFFSETS = (0, 20, 40)  # DDG lite 翻页偏移（实测 s=10 与首页重叠 9/10，步长取 20 才划算）
_DDG_PAGES = 3                   # 默认翻几页（1 = 关掉宽召回，行为同 3.53）
_READ_TOP_N = 3                  # 搜完顺带读几条正文（FR-11.1c 块3；0 = 关，只回标题+摘要）
_READ_CHARS = 1500               # 每条正文摘录的字符预算（按 query 摘相关段落，不是从头截）
_ENGINES = ("bing", "duckduckgo")
_RRF_K = 60                      # RRF 融合常数（业界惯用 60；越大越看重"多引擎都有"而非单引擎排名）


# ---- HTTP（IO，集中一处） -----------------------------------------------------

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def sniff_charset(raw: bytes) -> "str | None":
    """从 HTML 头部的 <meta charset> / <meta http-equiv content="…charset=…"> 里嗅字符集（纯函数）。

    很多中文站**不在响应头给 charset**（实测 pconline：`Content-Type: text/html` 光秃秃的），
    只写在 meta 里；默认当 utf-8 解就会整页乱码。
    """
    m = _META_CHARSET_RE.search(raw[:4096] or b"")
    return m.group(1).decode("ascii", errors="ignore").lower() if m else None


def _decompress(raw: bytes, content_encoding: str) -> bytes:
    """按 Content-Encoding 解压。**截断的流也要尽量解出已有部分**（我们只读前 2MB）。

    服务器**没被要求也会 gzip**（实测 pconline 就是），不解压就是把压缩字节当文本解 → 满屏乱码。
    """
    enc = (content_encoding or "").lower()
    is_gzip = "gzip" in enc or raw[:2] == b"\x1f\x8b"
    if not is_gzip and "deflate" not in enc:
        return raw
    import zlib
    for wbits in ((16 + zlib.MAX_WBITS,) if is_gzip else (zlib.MAX_WBITS, -zlib.MAX_WBITS)):
        try:
            # decompressobj 而非 gzip.decompress：后者对截断流直接抛错，前者能拿到已解出的部分
            return zlib.decompressobj(wbits).decompress(raw) or raw
        except zlib.error:
            continue
    return raw


def decode_http_body(raw: bytes, header_charset: "str | None", content_encoding: str = "") -> str:
    """HTTP 响应体 → 文本：先解压，再按「响应头 charset → HTML meta → utf-8 → gb18030」定编码。

    纯函数、可单测。挑编码时看**替换字符占比**：解出来一堆 U+FFFD 就换下一个候选，
    别把乱码当正文喂给模型（真跑踩到过：gzip + gb2312 双重误判，摘录整段是二进制噪声）。
    """
    raw = _decompress(raw, content_encoding)
    cands = []
    for c in (header_charset, sniff_charset(raw), "utf-8", "gb18030"):
        c = (c or "").lower().strip()
        if c in ("gb2312", "gbk"):
            c = "gb18030"          # 超集，能解 gb2312/gbk 全部字符
        if c and c not in cands:
            cands.append(c)
    best, best_bad = "", 1.0
    for c in cands:
        try:
            text = raw.decode(c, errors="replace")
        except LookupError:
            continue
        bad = text.count("\ufffd") / max(1, len(text))
        if bad < 0.002:            # 基本没有替换字符 → 就它了
            return text
        if bad < best_bad:
            best, best_bad = text, bad
    return best or raw.decode("utf-8", errors="replace")


def _http_get(url: str, timeout: int) -> tuple[str, str, str]:
    """GET 一个 URL，返回 (最终URL, 文本, content-type)。失败抛 ToolError（可读）。"""
    if not url.startswith(("http://", "https://")):
        raise ToolError(f"只支持 http(s) URL：{url[:100]}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_DOWNLOAD_BYTES)
            text = decode_http_body(data, r.headers.get_content_charset(),
                                    r.headers.get("Content-Encoding") or "")
            return r.geturl(), text, (r.headers.get("Content-Type") or "")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001 — 网络错误统一转可读
        raise ToolError(f"请求失败（{url[:100]}）：{type(e).__name__}: {e}") from None


def _http_post(url: str, form: dict, timeout: int) -> str:
    """POST 一个表单，返回文本。失败抛 ToolError（可读）。DDG lite 的翻页只认 POST。"""
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_DOWNLOAD_BYTES)
            return decode_http_body(data, r.headers.get_content_charset(),
                                    r.headers.get("Content-Encoding") or "")
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"请求失败（{url[:100]}）：{type(e).__name__}: {e}") from None


# ---- 纯函数：HTML 清洗与搜索结果解析 -------------------------------------------

def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html_mod.unescape(s)).strip()


def bing_real_url(u: str) -> str:
    """Bing 结果是 bing.com/ck/a 跳转链，真链在 u=a1<urlsafe-base64> 参数里。"""
    u = html_mod.unescape(u or "")
    if "bing.com/ck/a" not in u:
        return u
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
    enc = (q.get("u") or [""])[0]
    if enc.startswith("a1"):
        body = enc[2:]
        try:
            real = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode(
                "utf-8", errors="replace")
            if real.startswith(("http://", "https://")):
                return real
        except Exception:  # noqa: BLE001 — 解不开就保留跳转链（仍可访问）
            pass
    return u


def parse_bing(page: str) -> list[dict]:
    """解析 Bing 搜索结果页（b_algo 块）→ [{title, url, snippet}]。"""
    out: list[dict] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append({
            "title": _strip_tags(m.group(2)),
            "url": bing_real_url(m.group(1)),
            "snippet": _strip_tags(p.group(1)) if p else "",
        })
    return out


def parse_ddg_lite(page: str) -> list[dict]:
    """解析 DDG lite 结果页 → [{title, url, snippet}]（真链在 uddg= 参数）。"""
    links = re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snips = re.findall(r"class='result-snippet'>(.*?)</td>", page, re.S)
    out: list[dict] = []
    for i, (href, title) in enumerate(links):
        url = html_mod.unescape(href)
        if url.startswith("//duckduckgo.com/l/"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            real = urllib.parse.unquote((q.get("uddg") or [""])[0])
            if real.startswith(("http://", "https://")):
                url = real
        out.append({
            "title": _strip_tags(title),
            "url": url,
            "snippet": _strip_tags(snips[i]) if i < len(snips) else "",
        })
    return out


def parse_bing_rss(page: str) -> list[dict]:
    """解析 Bing 的 RSS 输出（`&format=rss`）→ [{title, url, snippet}]。

    **优先用它而不是啃 HTML**：RSS 是结构化端点，字段稳定、不随页面改版碎掉，实测一次返回 10 条。
    HTML 解析（parse_bing）保留作降级。
    """
    out: list[dict] = []
    for item in re.findall(r"<item>(.*?)</item>", page, re.S):
        m = re.search(r"<link>(.*?)</link>", item, re.S)
        t = re.search(r"<title>(.*?)</title>", item, re.S)
        d = re.search(r"<description>(.*?)</description>", item, re.S)
        url = html_mod.unescape(_strip_cdata(m.group(1)).strip()) if m else ""
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "title": _strip_tags(_strip_cdata(t.group(1))) if t else "",
            "url": bing_real_url(url),
            "snippet": _strip_tags(_strip_cdata(d.group(1))) if d else "",
        })
    return out


def _strip_cdata(s: str) -> str:
    m = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*$", s or "", re.S)
    return m.group(1) if m else (s or "")


# ---- 纯函数：跨引擎融合（RRF）+ 宽召回结果的确定性重排 / 去重 / 控源多样性 ----

_TRACK_PARAMS = re.compile(r"^(utm_|ref_|fbclid|gclid|spm|from|ref$|share)", re.I)


def canonical_url(url: str) -> str:
    """URL 归一（纯函数）：小写 host、去 fragment、去跟踪参数、去末尾斜杠。仅用于**判重**，不改回传链接。"""
    try:
        sp = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = (sp.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
         if not _TRACK_PARAMS.match(k)]
    path = sp.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((sp.scheme.lower(), host, path,
                                    urllib.parse.urlencode(sorted(q)), ""))


def fuse_results(per_engine: "list[tuple[str, list[dict]]]") -> list[dict]:
    """多引擎结果 **RRF 融合**（Reciprocal Rank Fusion，纯函数）。

    每条按各引擎里的名次给 `1/(K+rank)` 分并累加——**同时出现在多个引擎的结果自然上浮**，
    比"第一个引擎有结果就返回"稳得多（单引擎抽风/被投毒时不至于全盘跑偏）。
    判重用 `canonical_url`（去跟踪参数/末尾斜杠），保留首见的原始 URL 与最长的摘要；
    每条记 `sources`（哪些引擎给的），供输出标注。分数相同按首见顺序稳定。
    """
    agg: dict[str, dict] = {}
    order: list[str] = []
    for engine, results in per_engine or []:
        for rank, r in enumerate(results or [], 1):
            url = (r.get("url") or "").strip()
            if not url:
                continue
            key = canonical_url(url)
            cur = agg.get(key)
            if cur is None:
                cur = {"title": r.get("title") or "", "url": url,
                       "snippet": r.get("snippet") or "", "sources": [], "_score": 0.0,
                       "_seq": len(order)}
                agg[key] = cur
                order.append(key)
            cur["_score"] += 1.0 / (_RRF_K + rank)
            if engine not in cur["sources"]:
                cur["sources"].append(engine)
            if len(r.get("snippet") or "") > len(cur["snippet"]):
                cur["snippet"] = r["snippet"]        # 摘要取最长的那份（信息量更大）
            if not cur["title"]:
                cur["title"] = r.get("title") or ""
    fused = [agg[k] for k in order]
    fused.sort(key=lambda d: (-d["_score"], d["_seq"]))
    return fused


def _domain_of(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


# 中文疑问/泛化词：substring 匹配会让"怎么/如何"类百科页虚高，重排时丢弃（2-gram 粒度）
_CJK_STOP = frozenset({
    "怎么", "怎样", "如何", "什么", "么样", "哪些", "哪个", "为什", "这个", "那个",
    "可以", "知道", "告诉", "一下", "一个", "有没", "没有", "是否", "应该", "需要",
})
_CJK_RE = re.compile(r"[一-鿿]+")


def _query_terms(query: str) -> "set[str]":
    """查询 → 可匹配词集（确定性，无分词依赖）。

    关键：中文用户常把**整句**用空格分成**短语**（如「怎么挑选甜苹果 颜色 手感」），
    整短语 substring 匹配不到任何页 → 全 0 分 → 重排退化成引擎原序（吐百科垃圾）。
    故对每个 CJK 连续段切 **2-gram**（「甜苹果」→甜苹/苹果），让"苹果/颜色/手感"这些
    内容词真正可匹配；丢弃疑问/泛化停用词避免「怎么」百科页虚高。ASCII/数字词整体保留。
    """
    terms: set[str] = set()
    for tok in re.split(r"[\s,，、;；/|]+", (query or "").lower()):
        if not tok:
            continue
        if re.search(r"[a-z0-9]", tok) and not _CJK_RE.search(tok):
            if len(tok) >= 2:
                terms.add(tok)            # 英文/数字词整体（python、rtx5090）
            continue
        for run in _CJK_RE.findall(tok):  # 每个 CJK 段切 2-gram
            for i in range(len(run) - 1):
                bg = run[i:i + 2]
                if bg not in _CJK_STOP:
                    terms.add(bg)
            if len(run) == 1:             # 单字 CJK 段也保留（罕见）
                terms.add(run)
    return terms


def merge_pages(pages: "list[list[dict]]") -> list[dict]:
    """把同一引擎多页结果按页序合并、按 URL 去重（纯函数）。

    DDG lite 的分页页间有重叠（实测 s=20 与首页重叠 7/10），不去重的话候选池会被同一条撑虚，
    后面 RRF 融合还会因为"出现多次"给它加分——等于自己给自己投票。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for page in pages:
        for r in page or []:
            u = (r.get("url") or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(r)
    return out


def rerank_results(query: str, results: list[dict], top_n: int, per_domain_cap: int = 2) -> list[dict]:
    """对宽召回结果做**确定性**重排+去重+控源多样性，返回 top_n（option B 核心）。

    打分 = 查询词**覆盖度**（标题命中权重更高）——多词全覆盖的结果（如「苹果 水果」同时含两词的
    营养页）排到只覆盖单词的（只含「苹果」的 Apple 公司页）前面，治搜索引擎排序跑偏。
    每域名最多 `per_domain_cap` 条（避免单站霸屏、提升来源多样性，直接利好 Novelty）；去重完全相同 URL；
    分相同按原序稳定。配额没填满 top_n 时用被压的高分项补足。**纯函数**：只排不抓，便于单测/Golden。
    """
    terms = _query_terms(query)
    seen_url: set[str] = set()
    scored: list[tuple] = []
    for i, r in enumerate(results or []):
        url = (r.get("url") or "").strip()
        if not url or url in seen_url:
            continue
        seen_url.add(url)
        title = (r.get("title") or "").lower()
        snip = (r.get("snippet") or "").lower()
        if terms:
            in_title = sum(1 for t in terms if t in title)
            in_any = sum(1 for t in terms if t in title or t in snip)
            score = in_any * 10 + in_title * 3 + (1 if snip else 0)
        else:
            score = 0
        scored.append((-score, i, r))     # -score：分降序；i：原序稳定兜底
    scored.sort(key=lambda x: (x[0], x[1]))
    return apply_domain_cap([r for _s, _i, r in scored], top_n, per_domain_cap)


def apply_domain_cap(results: list[dict], top_n: int, per_domain_cap: int = 2) -> list[dict]:
    """按既定顺序取 top_n，每域名最多 per_domain_cap 条；配额没填满再用被压的补足（纯函数）。

    抽成共用助手：确定性重排与模型重排（FR-11.1c 块2）都要这一层——模型完全可能一口气挑 5 条
    同站的，控源多样性这道闸不能因为"换了个更聪明的排序器"就没了。
    """
    out: list[dict] = []
    overflow: list[dict] = []
    per_domain: dict[str, int] = {}
    for r in results or []:
        d = _domain_of(r.get("url", ""))
        if per_domain.get(d, 0) >= max(1, per_domain_cap):
            overflow.append(r)           # 同域超额：先压住，配额不够再补
            continue
        per_domain[d] = per_domain.get(d, 0) + 1
        out.append(r)
        if len(out) >= top_n:
            return out
    for r in overflow:                   # 多样性配额没填满 → 用高分溢出项补足 top_n
        if len(out) >= top_n:
            break
        out.append(r)
    return out


# ---- 模型语义重排（FR-11.1c 块2）--------------------------------------------
# 确定性重排只看**关键词覆盖度**——它治得了"多词只覆盖一个"的跑偏，治不了语义：
# 「2026 显卡 价格」的头名实测是个韩文 wiki 年份页（标题含 2026、就被算作命中）。
# 所以在候选池和最终结果之间加一道模型闸：让模型看标题+摘要挑真正对题的。
# 纪律同块H 裁判：**故障即降级**——解析不出/调用失败/返回空，一律退回确定性重排，绝不让搜索挂掉。

_RERANK_JSON_RE = re.compile(r"\{.*\}", re.S)


def build_rerank_prompt(query: str, candidates: list[dict], top_n: int) -> str:
    """构造重排 prompt：编号候选（标题/域名/摘要），要模型只回紧凑 JSON。"""
    lines = []
    for i, r in enumerate(candidates):
        snip = (r.get("snippet") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] {r.get('title', '')}\n    {_domain_of(r.get('url', ''))}"
                     + (f"\n    {snip[:200]}" if snip else ""))
    return (
        "你是搜索结果相关性排序器。下面是用户的查询，和一批候选结果（标题/域名/摘要）。\n"
        f"挑出**最能回答该查询**的至多 {top_n} 条，按相关性从高到低排列。\n"
        "判据：是否真正针对查询主题（不是只碰巧含关键词）、内容像不像有实质信息、"
        "来源是否可靠、时效是否对得上（查询含年份/最新时尤其看重）。\n"
        "宁缺毋滥：明显不对题的别硬凑数；但也别过度保守，够格的都可以留。\n"
        '只回 JSON，不要解释：{"pick": [编号, …], "why": "一句话说明挑选依据"}\n\n'
        f"【查询】{query}\n\n【候选】\n" + "\n".join(lines)
    )


def parse_rerank(raw: str, n_candidates: int) -> list[int]:
    """从模型输出里解析出候选编号列表（纯函数）。解析不出返回空列表＝调用方降级。"""
    m = _RERANK_JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    picked = data.get("pick") if isinstance(data, dict) else None
    if not isinstance(picked, list):
        return []
    out: list[int] = []
    for v in picked:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_candidates and i not in out:   # 越界/重复的编号丢掉（模型会瞎编）
            out.append(i)
    return out


def rerank_with_model(query: str, candidates: list[dict], top_n: int, rerank_fn,
                      per_domain_cap: int = 2) -> "tuple[list[dict], str]":
    """模型语义重排。返回 (结果, 用了哪种排序)；任何异常/空结果都降级到确定性重排。

    模型挑得不够 top_n 时，用确定性顺序补足——宁可多给两条，也别因为模型保守而让模型
    自己后面又发起一轮重搜（那更贵）。
    """
    fallback = rerank_results(query, candidates, top_n, per_domain_cap)
    if not rerank_fn or not candidates:
        return fallback, "确定性"
    try:
        raw = rerank_fn(build_rerank_prompt(query, candidates, top_n))
    except Exception:  # noqa: BLE001 — 重排是增值项，出错绝不能让搜索失败
        return fallback, "确定性(模型重排失败)"
    if not raw:                          # 闭包现读 config 发现开关关了 → 就是普通的确定性重排
        return fallback, "确定性"
    idx = parse_rerank(raw, len(candidates))
    if not idx:
        return fallback, "确定性(模型未给出有效结果)"
    # 模型选中的排前面，模型没选的按确定性顺序垫在后面，**最后统一过一次控源配额**。
    # 这个顺序很关键：先截断再补足的话，模型一口气全挑同站时会把多样性配额吃光
    # （补足只能从它自己挑的同站溢出里拿），单测钉住了这一点。
    ordered = [candidates[i] for i in idx]
    seen = {r.get("url") for r in ordered}
    ordered += [r for r in fallback if r.get("url") not in seen]
    return apply_domain_cap(ordered, top_n, per_domain_cap), "模型语义重排"


class _TextExtractor(HTMLParser):
    """HTML → 可读正文：跳过 script/style/noscript，块级标签换行，抓 <title>。"""
    _SKIP = {"script", "style", "noscript", "svg", "template"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "pre", "blockquote", "td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        elif data.strip():
            self.parts.append(data)


def extract_text(page: str) -> tuple[str, str]:
    """HTML → (标题, 正文文本)。空行压缩、行内空白归一。"""
    ex = _TextExtractor()
    try:
        ex.feed(page)
    except Exception:  # noqa: BLE001 — 残缺 HTML 尽力解析
        pass
    raw = "".join(ex.parts)
    return ex.title.strip(), _tidy(raw)


def _tidy(raw: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (raw or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ---- readability 式主正文抽取（纯逻辑）----------------------------------------
# 动机：原来的 extract_text 把整页文本倒出——实测抓一个 GitHub 仓库页 = 49,794 字符，
# 导航/文件列表/页脚全灌进上下文。主流做法（readability / trafilatura）是**按文本密度选正文块**：
# 链接占比高的是导航、类名带 nav|footer|sidebar 的是边角料，正文是"字多、链接少"的那棵子树。

_DROP_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "form", "nav",
                        "aside", "header", "footer", "iframe", "button", "select", "figure"})
_CANDIDATE_TAGS = frozenset({"article", "main", "div", "section", "td", "body"})
_BOILER_RE = re.compile(
    r"nav|menu|sidebar|side-bar|footer|header|masthead|comment|related|recommend|promo|"
    r"banner|cookie|breadcrumb|social|share|subscribe|newsletter|advert|\bads?\b|toc|"
    r"pagination|widget|tag-list|skip-link", re.I)
_CONTENT_RE = re.compile(r"article|content|main|post|entry|story|markdown|readme|"
                         r"doc-body|blog|text", re.I)
_MIN_MAIN_CHARS = 200        # 主正文候选的最低字数（低于此不认，退回整页）
_MAIN_MIN_RATIO = 0.10       # 主正文至少要占整页文本的比例（防把一小块侧栏当正文）


class _MainExtractor(HTMLParser):
    """把 HTML 走成一棵轻量块级树，记录每个节点的文本量/链接文本量，供密度打分选主正文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []       # 全局文本片段流（含换行）
        self.is_link: list[bool] = []    # 与 parts 等长：该片段是否在 <a> 内
        self.title = ""
        self.nodes: list[dict] = []      # 候选块：tag / sig(class+id) / start / end
        self._stack: list[dict] = []
        self._drop = 0
        self._in_title = False
        self._a = 0

    def _emit(self, s: str, link: bool = False) -> None:
        self.parts.append(s)
        self.is_link.append(link)

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._drop += 1
            return
        if self._drop:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            self._a += 1
        if tag in _TextExtractor._BLOCK:
            self._emit("\n")
        if tag in _CANDIDATE_TAGS:
            d = {k.lower(): (v or "") for k, v in attrs}
            node = {"tag": tag, "sig": f"{d.get('class', '')} {d.get('id', '')}",
                    "start": len(self.parts), "end": None}
            self.nodes.append(node)
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):   # <br/> 这类自闭合：只补换行，不进树
        if not self._drop and tag in _TextExtractor._BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a":
            self._a = max(0, self._a - 1)
        if tag in _TextExtractor._BLOCK:
            self._emit("\n")
        if tag in _CANDIDATE_TAGS:
            for i in range(len(self._stack) - 1, -1, -1):   # 容忍标签不闭合：回溯找同名
                if self._stack[i]["tag"] == tag:
                    for extra in self._stack[i:]:
                        extra["end"] = len(self.parts)
                    del self._stack[i:]
                    break

    def handle_data(self, data):
        if self._drop:
            return
        if self._in_title:
            self.title += data
        elif data.strip():
            self._emit(data, link=self._a > 0)

    def close(self):
        super().close()
        for node in self._stack:            # 收尾：未闭合的节点补 end
            node["end"] = len(self.parts)
        self._stack.clear()


def _node_metrics(ex: _MainExtractor, node: dict) -> tuple[int, int]:
    """(节点内文本字数, 其中属于链接的字数)。"""
    total = link = 0
    for i in range(node["start"], node["end"] or len(ex.parts)):
        s = ex.parts[i]
        if s == "\n":
            continue
        total += len(s)
        if ex.is_link[i]:
            link += len(s)
    return total, link


def score_node(tag: str, sig: str, text_len: int, link_len: int) -> float:
    """主正文候选打分（纯函数，便于单测）。

    基础分 = 非链接文本量；`<article>/<main>` 与内容类名加权，导航/页脚/侧栏类名重罚。
    """
    base = float(max(0, text_len - link_len))
    if tag in ("article", "main"):
        base *= 1.6
    if _CONTENT_RE.search(sig or ""):
        base *= 1.3
    if _BOILER_RE.search(sig or ""):
        base *= 0.2
    return base


def extract_main_text(page: str) -> tuple[str, str]:
    """HTML → (标题, **主正文**)。抽不出可信正文时回退整页文本（与 extract_text 同结果）。"""
    ex = _MainExtractor()
    try:
        ex.feed(page)
        ex.close()
    except Exception:  # noqa: BLE001 — 残缺 HTML 尽力解析
        pass
    title = ex.title.strip()
    full = _tidy("".join(ex.parts))
    if not full:
        return extract_text(page)
    best, best_score = None, 0.0
    for node in ex.nodes:
        if node["tag"] == "body":
            continue                      # body 恒等于整页，不作为"主正文"候选
        text_len, link_len = _node_metrics(ex, node)
        if text_len < _MIN_MAIN_CHARS:
            continue
        s = score_node(node["tag"], node["sig"], text_len, link_len)
        if s > best_score:
            best, best_score = node, s
    if best is None:
        return title, full
    main = _tidy("".join(ex.parts[best["start"]:best["end"] or len(ex.parts)]))
    if len(main) < _MIN_MAIN_CHARS or len(main) < _MAIN_MIN_RATIO * len(full):
        return title, full                # 选出来的太小，多半没选对 → 宁可全给
    return title, main


def _window_around_match(paras: list[str], terms: set, max_chars: int) -> str:
    """段落长过预算时，在第一处命中附近开一个窗口截取（纯函数）。

    比"从头截 max_chars"强：命中点常在正文中段（价格、结论、报错都在中间），从头截正好错过。
    """
    for p in paras:
        low = p.lower()
        pos = min((low.find(t) for t in terms if t in low), default=-1)
        if pos < 0:
            continue
        start = max(0, pos - max_chars // 3)        # 命中点前留三分之一预算做上文
        seg = p[start:start + max_chars]
        return ("…" if start > 0 else "") + seg + ("…" if start + max_chars < len(p) else "")
    return "\n".join(paras)[:max_chars]


def excerpt_for_query(text: str, focus: str, max_chars: int) -> str:
    """按 `focus` 从正文里摘相关段落（纯函数）——治"整页灌进上下文"。

    段落按查询词覆盖度打分，取高分段落但**按原文顺序**拼回（保持可读的上下文），
    段落之间不连续处标 `…`。全都不命中时退回取开头（总比空手好）。
    """
    paras = [p for p in (text or "").split("\n") if p.strip()]
    if not paras:
        return text or ""
    terms = _query_terms(focus)
    if not terms:
        return text[:max_chars]
    scored = []
    for i, p in enumerate(paras):
        low = p.lower()
        hits = sum(1 for t in terms if t in low)
        scored.append((-hits, i, p))
    scored.sort(key=lambda x: (x[0], x[1]))
    if not scored or scored[0][0] == 0:            # 一个词都没命中
        return text[:max_chars]
    # **段落比预算还长时开窗**（2026-08-10 修）：抽出来的正文经常整页就一个长段落
    # （没有换行），原来这种情况每个候选段落都因 `len(p) > max_chars` 被跳过，
    # 最后 picked 为空 → 返回空摘录。web_fetch 带 focus 抓这类页面时模型收到的是**一段空白**。
    if all(len(p) > max_chars for _h, _i, p in scored if _h < 0):
        return _window_around_match(paras, terms, max_chars)
    picked: set[int] = set()
    used = 0
    for neg_hits, i, p in scored:
        if neg_hits == 0:
            break
        if used + len(p) > max_chars:
            continue
        picked.add(i)
        used += len(p) + 1
        for j in (i - 1, i + 1):                   # 带上下文各一段（不计入命中，但占预算）
            if 0 <= j < len(paras) and j not in picked and used + len(paras[j]) <= max_chars:
                picked.add(j)
                used += len(paras[j]) + 1
    out: list[str] = []
    prev = -2
    for i in sorted(picked):
        if i != prev + 1 and out:
            out.append("…")
        out.append(paras[i])
        prev = i
    return "\n".join(out)


# ---- 工具 ---------------------------------------------------------------------

class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索（只读，免确认）：宽召回一大批候选 → 按相关性重排、控源多样、去重 → 返回若干条"
        "「标题/URL/摘要」，并**已自动抓取前几条的正文、按你的查询摘录**（结果里 ↳ 开头的部分）。"
        "适合查文档、报错信息、库用法、近期事实。**先看 ↳ 摘录再决定要不要 web_fetch 读全文**，"
        "多数问题看摘录就够了；摘录标了「未读到正文」的才需要 web_fetch（它会自动改用浏览器）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（中英文皆可，具体一点）"},
            "max_results": {"type": "integer", "description": "最多几条（默认按配置，上限 10）"},
        },
        "required": ["query"],
    }

    def __init__(self, *, engine: str = "auto", timeout: int = 20, max_results: int = 5,
                 widen_pages: int = 1, reranker=None,
                 read_top_n: int = 0, read_chars: int = _READ_CHARS,
                 artifacts=None) -> None:
        # 注意构造器默认＝**老行为**（不宽召回、不读正文、不重排），产品默认由 registry 从
        # config 注入（widen_pages=3 / read_top_n=3 / reranker）。同 research_judge 的做法：
        # 直接 new 出来的实例（存量单测、脚本）行为零变化，也不会在离线测试里偷偷连网。
        self._engine = engine
        self._timeout = timeout
        self._max_results = max_results
        self._widen_pages = max(1, min(int(widen_pages or 1), len(_DDG_PAGE_OFFSETS)))
        # 模型语义重排器 rerank_fn(prompt)->str（FR-11.1c 块2），由 registry 注入。
        # None = 只用确定性重排（行为同 3.54）。故障一律降级，不影响搜索可用性。
        self._reranker = reranker
        # 块3：搜完顺带读前 K 条正文，直接回「带来源摘录」的结果。
        # 为什么整合进 web_search 而不是单开一个深度工具：反复验证过的规律是**强模型在能凑合时
        # 会绕开新工具**（trace_run / search_code 都中招），而"直吞标题摘要"正是要治的病根——
        # 靠 prompt 劝不动，得由结构保证（同 v3.43 的教训）。
        self._read_top_n = max(0, int(read_top_n or 0))
        self._read_chars = max(200, int(read_chars or _READ_CHARS))
        self._artifacts = artifacts   # 读到的完整正文超 cap 时落产物（ADR 0021）

    def _search_one(self, engine: str, query: str) -> list[dict]:
        """跑一个引擎。**Bing 先走 RSS 结构化端点**，解析不出再降级啃 HTML。"""
        q = urllib.parse.quote(query)
        if engine == "bing":
            try:
                _, rss, _ = _http_get(
                    f"https://www.bing.com/search?q={q}&format=rss", self._timeout)
                items = parse_bing_rss(rss)
                if items:
                    return items
            except ToolError:
                pass                       # RSS 不通 → 降级 HTML，不让整条链路挂掉
            # 注意：不再传 count/first——实测 Bing 无视它们（恒 10 条、翻页返回同一批），
            # 传了只是自欺欺人。加宽候选靠 DDG 翻页（见 _DDG_PAGE_OFFSETS）。
            _, page, _ = _http_get(f"https://www.bing.com/search?q={q}", self._timeout)
            return parse_bing(page)
        return self._search_ddg(query)

    def _ddg_page(self, query: str, offset: int) -> list[dict]:
        """DDG lite 一页。offset=0 走 GET（最快、最不像自动化），翻页只能 POST。"""
        if offset <= 0:
            _, page, _ = _http_get(
                f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}", self._timeout)
        else:
            page = _http_post("https://lite.duckduckgo.com/lite/",
                              {"q": query, "s": str(offset)}, self._timeout)
        return parse_ddg_lite(page)

    def _search_ddg(self, query: str) -> list[dict]:
        """DDG 宽召回：并发翻 N 页再合并去重。**翻页失败不影响已拿到的页**（部分结果照用）。"""
        offsets = list(_DDG_PAGE_OFFSETS[:max(1, self._widen_pages)])
        if len(offsets) == 1:
            return self._ddg_page(query, 0)
        pages: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(offsets)) as pool:
            futs = {pool.submit(self._ddg_page, query, o): o for o in offsets}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    pages[futs[fut]] = fut.result()
                except Exception:  # noqa: BLE001 — 某一页翻不动就少那一页，别拖垮整次搜索
                    pages[futs[fut]] = []
        return merge_pages([pages.get(o, []) for o in offsets])   # 按页序合并，首页优先

    def _gather(self, engines: "tuple[str, ...]", query: str) -> tuple[list, list[str]]:
        """**并发**跑多个引擎，返回 ([(engine, results)…], 错误说明)。

        并发而非"第一个有结果就返回"：多引擎交叉验证能压掉单引擎的抽风/投毒，
        且总耗时 ≈ 最慢的那个，不是各家相加。
        """
        per: list[tuple[str, list[dict]]] = []
        errors: list[str] = []
        if len(engines) == 1:
            eng = engines[0]
            try:
                per.append((eng, self._search_one(eng, query)))
            except ToolError as e:
                errors.append(f"{eng}: {e}")
            return per, errors
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(engines)) as pool:
            futs = {pool.submit(self._search_one, e, query): e for e in engines}
            for fut in concurrent.futures.as_completed(futs):
                eng = futs[fut]
                try:
                    per.append((eng, fut.result()))
                except ToolError as e:
                    errors.append(f"{eng}: {e}")
                except Exception as e:      # noqa: BLE001 — 线程里的意外一律转可读
                    errors.append(f"{eng}: {type(e).__name__}: {e}")
        per.sort(key=lambda x: engines.index(x[0]))   # 固定顺序，结果可复现
        return per, errors

    def run(self, params: dict) -> str:
        query = (params.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        try:
            n = int(params.get("max_results") or self._max_results)
        except (TypeError, ValueError):
            n = self._max_results
        n = max(1, min(n, MAX_RESULTS_CAP))

        engines = _ENGINES if self._engine == "auto" else (self._engine,)
        per, errors = self._gather(engines, query)
        got = [(e, rs) for e, rs in per if rs]
        if not got:
            errors += [f"{e}: 无结果或页面结构无法解析" for e, rs in per if not rs]
            raise ToolError("搜索失败：" + ("；".join(errors) if errors else "无结果"))
        # 跨引擎 RRF 融合 → 重排（模型语义优先、确定性兜底）+控源多样性 → top-n
        fused = fuse_results(got)
        ranked, how = rerank_with_model(query, fused, n, self._reranker)
        used = "+".join(e for e, _ in got)
        head = (f"[搜索结果·{used}] {query}"
                f"（{len(got)} 个引擎并发、RRF 融合去重，自 {len(fused)} 条候选按{how}选 {len(ranked)} 条）")
        if errors:
            head += f"\n[注] 部分来源未用上：{'；'.join(errors)}"
        bodies = self._read_bodies(ranked[:self._read_top_n], query) if self._read_top_n else {}
        if bodies:
            head += f"\n[已读正文] 前 {len(bodies)} 条已抓取正文并按查询摘录（下面 ↳ 的部分）"
        lines = [head]
        for i, r in enumerate(ranked, 1):
            src = r.get("sources") or []
            tag = f"  [{'+'.join(src)}]" if len(src) > 1 else ""
            lines.append(f"{i}. {r['title']}{tag}\n   {r['url']}"
                         + (f"\n   {r['snippet']}" if r["snippet"] else ""))
            body = bodies.get(r["url"])
            if body:
                lines.append("   ↳ " + body.replace("\n", "\n     "))
        return "\n".join(lines)

    def _read_bodies(self, results: list[dict], query: str) -> dict:
        """并发抓前 K 条正文，按 query 摘录。返回 {url: 摘录}。

        **任何一条读不动都只影响它自己**：反爬/超时/空页都标一句原因跳过，
        搜索结果照常返回——读正文是增值项，绝不能让它把搜索拖失败。
        受阻的这里不切浏览器（那是 web_fetch 的自动升级职责），标注让模型按需自己去读。
        """
        results = [r for r in results if (r.get("url") or "").startswith(("http://", "https://"))]
        if not results:
            return {}
        out: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(results)) as pool:
            futs = {pool.submit(self._read_one, r["url"], query): r["url"] for r in results}
            for fut in concurrent.futures.as_completed(futs):
                url = futs[fut]
                try:
                    out[url] = fut.result()
                except Exception as e:  # noqa: BLE001
                    out[url] = f"[读取失败：{type(e).__name__}]"
        return {u: out[u] for u in (r["url"] for r in results) if out.get(u)}   # 保持结果顺序

    def _read_one(self, url: str, query: str) -> str:
        """抓一页 → 主正文 → 按 query 摘录（不够长就整段给）。"""
        try:
            _final, body, ctype = _http_get(url, self._timeout)
        except ToolError as e:
            # 403/429 之类多半是反爬（知乎实测就是 403），和"正文空壳"同一类处置：
            # 这里不切浏览器（那是 web_fetch 的自动升级职责），但要**指路**，别让模型以为此页没救。
            reason = re.sub(r"请求失败（[^）]*）：", "", str(e))[:80]
            return f"[未读到正文：{reason}——需要的话用 web_fetch 读它（会自动改用浏览器）]"
        ct = (ctype or "").lower()
        if ct and not any(k in ct for k in ("html", "text", "json", "xml")):
            return f"[未读到正文：不是文本内容（{ct.split(';')[0]}）]"
        is_html = "html" in ct or bool(re.search(r"<\s*html", body[:2000], re.I))
        _title, text = extract_main_text(body) if is_html else ("", body)
        if looks_garbled(text):
            # 兜底：编码/解压都试过仍是噪声（罕见，但真跑踩过 gzip+gb2312 双重误判）。
            # 宁可说"没读到"，也不能把二进制噪声当正文喂给模型——那比没有更糟。
            return "[未读到正文：内容无法正确解码（疑似二进制或未知编码）]"
        blocked = looks_blocked(text, is_html)
        if blocked:
            return f"[未读到正文：{blocked}——需要的话用 web_fetch 读它（会自动改用浏览器）]"
        text = (text or "").strip()
        if not text:
            return "[未读到正文：页面没有可提取的文本]"
        excerpt = excerpt_for_query(text, query, self._read_chars) if len(text) > self._read_chars \
            else text
        note = ""
        if self._artifacts is not None:
            art = self._artifacts.maybe_put(text, len(excerpt), tool="web_search", origin=url)
            if art is not None:
                note = f"\n[完整正文 {art.chars:,} 字符已存 {art.rel}]"
        tail = "" if len(text) <= self._read_chars else f"（摘自 {len(text):,} 字符正文）"
        return f"{excerpt}{tail}{note}"


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "抓取一个网页并转成可读正文（只读，免确认）。配合 web_search 用：先搜到 URL 再读内容。"
        "已自动去掉导航/侧栏/页脚只留主正文；**给 focus 说明你想找什么，就只回相关片段**（强烈建议给，省上下文）。"
        "遇到反爬/登录墙/JS 空壳时，若已开浏览器穿透会**自动改用浏览器读同一页**，你不用自己切换。"
        "也可以抓 http://localhost:端口 来检查自己启动的 dev server。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) 地址"},
            "focus": {"type": "string",
                      "description": "你想从这页里找什么（给了就只回相关段落，大幅省上下文）"},
            "max_chars": {"type": "integer",
                          "description": f"正文输出上限（默认 {DEFAULT_FETCH_CHARS}）"},
        },
        "required": ["url"],
    }

    def __init__(self, *, timeout: int = 20, max_chars: int = DEFAULT_FETCH_CHARS,
                 browser_reader=None, artifacts=None) -> None:
        self._timeout = timeout
        self._max_chars = max_chars
        # ADR 0021：抓到了却被 cap 掉的原文落成产物（None=照旧丢弃）。
        # 判据量的是**原文**长度而非返回长度——本工具的 cap 默认正好等于阈值，量返回长度会永远卡边界。
        self._artifacts = artifacts
        # 浏览器兜底读取器：callable(url) -> str | None。接了浏览器穿透时由 registry 注入。
        # **自动升级而不是让模型选路**：HTTP 判定受阻就换浏览器读同一 URL（v3.43 的"不许绕路"
        # 本意保留，但不再连唯一的快路一起砍掉）。
        self._browser_reader = browser_reader

    def _clip_and_keep(self, text: str, cap: int, focus: str, url: str) -> str:
        """裁剪正文；被 cap 掉的原文落产物并附句柄（省上下文的同时不丢数据）。"""
        out = _clip(text, cap, focus)
        if self._artifacts is None:
            return out
        art = self._artifacts.maybe_put(text, len(out), tool="web_fetch", origin=url)
        if art is None:
            return out
        return (out + f"\n[产物 {art.id}] 本页完整正文（{art.chars:,} 字符）已存 {art.rel}"
                      "——要被截掉/未摘录的部分就 grep_search / read_file(offset=) 它，别重抓。")

    def _read_via_browser(self, url: str) -> "str | None":
        if not self._browser_reader:
            return None
        try:
            out = self._browser_reader(url)
        except Exception as e:  # noqa: BLE001 — 浏览器侧任何异常都不该让 web_fetch 崩
            return f"[浏览器兜底失败] {type(e).__name__}: {e}"
        return out

    def run(self, params: dict) -> str:
        url = (params.get("url") or "").strip()
        if not url:
            raise ToolError("url 不能为空")
        focus = (params.get("focus") or "").strip()
        try:
            cap = int(params.get("max_chars") or self._max_chars)
        except (TypeError, ValueError):
            cap = self._max_chars
        cap = max(500, min(cap, 100_000))

        final_url, body, ctype = _http_get(url, self._timeout)
        is_html = "html" in ctype.lower() or bool(re.search(r"<\s*html", body[:2000], re.I))
        if is_html:
            title, text = extract_main_text(body)   # 主正文（去导航/页脚/侧栏）
        else:
            title, text = "", body  # JSON / 纯文本直出
        head = f"[URL] {final_url}" + (f"\n[标题] {title}" if title else "")

        # 反爬/需登录/JS 空壳的「假成功」：接了浏览器穿透就**自动**改用浏览器读同一 URL
        blocked = looks_blocked(text, is_html)
        if blocked:
            via = self._read_via_browser(final_url)
            if via and not via.startswith("[浏览器兜底失败]"):
                return (f"[URL] {final_url}\n[读取方式] HTTP 受阻（{blocked}）→ **已自动改用浏览器读取**"
                        "（浏览器带你的登录态，内容可能包含登录后才可见的信息）\n\n"
                        + self._clip_and_keep(via, cap, focus, final_url))
            hint = (f"（{via}）" if via else
                    "（未接浏览器穿透）")
            return (f"⚠ 抓取受阻（{blocked}）{hint}——下面内容可能是拦截页或不完整。\n"
                    "换官方 API / 其它来源，或开启浏览器穿透后重试。\n\n"
                    f"{head}\n\n{text if text.strip() else '(页面没有可提取的文本)'}")
        if not text.strip():
            return f"{head}\n\n(页面没有可提取的文本)"
        return f"{head}\n\n{self._clip_and_keep(text, cap, focus, final_url)}"


def _clip(text: str, cap: int, focus: str = "") -> str:
    """按预算裁剪正文：给了 focus 就摘相关片段，否则截断（纯逻辑，统一两条出口的行为）。"""
    if len(text) <= cap:
        return text
    if focus:
        out = excerpt_for_query(text, focus, cap)
        return out + f"\n…[已按 focus「{focus[:40]}」摘录相关片段；要全文就不传 focus 或调大 max_chars]"
    return text[:cap] + f"\n…[正文过长，已截断至 {cap} 字符；需要更多可调大 max_chars 或传 focus 摘录]"
