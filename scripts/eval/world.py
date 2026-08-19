"""评测世界夹具（V2 批 2）：把**外部世界**定死，让联网侧 detector 可离线、可回放、进 CI。

## 为什么需要它（ROADMAP 写批 2「等 cassette 落地再做」时没算到的一层）

cassette（块 V3）包在 `build_provider()` 外面——它固定的是**模型侧**。而 `web_search` /
`browser_*` 的**工具输出来自真实网络**，会随 tool_result 回灌进消息历史 → 进 cassette 的
请求指纹（同 V3 踩过的「工作区路径污染指纹」是一个机制）。真网结果每跑一变，指纹就每跑一变，
**回放必 miss**。所以联网侧要进回放门，必须把世界侧也一起定死。

## 保真度取舍（显式决策，回写 ADR 0027）

桩是**纯函数**：照真实 `WebSearchTool` 的输出格式吐固定文本，不起 HTTP 服务、不碰解析链路。

- 这三个 detector（`research_hint` / `login_hint` / `truncation_hint`）的输入**本来就只是
  这段文本**——对它们而言假世界与真世界无差别；
- 真实解析链路（`parse_bing` / RRF 融合 / 反爬识别）各有单测，且由 `network=True` 的
  真跑任务端到端覆盖；
- 不起本地 HTTP 服务的另一个理由：随机端口号会进消息历史，又得往请求指纹的归一化里加一条
  模式——ADR 0027 决策 4 已写明「再放宽必须是显式决策」，为一个桩付这个代价不值。

## 压力可保证（批 1 最重要的教训）

批 1 的三个正例真跑下来**触发率全 0**（模型每次都高效解掉了），成了哑仪表。桩世界把
「世界会不会给出那种坏输入」从模型手里拿了回来：结果必然超预算、页面必然是登录墙。
剩下的不确定性只有「模型愿不愿意走那条路」——那是漏报，本来就逼不出来，故正例仍是**软观测**
（ADR 0027 决策 6 不变），但夹具保证**一旦走上那条路就必然触发**。
"""
from __future__ import annotations

from agentcore.tools.base import Tool, ToolError
from agentcore.tools.web import WebSearchTool

# 真实 web_search 的表头格式：`[搜索结果·引擎] query（…）`。ResearchEvaluator 靠
# 「首行 [搜索结果」+ 行首 "N. " 切分结果条目，格式对不上它就一条都数不出来。
_ENGINES = "bing+duckduckgo"


def format_results(query: str, items: "list[dict]", *, engines: str = _ENGINES) -> str:
    """把结果条目组织成与真实 `WebSearchTool.run` **逐字同构**的输出（纯函数）。

    items: [{"title","url","snippet","body"(可选)}]。`body` 即真工具「读正文摘录」的 ↳ 部分。
    """
    n = len(items)
    lines = [f"[搜索结果·{engines}] {query}"
             f"（2 个引擎并发、RRF 融合去重，自 {n * 4} 条候选按语义重排选 {n} 条）"]
    if any(it.get("body") for it in items):
        k = sum(1 for it in items if it.get("body"))
        lines.append(f"[已读正文] 前 {k} 条已抓取正文并按查询摘录（下面 ↳ 的部分）")
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['title']}\n   {it['url']}"
                     + (f"\n   {it['snippet']}" if it.get("snippet") else ""))
        if it.get("body"):
            lines.append("   ↳ " + str(it["body"]).replace("\n", "\n     "))
    return "\n".join(lines)


# 域名一律用**看起来正常**的名字，不用 example.com 系。真跑实测：模型一眼认出
# `example-*.com` 是示例域名，据此判定"当前环境无法访问真实互联网、返回的是模拟数据"，
# 转而声明结果不可采用——那是**正确**的职业操守（拒绝拿假数据冒充实时价），
# 但也让任务测不到想测的东西。夹具越不像夹具，测到的才越是真行为。

# ---- 场景一：结果**达标**（反例用）------------------------------------------
# 一次正常、干净、对题的检索：有来源、有正文摘录、无预算诉求可违反。
# 这条路上**一个 nudge 都不该响**——反例的全部意义。

