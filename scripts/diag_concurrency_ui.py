#!/usr/bin/env python3
"""FR-17 并发可观测性的 UI 自检：真实 index.html + app.js，用真事件驱动多会话。

    python scripts/diag_concurrency_ui.py   # 需要 pip install playwright && playwright install chromium

为什么要真渲染而不是只跑 pure.js 单测：v3.62.1 的教训——纯逻辑全对、元素也都存在，
坏的是**布局流**（inline-block 的行尾换行被吞、两行叠在一起）。本 FR 同样给指挥中心的行
加了第二行（副标题），必须**量几何**确认两行不重叠、且 chip 真的看得见。

链路：三个会话分别进 running / awaiting(handoff) / awaiting(permission) →
顶部 chip 文案与配色 → 打开指挥中心 → 行序（等你在前）→ 每行副标题 → 点击直达 →
处理完回到 running 后计数回落。

自带活性：把 `concurrencyRows()` 里的 awaiting 分支去掉（即回到本 FR 之前的行为），
本脚本必须变红——见文末「活性自检」提示。
"""
import asyncio
import pathlib
import sys
import tempfile

from playwright.async_api import async_playwright

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

SESSIONS = [
    {"id": 11, "title": "重构 provider", "pinned": 0, "updated_at": "2026-08-12 10:00"},
    {"id": 22, "title": "查年报", "pinned": 0, "updated_at": "2026-08-12 10:01"},
    {"id": 33, "title": "跑回归", "pinned": 0, "updated_at": "2026-08-12 10:02"},
]

STUB = """
window.__calls = [];
window.pywebview = { api: new Proxy({}, { get: (t, name) => (...args) => {
  window.__calls.push([name, args]);
  if (name === 'list_sessions') return Promise.resolve(window.__sessions);
  if (name === 'set_window_title') { window.__titles = window.__titles || []; window.__titles.push(args[0]); }
  if (name === 'flash_window') { window.__flashes = (window.__flashes || 0) + 1; }
  // 切会话会连带刷工作区：桩要给出**真实形状**（`tree.children`），否则崩在 refreshWorkspace
  // 而不是崩在被测逻辑上——催桩的教训：catch-all 的 {ok:true} 会让"看着成功"的返回值撑爆调用方。
  if (name === 'get_workspace_tree')
    return Promise.resolve({ ok: true, root: '', label: '', tree: { children: [] } });
  return Promise.resolve({ ok: true });
}})};
"""

results = []


def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


async def shown(page, sel):
    """真的看得见吗——不问 `el.hidden` 属性（作者样式的 display 会盖掉它，CLAUDE.md 已知坑）。"""
    return await page.eval_on_selector(
        sel, "e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)")


