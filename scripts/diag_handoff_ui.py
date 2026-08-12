#!/usr/bin/env python3
"""换手面板（ADR 0023 决策 1~3）的 UI 自检：真实 index.html + app.js 驱动事件、点按钮、量几何。

    python scripts/diag_handoff_ui.py     # 需要 pip install playwright && playwright install chromium

**不是打桩测纯逻辑**：事件走真实 `__onAgentEvent` 路由 → 真实 `renderHandoff` 渲染 → 真的点按钮，
只把 pywebview 桥换成记录型 stub（真机上那一端是 Python）。

为什么要这一层（v3.62.1 的教训）：纯逻辑单测 + "元素在不在"的打桩**测不出布局**。
这里额外量两件事：换手卡片不会被长 URL 撑出横向溢出；面板上那两条安全信息（真实目标 + 凭据边界）
**真的可见**，不是藏在 DOM 里的死字符串。
"""
import asyncio
import pathlib
import sys
import tempfile

from playwright.async_api import async_playwright

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

STUB = """
window.__calls = [];
window.__browser = { enabled: true, headed: false };   // 默认造"无头"态：换手最该提醒的那种
window.pywebview = { api: new Proxy({}, { get: (t, name) => (...args) => {
  window.__calls.push([name, args]);
  if (name === 'browser_handoff_status') return Promise.resolve(window.__browser);
  if (name === 'handoff_open_target') { window.__browser = { enabled: true, headed: true };
                                        return Promise.resolve({ ok: true, switched: true }); }
  if (name === 'list_sessions') return Promise.resolve([]);
  return Promise.resolve({ok:true});
}})};
"""

LONG_URL = "https://sso.example.com/authorize?client_id=" + "a" * 300 + "&redirect_uri=/cb"

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


