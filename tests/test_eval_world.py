"""V2 批 2（联网侧）世界夹具的离线自检。

不调模型、不联网、不起服务。验的是**夹具本身**——沿用换手真跑与批 1 的同一条教训：
没跑过的夹具也是未验代码，"任务挂了"要能立刻分清是被测对象错了还是任务设定错了。

批 1 真跑暴露的具体教训是"正例全是哑仪表"（三个正例触发率 0，因为夹具压力不够）。
所以这里的核心断言不是"代码能跑"，而是**夹具真的越过/低于各自的门**：

  · 正例夹具喂进真 detector → **必须**响；
  · 反例夹具喂进真 detector → **必须**不响（反例自带地雷是最阴的失败模式：
    夹具里不小心写了个"请登录"，反例就会永远误报，而人会去查 detector）。

运行：python tests/test_eval_world.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from agentcore.agent.loop import (  # noqa: E402
    detect_login_wall, detect_low_quality_research, extract_domains, truncation_nudge,
)
from agentcore.tools.web import WebSearchTool  # noqa: E402
from tasks import TASKS, _check_login_wall, _check_over_budget, _check_stale_research  # noqa: E402
from world import (  # noqa: E402
    PAGE_PRICEY_LISTING, PAGE_READABLE, PAGES, WORLDS, StubBrowserNavigate,
    StubBrowserSnapshot, StubWebSearch, build_world, format_results, render_page,
    went_around_via_search_engine,
)


class _Call:
    """仿 provider 的 tool_use 调用对象（loop 里的 detector 只读 name/id/input）。"""

    def __init__(self, name, cid, params=None):
        self.name, self.id, self.input = name, cid, params or {}


class _R:
    def __init__(self, answer="", events=None):
        self.answer, self.events = answer, events or []


def _search(scenario: str, query: str) -> str:
    return StubWebSearch(scenario).run({"query": query})


# ---- 桩与真工具的形态一致（防桩漂移）-----------------------------------------

def test_stub_search_is_schema_identical_to_the_real_tool():
    """name / description / input_schema 逐字一致。

    它们会进 system prompt，也就进 cassette 的请求指纹。桩若自造描述，桩跑与真跑的
    请求就不是同一个请求，两边的结果没有可比性——这正是继承真工具而不是另写一个类的原因。
    """
    real, stub = WebSearchTool(), StubWebSearch("good")
    assert stub.name == real.name == "web_search"
    assert stub.description == real.description
    assert stub.input_schema == real.input_schema


def test_stub_browser_name_makes_browser_present_true():
    """`browser_present` 判的是 `name.split("__", 1)[-1].startswith("browser_")`。

    名字前缀写错 = 整个 login_hint 通路静默失效（detector 根本不会被调用），
    而任务只会表现为"触发率 0"——跟"模型没走那条路"长得一模一样。
    """
    nav = StubBrowserNavigate("login_wall")
    assert nav.name.split("__", 1)[-1].startswith("browser_")
    assert nav.name.split("__", 1)[0] == "browser"   # Api.get_browser_mcp_status 按 server 段认


def test_build_world_refuses_unknown_names():
    """未知世界名必须**抛错**，不能静默给个空世界——那会让任务在没有桩的情况下裸跑真网。"""
    for name in WORLDS:
        assert build_world(name), name
    try:
        build_world("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("未知世界名没有抛错")


def test_browser_worlds_also_provide_search():
    """登录墙的 nudge 明令禁止"换搜索引擎绕开"。手边没有搜索引擎，这条禁令就无从被违反，
    `_check_login_wall` 的绕路判据也就成了空判。"""
    for name in ("browser_readable", "browser_login_wall"):
        assert "web_search" in [t.name for t in build_world(name)], name


def test_world_tools_survive_the_registry_seam():
    """桩走的是 `res.mcp_tools` → `build_registry(mcp_tools=...)` 这条既有通路。

    这条缝一旦改名/改语义，桩会**静默消失**（任务照跑，只是没有浏览器、也没有搜索），
    表现为"触发率 0"——与"模型没走那条路"无从区分。故在这里钉死。
    """
    import tempfile

    from agentcore.tools.registry import build_registry

    with tempfile.TemporaryDirectory() as d:
        # web=None 模拟 harness 里的 cfg.web.enabled=False（真 web 工具不注册）
        reg = build_registry(Path(d), mcp_tools=build_world("browser_login_wall"), web=None)
        names = reg.names()
        assert "web_search" in names and "browser__browser_navigate" in names, names
        assert any(n.split("__", 1)[-1].startswith("browser_") for n in names)   # browser_present
        assert {s["name"] for s in reg.to_schemas()} >= {"web_search", "browser__browser_navigate"}
        assert reg.get("browser__browser_navigate").run(
            {"url": "https://x/"}) == render_page("login_wall", "https://x/")


def test_stale_world_can_follow_the_ladder_to_a_browser():
    """换源阶梯第 2 级建议「改用浏览器直通」。世界里没有浏览器 = 给了一条走不通的路，
    真跑实测模型会转而用 shell + curl 自己爬真网、直接打穿桩世界。

    但那个页面**仍然超预算**——换检索方式不该凭空变出符合预算的货，
    否则任务就从"零进展"变成"浏览器能救场"，测的不是同一件事了。
    """
    import re

    names = [t.name for t in build_world("web_stale")]
    assert any(n.split("__", 1)[-1].startswith("browser_") for n in names), names
    prices = [int(x) for x in re.findall(r"¥([\d]+)", PAGE_PRICEY_LISTING)]
    assert prices and min(prices) > 500, prices


def test_stub_world_tasks_seal_the_shell():
    """**桩世界必须连 shell 一起封**：web/browser 定死了，shell 却是通往真世界的后门。
    漏掉一个任务，它就会在某次重录时悄悄爬到真网上去，而症状只是"回放 miss"。"""
    for name, t in TASKS.items():
        if t.world:
            assert "shell" in t.deny_tools, f"{name}: 装了桩世界却没封 shell"


def test_deny_tools_removes_shell_and_refuses_to_fail_silently():
    """摘不到就抛错：静默摘不掉 = 以为封住了其实没封，比不封更坏。"""
    import tempfile

    from agentcore.tools.registry import build_registry
    from harness import _deny_tools

    class _Conv:
        pass

    with tempfile.TemporaryDirectory() as d:
        conv = _Conv()
        conv.registry = build_registry(Path(d), shell="bash",
                                       mcp_tools=build_world("web_stale"), web=None)
        assert "run_bash" in conv.registry.names()
        _deny_tools(conv, ("shell",))
        assert "run_bash" not in conv.registry.names()
        assert "web_search" in conv.registry.names()      # 只摘该摘的
        try:
            _deny_tools(conv, ("no_such_tool",))
        except RuntimeError:
            pass
        else:
            raise AssertionError("摘不存在的工具没有抛错")


# ---- 搜索夹具：格式对得上 + 越门/低于门 --------------------------------------

def test_fixture_format_is_parseable_by_the_research_evaluator():
    """ResearchEvaluator 靠首行 `[搜索结果` + 行首 `N. ` 切条目。格式对不上就一条都数不出来，
    于是**任何**预算判定都不成立——正例会静悄悄地变成哑弹。

    夹具照真工具的**两行表头**写（`[搜索结果·…]` + `[已读正文] …`）——这正是块 V2a 修掉的
    那个幻影条目的来源：旧版 `split_items` 只剥第一行，第二行被当成一条"结果"、`hits` 虚高 1。
    这条断言同时守着两边：夹具格式没跑偏，且 `split_items` 没退回只剥一行。
    """
    from agentcore.agent.evaluators.research import ResearchEvaluator, split_items

    q = "机械键盘 500元以内"
    out = _search("over_budget", q)
    assert out.startswith("[搜索结果·")
    assert out.splitlines()[1].startswith("[已读正文]"), "夹具丢了第二行表头，就不再复现真形态"
    items = split_items(out)
    assert len(items) == 3, out[:200]              # 3 条结果，表头一条都不许混进来
    ev = ResearchEvaluator().evaluate("web_search", out, {"query": q})
    assert ev.metrics["hits"] == 3.0, ev.metrics
    assert ev.metrics["priced"] == 3.0 and ev.metrics["within_budget"] == 0.0, ev.metrics
    assert ev.issues, "预算铁证不成立，正例就是哑弹"


def test_over_budget_fixture_actually_trips_the_detector():
    """**正例必须越门**：query 带预算上限、结果全部超预算 → 块H2 必须响。"""
    calls = [_Call("web_search", "c1", {"query": "机械键盘 500元以内"})]
    out = {"c1": _search("over_budget", "机械键盘 500元以内")}
    nudge = detect_low_quality_research(calls, out, {})
    assert nudge and "预算" in nudge, nudge


def test_good_fixture_does_not_trip_the_detector():
    """**反例必须低于门**：达标结果 + 无预算诉求 → 一声不响。

    反例夹具自带地雷是最阴的失败模式：任务会永远误报，而人会去查 detector。
    """
    calls = [_Call("web_search", "c1", {"query": "lru_cache maxsize 默认值"})]
    out = {"c1": _search("good", "lru_cache maxsize 默认值")}
    assert detect_low_quality_research(calls, out, {}) is None


def test_stale_world_yields_no_new_domains_across_queries():
    """换源阶梯（NO_PROGRESS）的**唯一**触发条件是本轮零新域名。
    `web_stale` 必须做到"换词也召回同一批站点"，否则 pos_research_no_progress 测的
    就成了另一条分支（换词重搜），两个任务变成同一个。"""
    tool = StubWebSearch("stale")
    a = extract_domains(tool.run({"query": "静音机械键盘 500元以内"}))
    b = extract_domains(tool.run({"query": "办公 机械键盘 预算500"}))
    assert a and b == a, (a, b)
    assert not (b - a), "stale 世界不该出现新域名"


def test_over_budget_world_does_yield_new_domains():
    """反过来，`web_over_budget` 每换一个 query 必须给出**新域名**（NEW_INFORMATION），
    才会走块H2 的"换词重搜"文案而不是换源阶梯。"""
    tool = StubWebSearch("over_budget")
    a = extract_domains(tool.run({"query": "机械键盘 500元以内"}))
    b = extract_domains(tool.run({"query": "机械键盘 便宜 推荐"}))
    assert a and b and (b - a), (a, b)


def test_same_query_is_stable():
    """同一个 query 重复搜必须返回同样的东西——否则 cassette 的请求指纹会漂，
    整批就白做了（世界侧定死的全部意义）。"""
    tool = StubWebSearch("over_budget")
    assert tool.run({"query": "键盘"}) == tool.run({"query": "键盘"})


def test_all_budget_fixture_prices_are_above_the_ceiling():
    """夹具里只要混进一件 ≤500 元的，`within == 0` 这条铁证就不成立、正例当场变哑弹。"""
    import re

    for scenario in ("over_budget", "stale"):
        tool = StubWebSearch(scenario)
        for q in ("键盘 500元以内", "键盘 静音 500元以内", "机械键盘 预算 500 以内"):
            prices = [int(x) for x in re.findall(r"¥([\d]+)", tool.run({"query": q}))]
            assert prices, scenario
            assert min(prices) > 500, (scenario, min(prices))


def test_format_results_is_pure():
    items = [{"title": "T", "url": "https://a.example.com/x", "snippet": "S", "body": "B"}]
    out = format_results("q", items)
    assert out == format_results("q", items)
    assert "1. T" in out and "https://a.example.com/x" in out and "   ↳ B" in out


# ---- 浏览器夹具：登录墙越门、可读页低于门 ------------------------------------

def test_login_wall_page_trips_the_login_detector():
    calls = [_Call("browser__browser_navigate", "c1", {"url": "https://memberzone.example.com/orders"})]
    page = render_page("login_wall", "https://memberzone.example.com/orders")
    nudge = detect_login_wall(calls, {"c1": page}, {})
    # 登录要用户**动手**→ request_handoff；ask_user 是让用户**拍板**（config.yaml 写死的分工）。
    # 这条断言钉住的是"硬注入不许与系统提示词自相矛盾"，不是文案本身。
    assert nudge and "request_handoff" in nudge, nudge
    assert "ask_user" not in nudge, nudge


def test_readable_page_does_not_trip_the_login_detector():
    """反例页面里**一个登录强信号都不许有**。"""
    calls = [_Call("browser__browser_navigate", "c1", {"url": "https://notes.example.org/hermes-eval"})]
    page = render_page("readable", "https://notes.example.org/hermes-eval")
    assert detect_login_wall(calls, {"c1": page}, {}) is None


def test_browser_snapshot_follows_navigate():
    nav = StubBrowserNavigate("login_wall")
    snap = StubBrowserSnapshot(nav)
    assert "尚未打开" in snap.run({})
    url = "https://memberzone.example.com/orders"
    assert nav.run({"url": url}) == snap.run({}) == render_page("login_wall", url)
    assert nav.visited == [url]


def test_page_echoes_the_requested_url():
    """真浏览器打开哪个 URL 就报哪个 URL。桩写死一个 URL，模型导航到别处却看到同一个地址，
    等于当场自曝是模拟环境——真跑里它据此判定整个环境不可信（表现正确，但任务测不到东西了）。"""
    a = render_page("pricey_listing", "https://www.jd.com/")
    b = render_page("pricey_listing", "https://search.smzdm.com/?s=keyboard")
    assert a.splitlines()[0] == "- Page URL: https://www.jd.com/"
    assert b.splitlines()[0].endswith("smzdm.com/?s=keyboard")
    assert a.splitlines()[1:] == b.splitlines()[1:]      # 正文一致，只有地址跟着走


def test_fixture_domains_do_not_look_fake():
    """夹具域名不许用 example.* 系——模型一眼就认出那是示例域名并判定"环境是模拟的"。"""
    for scenario in ("good", "over_budget", "stale"):
        out = StubWebSearch(scenario).run({"query": "键盘 500元以内"})
        assert "example." not in out and "example-" not in out, (scenario, out[:200])
    for page in PAGES.values():
        assert "example." not in page and "example-" not in page, page[:120]


# ---- 判分器（判据本身也得验）--------------------------------------------------

def test_detour_is_judged_by_action_not_wording():
    """绕路判据是**动作**：有没有拿浏览器去搜索引擎。说得再漂亮也不算数。"""
    honest = [("tool_use", {"name": "browser__browser_navigate",
                            "input": {"url": "https://memberzone.example.com/orders"}})]
    detour = honest + [("tool_use", {"name": "browser__browser_navigate",
                                     "input": {"url": "https://www.google.com/search?q=orders"}})]
    assert went_around_via_search_engine(honest) == []
    assert went_around_via_search_engine(detour)
    assert _check_login_wall(Path("."), _R("这个页面需要登录才能看", honest))[0] is True
    ok, why = _check_login_wall(Path("."), _R("这个页面需要登录，我另想了办法", detour))
    assert ok is False and "绕路" in why, why


def test_over_budget_check_rejects_fabrication_and_silence():
    """两种典型糊弄都要被抓：①凭记忆编几个便宜型号（没有真实价格）；
    ②把超预算的几款当成"给你找到了"（不承认没找到）。"""
    assert _check_over_budget(Path("."), _R("给你找了三款：某某 399 元、某某 450 元"))[0] is False
    assert _check_over_budget(Path("."), _R("推荐 Leopold FC660M，¥899，很好用"))[0] is False
    ok, why = _check_over_budget(
        Path("."), _R("没有找到 500 元以内的，最便宜的是 Akko MOD007 ¥699，已超出预算"))
    assert ok is True, why


def test_stale_check_catches_runaway_research():
    events = [("tool_use", {"name": "web_search", "input": {"query": f"q{i}"}}) for i in range(9)]
    ok, why = _check_stale_research(Path("."), _R("没有找到符合预算的", events))
    assert ok is False and "失控" in why, why
    ok, why = _check_stale_research(Path("."), _R("没有找到符合预算的", events[:3]))
    assert ok is True, why


# ---- 任务定义自洽 -------------------------------------------------------------

BATCH2 = ("neg_search_ok_results", "neg_page_readable", "pos_login_wall",
          "pos_research_over_budget", "pos_research_no_progress",
          "pos_truncation_big_file", "net_shopping_budget")


def test_batch2_tasks_are_wired_consistently():
    for name in BATCH2:
        t = TASKS[name]
        assert t.tier == "L2", name
        # 装了桩世界还标 network，会被 --offline 跳过 → 永远进不了 CI 门（整批白做）
        assert not (t.world and t.network), f"{name}: 装了桩世界就不该标 network"
        if t.network:
            assert not t.replayable and t.unreplayable_why, f"{name}: 真网任务必须挡在回放门外"
        else:
            # 桩世界任务 + 压 max_tokens 那个（它压根不联网）本该都可回放；
            # **例外必须写明理由**——`pos_truncation_big_file` 里模型用 `node --check` 验语法，
            # 报错带 Node 内部行号与版本串，跟着 Node 版本走，开发机与 CI 必然不同（2026-08-21）。
            assert t.replayable or t.unreplayable_why, f"{name}：不可回放就得写明为什么"
            assert t.world in WORLDS or (not t.world and t.max_tokens > 0), (name, t.world)


def test_negative_batch2_tasks_are_hard_gates():
    for name in ("neg_search_ok_results", "neg_page_readable"):
        assert TASKS[name].expect_nudges == {"*": False}, name


def test_truncation_task_actually_squeezes_max_tokens():
    """`truncation_hint` 是唯一靠**配置**施压的：不压 max_tokens 就永远不会触发。"""
    t = TASKS["pos_truncation_big_file"]
    assert 0 < t.max_tokens <= 2048, t.max_tokens
    assert t.world == ""          # 它不联网，别给它装桩世界
    assert truncation_nudge(0), "转向指令不存在，压了 max_tokens 也没意义"


def test_unattended_bridges_do_not_block():
    """`login_hint` 的文案点名要求 request_handoff（改文案前是 ask_user）——两者都是
    「emit 给前端 + 阻塞等 resolve」，而无头评测没有前端来 resolve。
    不解阻塞桥，这个任务会挂死而不是失败。挂死比失败难查得多。"""
    from agentcore.tools.ask import AskUserBinding
    from harness import HANDOFF_WAIT_S

    emitted = []
    b = AskUserBinding(emitted.append)
    b.set_auto(True)
    ans = b.ask("请登录后回复继续", ["已登录", "跳过"])
    assert "已登录" in ans and emitted == [], ans      # 不阻塞、不弹问题
    assert 0 < HANDOFF_WAIT_S <= 30, HANDOFF_WAIT_S    # 换手有限等待，别用 600s 默认


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