async def send(page, cid, state, reason=""):
    """走真实的 `window.__onAgentEvent`，不直接改内部变量——测的是接线不是我的假设。"""
    data = {"state": state}
    if reason:
        data["reason"] = reason
    await page.evaluate(
        "([cid, data]) => window.__onAgentEvent({ event: 'state', data, cid })", [cid, data])
    await page.wait_for_timeout(60)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.add_init_script(STUB)
        await page.add_init_script("window.__sessions = " + repr(SESSIONS).replace("'", '"'))
        await page.goto((WEB / "index.html").as_uri())
        await page.wait_for_timeout(400)

        # 三个会话：cid 1/2/3 ↔ sid 11/22/33，其中 1 是当前打开的那个
        await page.evaluate("""() => {
          [[1, 11], [2, 22], [3, 33]].forEach(([cid, sid]) => {
            const v = getView(cid); v.sessionId = sid; sessionIdToCid.set(sid, cid);
          });
          activeCid = 1; activeSessionId = 11; mountView(1);
          lastSessions = window.__sessions;
        }""")

        # ---- 1. 只有运行中：chip 是老样子（不回归既有行为）----
        await send(page, 1, "running")
        check("只有运行中时 chip 文案不变",
              (await page.eval_on_selector("#running-chip", "e => e.textContent")) == "1 运行中")
        check("chip 看得见", await shown(page, "#running-chip"))
        check("没人等你时 chip 不是警告色",
              not await page.eval_on_selector("#running-chip", "e => e.classList.contains('waiting')"))

        # ---- 2. 换手挂起：这是本 FR 的核心场景 ----
        # 会话 2 在后台请求换手。改造前：state 压根不变（仍是 running），全局零信号。
        await send(page, 2, "awaiting", "handoff")
        txt = await page.eval_on_selector("#running-chip", "e => e.textContent")
        check("后台换手挂起 → chip 出现「等你」段且排在前", txt == "✋ 1 等你 · 1 运行中", txt)
        check("有人等你 → chip 转警告色",
              await page.eval_on_selector("#running-chip", "e => e.classList.contains('waiting')"))

        # ---- 3. 第三个会话等权限：两种等待累加 ----
        await send(page, 3, "awaiting", "permission")
        txt = await page.eval_on_selector("#running-chip", "e => e.textContent")
        check("两种等待累加进同一个计数", txt == "✋ 2 等你 · 1 运行中", txt)

        # ---- 4. 指挥中心：行序与副标题 ----
        await page.click("#running-chip")
        await page.wait_for_timeout(150)
        check("指挥中心弹层可见", await shown(page, "#cc-popover"))
        names = await page.eval_on_selector_all(
            "#cc-popover .cc-title", "els => els.map(e => e.textContent.trim())")
        check("等你的会话排在运行中的前面",
              names[:2] == ["查年报", "跑回归"] and names[2].startswith("重构 provider"), str(names))
        subs = await page.eval_on_selector_all(
            "#cc-popover .cc-sub", "els => els.map(e => e.textContent.trim())")
        check("副标题分别报出「等什么」", [s for s in subs if s] == ["等接管", "等确认"], str(subs))
        # `.cc-sub` 恒定渲染（给就地更新留落点），空的必须被 CSS `:empty` 收起来、不占行高
        check("没内容的副标题不可见（不留空行）",
              await page.eval_on_selector_all(
                  "#cc-popover .cc-sub",
                  """els => els.every(e => e.textContent.trim()
                       ? true : !(e.offsetWidth || e.offsetHeight || e.getClientRects().length))"""))
        check("等待行有 cc-wait 标记（配色靠它）",
              await page.eval_on_selector_all(
                  "#cc-popover .cc-row.cc-wait", "els => els.length") == 2)

        # ---- 5. 几何：两行不重叠（v3.62.1 那类布局 bug 只有量出来才知道）----
        # 取不到元素时**报红而不是抛异常**：修复被退回时这里首当其冲，脚本崩掉的话
        # 后面几组根本跑不到，看不出坏在哪（v3.63 记过同类：自检自己要"坏了会红、不会卡"）。
        boxes = await page.evaluate(
            """() => { const r = document.querySelector('#cc-popover .cc-row.cc-wait');
                       if (!r) return null;
                       const t = r.querySelector('.cc-title'), s = r.querySelector('.cc-sub');
                       if (!t || !s) return null;
                       const tb = t.getBoundingClientRect(), sb = s.getBoundingClientRect();
                       return { tb: tb.bottom, st: sb.top, sh: sb.height,
                                rh: r.getBoundingClientRect().height }; }""")
        check("标题与副标题上下分行、不重叠",
              boxes and boxes["st"] >= boxes["tb"] - 1 and boxes["sh"] > 0, str(boxes))
        check("行高容得下两行（没被压扁）",
              boxes and boxes["rh"] >= boxes["sh"] * 2, str(boxes))
        rows_overlap = await page.eval_on_selector_all(
            "#cc-popover .cc-row",
            """els => { const b = els.map(e => e.getBoundingClientRect());
                        for (let i = 1; i < b.length; i++) if (b[i].top < b[i-1].bottom - 1) return true;
                        return false; }""")
        check("相邻两行之间也不重叠", not rows_overlap)

        # ---- 5b. 忙碌会话仍然点得动（真机 bug 回归，2026-08-12）----
        # 弹层开着时事件一直在来；旧实现每次都重写整块 innerHTML，把用户正按着的那一行
        # 从文档里换掉 → mousedown/mouseup 落在不同节点 → click 不触发。
        # 症状是"停在等待中的会话里，点另一个跑着的会话切不过去"——**越忙越点不动**。
        # 这里用真鼠标序列复现：按下 → 期间灌一串 tool_use → 抬起。
        target = await page.query_selector('#cc-popover .cc-row[data-sid="11"] .cc-name')
        if target is None:
            check("忙碌会话在事件流中仍可点击切换", False, "取不到目标行")
        else:
            box = await target.bounding_box()
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.mouse.down()
            for i in range(5):
                await page.evaluate(
                    """(i) => window.__onAgentEvent({ event: 'tool_use', cid: 1,
                         data: { id: 't' + i, name: 'read_file', input: { path: 'f' + i + '.py' } } })""", i)
            await page.wait_for_timeout(50)
            await page.mouse.up()
            await page.wait_for_timeout(250)
            check("按住期间来事件，节点不被换掉（结构没变就不重建 DOM）",
                  await page.evaluate("e => e.isConnected", target))
            check("忙碌会话在事件流中仍可点击切换",
                  await page.evaluate("() => activeSessionId") == 11,
                  str(await page.evaluate("() => activeSessionId")))
            # 切回来，后面的用例接着用会话 11 之外的当前态
            await page.evaluate("() => selectSession(22)")
            await page.wait_for_timeout(250)
            await page.click("#running-chip")
            await page.wait_for_timeout(150)

        # ---- 6. 点击直达：等你的那条要能一键切过去 ----
        clicked = await page.evaluate(
            """() => { const n = document.querySelector('#cc-popover .cc-row.cc-wait .cc-name');
                       if (!n) return false; n.click(); return true; }""")
        await page.wait_for_timeout(250)
        check("点等待行 → 切到该会话",
              clicked and await page.evaluate("() => activeSessionId") == 22,
              str(await page.evaluate("() => activeSessionId")))

        # ---- 7. 处理完：计数回落，警告色撤掉 ----
        await send(page, 2, "running")
        await send(page, 3, "running")
        txt = await page.eval_on_selector("#running-chip", "e => e.textContent")
        check("三个都在跑 → 回到纯运行中文案", txt == "3 运行中", txt)
        check("没人等你 → 警告色撤掉",
              not await page.eval_on_selector("#running-chip", "e => e.classList.contains('waiting')"))

        # ---- 8. T2「在干什么」：工具事件要落进指挥中心的副标题 ----
        await page.evaluate(
            """() => window.__onAgentEvent({ event: 'tool_use', cid: 3,
                 data: { id: 't1', name: 'run_bash', input: { command: 'pytest -q' } } })""")
        await page.wait_for_timeout(120)
        await page.click("#running-chip")
        await page.wait_for_timeout(150)
        subs = await page.eval_on_selector_all(
            "#cc-popover .cc-sub", "els => els.map(e => e.textContent.trim())")
        check("运行中的会话副标题报出当前工具", "run_bash pytest -q" in subs, str(subs))

        # 等待态压过活动：这行是催人的，不是报进度的
        await send(page, 3, "awaiting", "handoff")
        await page.wait_for_timeout(120)
        subs = await page.eval_on_selector_all(
            "#cc-popover .cc-sub", "els => els.map(e => e.textContent.trim())")
        check("同一会话进入等待后，副标题改报「等接管」而非工具名",
              "等接管" in subs and "run_bash pytest -q" not in subs, str(subs))
        await send(page, 3, "running")

        # ---- 9. T3 标题角标：真的过桥了、且文案对 ----
        titles = await page.evaluate("() => window.__titles || []")
        check("等待期间把「等你」写进了系统标题",
              any("等你" in t for t in titles), str(titles[-3:]))
        check("标题角标只在变化时过桥（没有连续重复项）",
              all(titles[i] != titles[i - 1] for i in range(1, len(titles))), str(titles))

        # ---- 10. 全部收工：chip 隐藏、标题回落干净 ----
        for cid in (1, 2, 3):
            await send(page, cid, "idle")
        check("都空闲 → chip 真的不可见", await shown(page, "#running-chip") is False)
        # 会话 3 在后台出过工具输出 → markActivity 给它打了未读；都停下后角标应报「1 完成」。
        titles = await page.evaluate("() => window.__titles || []")
        check("跑完没看的会话 → 标题报「完成」", titles[-1] == "(1 完成) Hermes", str(titles[-3:]))
        # 切过去看一眼 = 读了 → 角标必须消失（否则就成了永远撤不掉的红点）
        await page.evaluate("() => selectSession(33)")
        await page.wait_for_timeout(300)
        await page.evaluate("() => { getView(3).unread = false; updateSessionRow(getView(3)); }")
        await page.wait_for_timeout(150)
        titles = await page.evaluate("() => window.__titles || []")
        check("看过之后 → 标题回落成干净的 Hermes", titles[-1] == "Hermes", str(titles[-3:]))

        # ---- 11. T3 后台终态提醒：看着窗口 → toast；没看着 → 闪任务栏 ----
        # 真机反馈"根本没注意到 toast"——因为 T3 覆盖的正是你没盯着窗口的时候。
        await page.evaluate("() => { window.__flashes = 0; document.hasFocus = () => true; }")
        await page.evaluate(
            "() => window.__onAgentEvent({ event: 'done', data: {}, cid: 1 })")
        await page.wait_for_timeout(150)
        check("窗口有焦点 → 走应用内 toast，不闪任务栏",
              await page.evaluate("() => window.__flashes") == 0
              and await shown(page, "#toast"))

        await page.evaluate("() => { document.hasFocus = () => false; }")
        # 注意挑一个**非当前**会话：提醒只对后台会话发（当前会话你正看着，不用提醒）
        await page.evaluate(
            "() => window.__onAgentEvent({ event: 'done', data: {}, cid: 2 })")
        await page.wait_for_timeout(150)
        check("窗口没焦点 → 闪任务栏",
              await page.evaluate("() => window.__flashes") == 1,
              str(await page.evaluate("() => window.__flashes")))

        # 当前会话跑完不该打扰：既不闪也不弹（你就看着它）
        await page.evaluate(
            "() => window.__onAgentEvent({ event: 'done', data: {}, cid: 3 })")
        await page.wait_for_timeout(150)
        check("当前会话跑完不闪任务栏（你正看着它）",
              await page.evaluate("() => window.__flashes") == 1,
              str(await page.evaluate("() => window.__flashes")))

        check("无 JS 报错", not errors, str(errors[:2]))
        await page.screenshot(
            path=str(pathlib.Path(tempfile.gettempdir()) / "diag_concurrency_ui.png"), full_page=True)
        await b.close()

    bad = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    if bad:
        print("活性自检：把 app.js `concurrencyRows()` 里的 awaiting 分支删掉（回到本 FR 之前的行为），"
              "第 2~8 组应当变红——若仍全绿说明这脚本没在测真东西。")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