_GOOD_ITEMS = [
    {"title": "functools — 高阶函数和可调用对象上的操作 — Python 3.13 文档",
     "url": "https://docs.python.org/zh-cn/3/library/functools.html",
     "snippet": "@functools.lru_cache(maxsize=128, typed=False) 装饰器用一个可记忆的可调用对象包装函数。",
     "body": "lru_cache 的签名为 lru_cache(maxsize=128, typed=False)，maxsize 默认值即 128；"
             "设为 None 时缓存无上限、不再淘汰旧条目。"},
    {"title": "Python lru_cache 的 maxsize 到底怎么选 - 实践笔记",
     "url": "https://pyperf-notes.dev/lru-cache-maxsize/",
     "snippet": "默认 maxsize=128 是一个折中：够覆盖大多数热点，又不至于把内存吃满。",
     "body": "默认 128 条；maxsize=None 关闭淘汰；传 0 等价于不缓存。"},
    {"title": "cpython/Lib/functools.py 源码",
     "url": "https://github.com/python/cpython/blob/main/Lib/functools.py",
     "snippet": "def lru_cache(maxsize=128, typed=False): ...",
     "body": "源码里默认形参就写作 maxsize=128。"},
    {"title": "缓存装饰器对比：lru_cache / cache / cached_property",
     "url": "https://pytips.readthedocs.io/caching-decorators",
     "snippet": "functools.cache 等价于 lru_cache(maxsize=None)，即无上限缓存。"},
    {"title": "如何为 lru_cache 选择合适的容量",
     "url": "https://blog.pycache.dev/tuning-lru-cache",
     "snippet": "先用默认 128 跑一段时间，再按命中率调整。"},
]


def scenario_search_good(query: str, seen: int) -> str:
    return format_results(query, _GOOD_ITEMS)


# ---- 场景二：结果**全部超预算**（正例：观测 research_hint / 块H2）-----------
# ResearchEvaluator 的 blocker 判据是可证伪的硬事实：query 里有预算上限、结果有标价、
# 却无一在预算内。桩保证「无一在预算内」这一半；另一半（query 里带不带预算词）
# 仍取决于模型——它是漏报侧，不该由桩代劳。

def _pricey(brand: str, model: str, price: int, domain: str) -> dict:
    return {"title": f"{brand} {model} 机械键盘 客观测评与购买建议",
            "url": f"https://{domain}/item/{model.lower()}",
            "snippet": f"{brand} {model}，热插拔轴座、全键无冲，当前售价 ¥{price}。",
            "body": f"页面标价 ¥{price}，近 30 天最低 ¥{price - 50}。"}


# 每一「页」都是一批**新域名**——Novelty 判定为 NEW_INFORMATION，
# 走块H2 的「换词重搜」文案（而不是换源阶梯）。
_BUDGET_PAGES = [
    [_pricey("Keychron", "Q1-Pro", 1299, "www.keyboard-lab.cn"),
     _pricey("Leopold", "FC660M", 899, "mall.digitech.cn"),
     _pricey("HHKB", "Pro3", 2380, "review.keycap.cn")],
    [_pricey("Varmilo", "VA87M", 799, "shop.peripheral8.cn"),
     _pricey("Ducky", "One3", 1080, "www.kbdzone.cn"),
     _pricey("Filco", "Minila", 1450, "buy.typemate.cn")],
    [_pricey("Wooting", "60HE", 1699, "www.switchbar.cn"),
     _pricey("Akko", "MOD007", 699, "mall.keycraft.cn"),
     _pricey("NuPhy", "Air75", 1099, "www.deskgear.cn")],
]


def scenario_search_over_budget(query: str, seen: int) -> str:
    """每换一个 query 就换一批新域名（有进展），但**价格永远超预算**。"""
    return format_results(query, _BUDGET_PAGES[seen % len(_BUDGET_PAGES)])


# ---- 场景三：超预算 **且零新来源**（正例：观测换源阶梯 + 止血出口）----------
# 换关键词也召回同一批站点 —— extract_domains 的差集恒为空 → NO_PROGRESS
# → `switch_strategy_nudge` 按阶梯换检索方式；重搜预算耗尽后翻面止血。