async def emit(page, data):
    await page.evaluate(
        "d => window.__onAgentEvent({event: 'handoff_request', data: d, cid: 1})", data)
    await page.wait_for_timeout(80)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.add_init_script(STUB)
        await page.goto((WEB / "index.html").as_uri())
        await page.wait_for_timeout(400)
        await page.evaluate("() => { activeCid = 1; mountView(1); }")

        # ---- 1. 有人值守：面板该显示的都显示 ----
        await emit(page, {"id": 7, "reason": "这站要短信验证码，只能你来收",
                          "target": "https://bank.example.com/login",
                          "verify": "重新 snapshot 看页面是否出现账户名", "unattended": False})
        await page.wait_for_selector(".handoff-bar")
        vis = await page.eval_on_selector_all(
            ".handoff-bar > *", "els => els.filter(e => e.offsetHeight > 0).map(e => e.textContent)")
        joined = " ".join(vis)
        check("换手理由可见", "这站要短信验证码" in joined, joined[:120])
        check("**真实目标**可见（换手是钓鱼位，来源必须常驻）",
              "https://bank.example.com/login" in joined, joined[:160])
        check("目标类型标成「网址」", "网址" in joined)
        check("verify 可见（人能看出 agent 打算怎么验）",
              "重新 snapshot 看页面是否出现账户名" in joined)
        check("**凭据边界声明**可见", "hermes 不读取、不回传" in joined, joined[:200])
        check("有人值守时不显示无人值守提示", "阻塞：待人工换手" not in joined)
        n_note = await page.eval_on_selector_all(".handoff-note", "els => els.length")
        check("有补充输入框", n_note == 1)

        # ---- 2. 点「我做完了」→ 带上补充回给 Python，且按钮锁死（防重复 resolve）----
        await page.fill(".handoff-note", "登录了")
        await page.click(".handoff-done")
        await page.wait_for_timeout(80)
        calls = await page.evaluate("window.__calls.filter(c => c[0] === 'resolve_handoff')")
        check("点完成 → resolve_handoff(id, 'done', note, cid)",
              calls == [["resolve_handoff", [7, "done", "登录了", 1]]], str(calls))
        head = await page.eval_on_selector(".handoff-q", "e => e.textContent")
        check("面板上留下「→ 我做完了」的痕迹", head.strip().endswith("→ 我做完了"), head)
        dis = await page.eval_on_selector_all(
            ".handoff-bar button, .handoff-bar input", "els => els.every(e => e.disabled)")
        check("按钮与输入框已锁死（不能重复回答）", dis)
        await page.click(".handoff-done", force=True)
        await page.wait_for_timeout(60)
        n = await page.evaluate("window.__calls.filter(c => c[0] === 'resolve_handoff').length")
        check("锁死后再点：不会再发一次", n == 1, f"{n} 次")

        # ---- 3. 无人值守 + 「做不了，跳过」+ 超长 URL 不撑破卡片 ----
        await emit(page, {"id": 8, "reason": "需要企业 SSO 登录", "target": LONG_URL,
                          "verify": "看是否跳回回调页", "unattended": True})
        bars = await page.eval_on_selector_all(".handoff-bar", "els => els.length")
        check("第二次换手渲染成新的一张卡", bars == 2, f"{bars} 张")
        hint = await page.eval_on_selector_all(
            ".handoff-hint", "els => els.map(e => e.textContent).join(' ')")
        check("无人值守：明说会收成「阻塞：待人工换手」、不算完成",
              "阻塞：待人工换手" in hint and "不会被记成完成" in hint, hint)
        # 长 URL：卡片自身不横向溢出（overflow-wrap 生效），且 URL 原样在 DOM 里
        geo = await page.evaluate("""() => {
          const bar = document.querySelectorAll('.handoff-bar')[1];
          const code = bar.querySelector('.handoff-target code');
          return {barW: bar.clientWidth, barSW: bar.scrollWidth,
                  codeR: Math.round(code.getBoundingClientRect().right),
                  barR: Math.round(bar.getBoundingClientRect().right),
                  text: code.textContent};
        }""")
        check("超长 URL 不把卡片撑出横向滚动", geo["barSW"] <= geo["barW"] + 1,
              f"scrollWidth={geo['barSW']} clientWidth={geo['barW']}")
        check("超长 URL 不溢出卡片右边界", geo["codeR"] <= geo["barR"] + 1,
              f"code.right={geo['codeR']} bar.right={geo['barR']}")
        check("URL 原样显示、不截断（用户要据它判断该不该登）", geo["text"] == LONG_URL)
        await page.eval_on_selector_all(
            ".handoff-bar .handoff-skip", "els => els[els.length - 1].click()")
        await page.wait_for_timeout(80)
        calls = await page.evaluate("window.__calls.filter(c => c[0] === 'resolve_handoff')")
        check("点「做不了，跳过」→ outcome='skipped'（空补充也照发）",
              len(calls) == 2 and calls[1][1] == [8, "skipped", "", 1], str(calls[-1:]))

        # ---- 3b. 网页目标 + 无头浏览器：人根本没地方登录，必须说清并给一键切换 ----
        # （真机指出的设计漏洞：用户在自己日常 Chrome 里登录，hermes 那个独立 profile 看不见）
        await emit(page, {"id": 21, "reason": "要登录才看得到", "target": "https://ir.example.com/private",
                          "verify": "重开目标页 snapshot 看是否已登录", "unattended": False})
        row = await page.eval_on_selector_all(
            ".handoff-browser", "els => els[els.length-1].textContent")
        check("无头时明说「你在自己 Chrome 里登录不算数」", "不算数" in row, row)
        n_sw = await page.evaluate(
            "() => { const b = document.querySelectorAll('.handoff-bar');"
            "  return b[b.length-1].querySelectorAll('.handoff-switch').length; }")
        check("给了「切到有头并打开这页」按钮", n_sw == 1, f"{n_sw} 个")
        await page.eval_on_selector_all(".handoff-switch", "els => els[els.length-1].click()")
        await page.wait_for_timeout(150)
        called = await page.evaluate(
            "window.__calls.filter(c => c[0] === 'handoff_open_target')")
        check("点了 → handoff_open_target(真实 URL)",
              called == [["handoff_open_target", ["https://ir.example.com/private"]]], str(called))
        row = await page.eval_on_selector_all(
            ".handoff-browser", "els => els[els.length-1].textContent")
        check("切换后改口：让人去弹出的那个窗口登录", "弹出的那个浏览器窗口" in row, row)

        # 已经是有头时：不给切换按钮，但仍要点明别在日常 Chrome 里登
        await emit(page, {"id": 22, "reason": "要登录", "target": "https://ir.example.com/private2",
                          "verify": "看页面", "unattended": False})
        row = await page.eval_on_selector_all(
            ".handoff-browser", "els => els[els.length-1].textContent")
        check("有头时提示改成「在弹出的窗口里登录，不是你平时用的 Chrome」",
              "不是你平时用的 Chrome" in row, row)

        # 本地路径目标：与浏览器无关，别冒这行提示
        await emit(page, {"id": 23, "reason": "把数据放进来", "target": "report.txt",
                          "verify": "重读文件", "unattended": False})
        n_rows = await page.evaluate(
            "() => document.querySelectorAll('.handoff-bar')[document.querySelectorAll('.handoff-bar').length-1]"
            ".querySelectorAll('.handoff-browser').length")
        check("本地路径目标不提浏览器的事", n_rows == 0, f"{n_rows} 行")

        # ---- 4. 目标里的 HTML 不当标记解析（换手请求可能来自不可信技能）----
        await emit(page, {"id": 9, "reason": "登录", "target": '<img src=x onerror="window.__xss=1">',
                          "verify": "看页面", "unattended": False})
        xss = await page.evaluate("() => window.__xss || false")
        txt = await page.eval_on_selector_all(
            ".handoff-target code", "els => els[els.length-1].textContent")
        n_img = await page.eval_on_selector_all(".handoff-bar img", "els => els.length")
        check("目标里的 HTML 被转义（不执行、不插元素）",
              xss is False and n_img == 0 and txt == '<img src=x onerror="window.__xss=1">', txt)

        check("无 JS 报错", not errors, str(errors[:2]))
        await page.screenshot(
            path=str(pathlib.Path(tempfile.gettempdir()) / "diag_handoff_ui.png"), full_page=True)
        await b.close()

    bad = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
