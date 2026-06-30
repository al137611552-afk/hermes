"""联网检索工具（FR-11.1）：web_search + web_fetch。零新依赖（urllib + html.parser + 正则）。

- web_search：免 key 搜索。auto 链路 = Bing 优先（国内外均可达，实测）、DDG lite 兜底；
  解析器为纯函数（parse_bing / parse_ddg_lite），页面改版解析不出时自动换下一个源、
  全挂给可读错误。真实链接从跳转参数还原（Bing `u=a1<base64>` / DDG `uddg=`）。
- web_fetch：抓取网页转正文文本（HTMLParser 去 script/style、保标题；JSON/纯文本直出）；
  下载上限 2MB、输出截断带标记。允许抓 localhost（配合后台 dev server 自测是特性）。
两工具均只读、非危险、不过权限 gate，并进只读子 Agent 角色白名单。
"""
from __future__ import annotations

import base64
import html as html_mod
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
MAX_RESULTS_CAP = 10             # 单次搜索条数硬上限
_ENGINES = ("bing", "duckduckgo")


# ---- HTTP（IO，集中一处） -----------------------------------------------------

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
            charset = r.headers.get_content_charset() or "utf-8"
            return r.geturl(), data.decode(charset, errors="replace"), \
                (r.headers.get("Content-Type") or "")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001 — 网络错误统一转可读
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
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return ex.title.strip(), text


# ---- 工具 ---------------------------------------------------------------------

class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索（只读，免确认）：返回若干条「标题/URL/摘要」。适合查文档、报错信息、"
        "库用法、近期事实。拿到结果后用 web_fetch 读具体页面正文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（中英文皆可，具体一点）"},
            "max_results": {"type": "integer", "description": "最多几条（默认按配置，上限 10）"},
        },
        "required": ["query"],
    }

    def __init__(self, *, engine: str = "auto", timeout: int = 20, max_results: int = 5) -> None:
        self._engine = engine
        self._timeout = timeout
        self._max_results = max_results

    def _search_one(self, engine: str, query: str) -> list[dict]:
        q = urllib.parse.quote(query)
        if engine == "bing":
            _, page, _ = _http_get(f"https://www.bing.com/search?q={q}", self._timeout)
            return parse_bing(page)
        _, page, _ = _http_get(f"https://lite.duckduckgo.com/lite/?q={q}", self._timeout)
        return parse_ddg_lite(page)

    def run(self, params: dict) -> str:
        query = (params.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        try:
            n = int(params.get("max_results") or self._max_results)
        except (TypeError, ValueError):
            n = self._max_results
        n = max(1, min(n, MAX_RESULTS_CAP))

        chain = _ENGINES if self._engine == "auto" else (self._engine,)
        errors: list[str] = []
        for eng in chain:
            try:
                results = self._search_one(eng, query)
            except ToolError as e:
                errors.append(f"{eng}: {e}")
                continue
            if results:
                lines = [f"[搜索结果·{eng}] {query}"]
                for i, r in enumerate(results[:n], 1):
                    lines.append(f"{i}. {r['title']}\n   {r['url']}"
                                 + (f"\n   {r['snippet']}" if r["snippet"] else ""))
                return "\n".join(lines)
            errors.append(f"{eng}: 无结果或页面结构无法解析")
        raise ToolError("搜索失败：" + "；".join(errors))


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "抓取一个网页并转成可读正文（只读，免确认）。配合 web_search 用：先搜到 URL 再读内容。"
        "也可以抓 http://localhost:端口 来检查自己启动的 dev server。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) 地址"},
            "max_chars": {"type": "integer",
                          "description": f"正文输出上限（默认 {DEFAULT_FETCH_CHARS}）"},
        },
        "required": ["url"],
    }

    def __init__(self, *, timeout: int = 20, max_chars: int = DEFAULT_FETCH_CHARS) -> None:
        self._timeout = timeout
        self._max_chars = max_chars

    def run(self, params: dict) -> str:
        url = (params.get("url") or "").strip()
        if not url:
            raise ToolError("url 不能为空")
        try:
            cap = int(params.get("max_chars") or self._max_chars)
        except (TypeError, ValueError):
            cap = self._max_chars
        cap = max(500, min(cap, 100_000))

        final_url, body, ctype = _http_get(url, self._timeout)
        is_html = "html" in ctype.lower() or bool(re.search(r"<\s*html", body[:2000], re.I))
        if is_html:
            title, text = extract_text(body)
        else:
            title, text = "", body  # JSON / 纯文本直出
        if len(text) > cap:
            text = text[:cap] + f"\n…[正文过长，已截断至 {cap} 字符；需要更多可调大 max_chars]"
        head = f"[URL] {final_url}" + (f"\n[标题] {title}" if title else "")
        # 反爬/需登录/JS 空壳的「假成功」：明确报受阻 + 建议改用浏览器穿透（有登录态、能渲染 JS、过反爬）
        blocked = looks_blocked(text, is_html)
        if blocked:
            return (f"⚠ 抓取受阻（{blocked}）——下面内容可能是拦截页或不完整。\n"
                    f"**若已开启浏览器穿透，改用 browser_navigate 打开 {final_url} 再 browser_snapshot 读**"
                    "（浏览器有你的登录态、能渲染 JS、过这类反爬）；没开浏览器穿透就换官方 API / 其它来源。\n\n"
                    f"{head}\n\n{text if text.strip() else '(页面没有可提取的文本)'}")
        return f"{head}\n\n{text if text.strip() else '(页面没有可提取的文本)'}"