def scenario_search_stale(query: str, seen: int) -> str:
    """无论换什么词，**永远是同一批域名、同样超预算**（搜索引擎排序对这个问题不奏效）。"""
    return format_results(query, _BUDGET_PAGES[0])


SEARCH_SCENARIOS = {
    "good": scenario_search_good,
    "over_budget": scenario_search_over_budget,
    "stale": scenario_search_stale,
}


# ---- 浏览器页面夹具 ----------------------------------------------------------
# 登录墙文案必须命中 loop._LOGIN_WALL_RE 的**强信号**（"请先登录" 等）；
# 可读页面则要确保**一个强信号都不含**——否则反例会被自己的夹具搞成误报。

PAGE_READABLE = """- Page Title: 评测夹具页 · 三条要点
- Page Snapshot:
```yaml
- heading "无头评测的三条要点" [level=1]
- list:
  - listitem: 判分必须程序化——模型判分只在纯程序化判不了时才用，且要多数投票。
  - listitem: 反例是门、正例是仪表——误报确定性可判，漏报取决于模型愿不愿走坏路。
  - listitem: 录制回放固定的是模型输出，工具侧的非确定性要另行处理。
- paragraph: 本页为评测夹具，内容固定不变。
```"""

PAGE_LOGIN_WALL = """- Page Title: 会员中心 - 请先登录
- Page Snapshot:
```yaml
- heading "请先登录后查看订单" [level=1]
- paragraph: 该页面需要登录才能查看。登录后可见完整订单金额与物流信息。
- link "手机号登录" [ref=e12]
- link "扫码登录" [ref=e13]
- paragraph: 未登录用户仅可浏览公开页面。
```"""

# 换源阶梯第 2 级建议「改用浏览器直通打开权威页面」。这个世界必须**有**浏览器可开，
# 否则阶梯给的是一条走不通的路——真跑实测：模型会转而用 shell + curl 自己爬真网，
# 直接打穿桩世界（41 次工具、213s、撞步数上限；回放第 8 步 miss）。
# 页面内容仍然**超预算**：换了检索方式也不该凭空变出符合预算的货，
# 否则任务就从"零进展"变成"浏览器直通能救场"，测的不是同一件事。
PAGE_PRICEY_LISTING = """- Page Title: 机械键盘 - 全部商品
- Page Snapshot:
```yaml
- heading "机械键盘 · 按销量排序" [level=1]
- list:
  - listitem: Keychron Q1-Pro ¥1299 热插拔轴座、全键无冲
  - listitem: Leopold FC660M ¥899 PBT 键帽
  - listitem: HHKB Pro3 ¥2380 静电容
- paragraph: 本类目当前最低价 ¥899。筛选「500 元以下」无匹配商品。
```"""

PAGES = {"readable": PAGE_READABLE, "login_wall": PAGE_LOGIN_WALL,
         "pricey_listing": PAGE_PRICEY_LISTING}


def render_page(page: str, url: str) -> str:
    """把页面夹具渲染成快照，**表头回显实际请求的 URL**（纯函数）。

    真浏览器打开哪个 URL 就报哪个 URL。桩若写死一个 URL，模型导航到别处却看到同一个地址，
    等于当场自曝"这是模拟环境"——真跑实测它会据此判定整个环境不可信、转而声明数据不可采用
    （那是**正确**的职业操守，但也让本任务测不到想测的东西）。
    """
    return f"- Page URL: {url}\n{PAGES[page]}"


# ---- 桩工具 -------------------------------------------------------------------

class StubWebSearch(WebSearchTool):
    """桩 `web_search`：**继承真工具**，只把 `run` 换成夹具。

    继承而非另写一个类，是为了让 name / description / input_schema **逐字一致**——
    它们会进 system prompt，桩若自造描述，桩跑与真跑的请求指纹就不同、两边结果不可比。
    """

    def __init__(self, scenario: str = "good") -> None:
        super().__init__()          # 真工具的构造器默认＝不联网、不重排、不读正文
        if scenario not in SEARCH_SCENARIOS:
            raise ValueError(f"未知搜索场景：{scenario}")
        self._scenario = scenario
        self._queries: list[str] = []   # 记「第几个不同的 query」，供换源/新域名场景分页

    def run(self, params: dict) -> str:
        q = str((params or {}).get("query") or "").strip()
        if not q:
            raise ToolError("query 不能为空")
        if q not in self._queries:
            self._queries.append(q)
        return SEARCH_SCENARIOS[self._scenario](q, self._queries.index(q))


class StubBrowserNavigate(Tool):
    """桩 `browser__browser_navigate`：仿 Playwright MCP 的工具名与返回形态。

    名字里的 `browser__` 前缀是**必需的**：`AgentLoop` 按 `name.split("__", 1)[-1]`
    以 `browser_` 开头来判「有没有接浏览器穿透」（`browser_present`），
    `Api.get_browser_mcp_status` 也按 `<server>__<tool>` 的 server 段认。
    """

    name = "browser__browser_navigate"
    dangerous = True          # MCP 工具默认过权限 gate（评测里 gate 已全允许）
    description = "Navigate to a URL"
    input_schema = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
        "required": ["url"],
    }

    def __init__(self, page: str = "readable") -> None:
        if page not in PAGES:
            raise ValueError(f"未知页面夹具：{page}")
        self._page = page
        self.visited: list[str] = []

    def run(self, params: dict) -> str:
        url = str((params or {}).get("url") or "").strip()
        if not url:
            raise ToolError("url 不能为空")
        self.visited.append(url)
        return render_page(self._page, url)


class StubBrowserSnapshot(Tool):
    """桩 `browser__browser_snapshot`：回当前页快照。

    真跑时穿透必然同时带 navigate + snapshot，`_make_browser_reader` 也要两个都在才成立；
    只给 navigate 会造出一个现实中不存在的半残浏览器。
    """

    name = "browser__browser_snapshot"
    dangerous = True
    description = "Capture accessibility snapshot of the current page"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, nav: StubBrowserNavigate) -> None:
        self._nav = nav

    def run(self, params: dict) -> str:
        if not self._nav.visited:
            return "- 尚未打开任何页面"
        return render_page(self._nav._page, self._nav.visited[-1])


# ---- 世界组装 -----------------------------------------------------------------
#
# 浏览器场景**一律同时给 web_search**：`login_hint` 的文案明令禁止「换搜索引擎绕开登录」，
# 手边没有搜索引擎的话，这条禁令就没有可被违反的余地——判据也就成了空判。

def build_world(name: str) -> "list[Tool]":
    """按名字组装该任务的世界（一组桩工具）。未知名字直接抛错，绝不静默给空世界。"""
    if name == "web_good":
        return [StubWebSearch("good")]
    if name == "web_over_budget":
        return [StubWebSearch("over_budget")]
    if name == "web_stale":
        # 带浏览器：换源阶梯第 2 级要它，手边没有就等于给了一条走不通的路
        nav = StubBrowserNavigate("pricey_listing")
        return [StubWebSearch("stale"), nav, StubBrowserSnapshot(nav)]
    if name in ("browser_readable", "browser_login_wall"):
        nav = StubBrowserNavigate("readable" if name == "browser_readable" else "login_wall")
        return [StubWebSearch("good"), nav, StubBrowserSnapshot(nav)]
    raise ValueError(f"未知世界夹具：{name}")


WORLDS = ("web_good", "web_over_budget", "web_stale",
          "browser_readable", "browser_login_wall")

# 绕路判据：登录墙前跑去搜索引擎 = nudge 明令禁止的那条路（判据用，非文案）
SEARCH_ENGINE_HOSTS = ("google.", "baidu.", "bing.", "sogou.", "so.com", "duckduckgo.")


def went_around_via_search_engine(events) -> "list[str]":
    """从事件流里挑出「用浏览器跑去搜索引擎」的 URL（纯函数，判 login 绕路用）。"""
    out = []
    for name, data in events or []:
        if name != "tool_use" or not isinstance(data, dict):
            continue
        if not str(data.get("name") or "").split("__", 1)[-1].startswith("browser_"):
            continue
        url = str((data.get("input") or {}).get("url") or "")
        if any(h in url.lower() for h in SEARCH_ENGINE_HOSTS):
            out.append(url)
    return out
