#!/usr/bin/env python3
"""轨迹录制与固化（ADR 0023 决策 4~8）的 UI 自检：真实 index.html + app.js 全流程走一遍。

    python scripts/diag_trace_ui.py       # 需要 pip install playwright && playwright install chromium

链路：点轨迹按钮 → 状态条出现 → 记一步 → 停止 → 固化面板（勾步骤/改变量名/选范围）→
「生成技能」→ 检查真的走了 `trajectory_compose` 且提示词以**正常消息**发出去（用户看得见、能撤）。
pywebview 桥换成记录型 stub，**后端返回的形状照抄真实 `trajectory_*` 的返回值**。

顺带守住 ADR 0023 决策 4 的一条改动：composer 上的 `#review-btn` 已撤、原位是 `#trace-btn`，
评审入口收敛到每条回复下的「🔬 评审这段」。
"""
import asyncio
import json
import pathlib
import sys
import tempfile

from playwright.async_api import async_playwright

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

# 真实 trajectory_stop() 的返回形状（字段名与 trajectory.py 的 as_dict/param_candidates 一致）
STOP_RESULT = {
    "ok": True, "goal": "查公司年报", "seconds": 42, "truncated": False,
    "steps": [
        {"kind": "tool", "at": 1.0, "label": "web_search(query=示例公司 2025 年报)",
         "tool": "web_search", "detail": "", "count": 1, "ok": True},
        {"kind": "note", "at": 8.0, "label": "这个站的年报比二手媒体准",
         "tool": "", "detail": "https://ir.example.com/2025", "count": 1, "ok": True},
        {"kind": "tool", "at": 20.0, "label": "web_fetch(url=https://news.example.com/x)",
         "tool": "web_fetch", "detail": "", "count": 1, "ok": False},
        {"kind": "say", "at": 30.0, "label": "不对，只用一手年报", "tool": "",
         "detail": "", "count": 1, "ok": True},
    ],
    "params": [
        {"value": "https://ir.example.com/2025", "kind": "url", "name": "{{网址}}", "occurrences": 2},
        {"value": "2026-03-31", "kind": "date", "name": "{{日期}}", "occurrences": 1},
    ],
}

STUB = """
window.__calls = [];
window.__sent = [];
window.__rec = { recording: false, steps: 0, seconds: 0, full: false };
window.pywebview = { api: new Proxy({}, { get: (t, name) => (...args) => {
  window.__calls.push([name, args]);
  if (name === 'trajectory_state') return Promise.resolve(window.__rec);
  if (name === 'trajectory_start') {
    window.__rec = { recording: true, steps: 0, seconds: 0, full: false, goal: args[0] };
    return Promise.resolve({ ok: true, ...window.__rec });
  }
  if (name === 'trajectory_mark') {
    window.__rec = { ...window.__rec, steps: window.__rec.steps + 1, seconds: 42 };
    return Promise.resolve({ ok: true, ...window.__rec });
  }
  if (name === 'trajectory_stop') {
    window.__rec = { recording: false, steps: 0, seconds: 0, full: false };
    return Promise.resolve(window.__stop);
  }
  if (name === 'trajectory_discard') {
    window.__rec = { recording: false, steps: 0, seconds: 0, full: false };
    return Promise.resolve({ ok: true, ...window.__rec });
  }
  if (name === 'trajectory_compose') {
    window.__composed = args[0];
    return Promise.resolve({ ok: true, prompt: '（使用 `skill-creator` 技能）……' });
  }
  if (name === 'send_message') { window.__sent.push(args); return Promise.resolve({ ok: true }); }
  if (name === 'list_sessions') return Promise.resolve([]);
  return Promise.resolve({ ok: true });
}})};
"""

results = []


async def shown(page, sel):
    """**真的看得见吗**——不是问 `el.hidden` 属性。

    v3.62.1 的教训在这条上又踩了一次：作者样式里的 `display:` 会盖掉 UA 的 `[hidden]{display:none}`，
    于是 `hidden` 属性是 true、元素却照常显示。断言属性一路全绿，用户一开 app 就看见
    "正在录制轨迹"常驻。凡是 hidden 控制显隐的元素，一律量渲染结果。
    """
    return await page.eval_on_selector(
        sel, "e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)")


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.add_init_script(STUB)
        await page.add_init_script("window.__stop = " + json.dumps(STOP_RESULT))
        await page.goto((WEB / "index.html").as_uri())
        await page.wait_for_timeout(400)
        await page.evaluate("() => { activeCid = 1; mountView(1); }")

        # ---- 0. 决策 4 的按钮换位：composer 上没有评审按钮了，有轨迹按钮 ----
        check("composer 上的 #review-btn 已撤",
              await page.eval_on_selector_all("#review-btn", "els => els.length") == 0)
        check("原位是 #trace-btn（轨迹）",
              await page.eval_on_selector_all("#trace-btn", "els => els.length") == 1)
        check("撤按钮没把评审撤掉：消息级入口与快捷键兜底都还在",
              await page.evaluate(
                  "() => typeof startReviewOn === 'function' && typeof startReviewFallback === 'function'"))
        check("录制前状态条不显示（量渲染，不是问 hidden 属性）",
              await shown(page, "#trace-bar") is False)

        # ---- 1. 开始录制：状态条常驻，按钮点亮 ----
        # 真机上输入框由启动流程解禁；这里只是造个初始状态（stub 环境没跑那段）
        await page.evaluate("() => { input.disabled = false; input.value = '把示例公司年报整理成表'; }")
        await page.click("#trace-btn")
        await page.wait_for_timeout(150)
        started = await page.evaluate("window.__calls.filter(c => c[0] === 'trajectory_start')")
        check("点轨迹按钮 → trajectory_start，并把输入框里的话当作目标",
              len(started) == 1 and started[0][1] == ["把示例公司年报整理成表"], str(started))
        check("状态条出现", await shown(page, "#trace-bar") is True)
        check("轨迹按钮点亮（一眼看得出在录）",
              "active" in await page.eval_on_selector("#trace-btn", "e => e.className"))
        txt = await page.eval_on_selector("#trace-status", "e => e.textContent")
        check("状态条文案含步数与时长", "已录 0 步" in txt and "00:00" in txt, txt)

        # ---- 2. 记一步：应用内询问弹窗（**居中**）+ 带上人写的那句意图 ----
        # 原生 prompt 在 WebView2 里贴着窗口最上沿弹（真机反馈），这里量的就是"真的居中了"。
        await page.click("#trace-mark")
        await page.wait_for_timeout(120)
        geo = await page.evaluate("""() => {
          const m = document.querySelector('.ask-modal');
          if (!m || !m.offsetHeight) return null;
          const r = m.getBoundingClientRect();
          return {dy: Math.abs((r.top + r.height / 2) - innerHeight / 2),
                  dx: Math.abs((r.left + r.width / 2) - innerWidth / 2), top: Math.round(r.top)};
        }""")
        check("记一步弹的是应用内弹窗（不是原生 prompt）", geo is not None)
        check("弹窗竖直居中（不再贴窗口最上沿）", geo and geo["dy"] <= 40, str(geo))
        check("弹窗水平居中", geo and geo["dx"] <= 10, str(geo))
        await page.fill("#ask-input", "这个站的年报比二手媒体准")
        await page.click("#ask-ok")
        await page.wait_for_timeout(150)
        marks = await page.evaluate("window.__calls.filter(c => c[0] === 'trajectory_mark')")
        check("记一步 → trajectory_mark(说明)",
              marks == [["trajectory_mark", ["这个站的年报比二手媒体准"]]], str(marks))
        check("确定后弹窗关闭",
              await page.eval_on_selector_all(".ask-modal", "els => els.every(e => !e.offsetHeight)"))
        txt = await page.eval_on_selector("#trace-status", "e => e.textContent")
        check("状态条随打点更新（已录 1 步 · 00:42）", "已录 1 步" in txt and "00:42" in txt, txt)

        # Esc 取消：不打点（原生 prompt 时代这条也在，别退化）
        await page.click("#trace-mark")
        await page.wait_for_timeout(120)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)
        n = await page.evaluate("window.__calls.filter(c => c[0] === 'trajectory_mark').length")
        check("弹窗里按 Esc 取消：不打点", n == 1, f"{n} 次")

        # ---- 2b. 浮层叠加：弹窗开在设置面板之上时，Esc 只关**最上层** ----
        # （真机踩到：弹窗没入浮层栈 → 栈的捕获处理器把设置面板关了，弹窗还杵在那儿）
        await page.evaluate("() => openSettings()")
        await page.wait_for_timeout(200)
        await page.evaluate("() => { window.__askP = askInput('测试一下', ''); }")
        await page.wait_for_timeout(120)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
        st = await page.evaluate("""() => ({
          ask: !!document.querySelector('.ask-modal') && !!document.querySelector('.ask-modal').offsetHeight,
          settings: !document.getElementById('settings-overlay').hidden })""")
        check("Esc 关掉的是弹窗，不是它下面的设置面板",
              st["ask"] is False and st["settings"] is True, str(st))
        # 注意别裸 await 那个 Promise：万一没修（弹窗不关），它永远不兑现，脚本会挂死而不是报红
        check("弹窗取消后 Promise 兑现为 null",
              await page.evaluate(
                  "async () => await Promise.race(["
                  "  window.__askP.then(v => v === null),"
                  "  new Promise(r => setTimeout(() => r('timeout'), 1000))]) === true"))
        await page.keyboard.press("Escape")           # 再按一次才轮到设置面板
        await page.wait_for_timeout(150)
        check("再按 Esc 才关设置面板",
              await page.eval_on_selector("#settings-overlay", "e => e.hidden") is True)

        # ---- 3. 停止 → 固化面板（人过一遍才落盘）----
        await page.click("#trace-stop")
        await page.wait_for_timeout(200)
        check("固化面板打开", await shown(page, "#trace-overlay") is True)
        check("停止后状态条不再显示", await shown(page, "#trace-bar") is False)
        steps = await page.eval_on_selector_all(".tr-step", "els => els.map(e => e.textContent)")
        check("四步都列出来了（含旁白与打点）", len(steps) == 4, str(len(steps)))
        joined = " ".join(steps)
        check("旁白/纠正在列表里（意图推不出来，只能听见）", "不对，只用一手年报" in joined)
        check("打点带现场 URL", "https://ir.example.com/2025" in joined)
        bad = await page.eval_on_selector_all(".tr-step.tr-bad", "els => els.length")
        check("失败的那步标出来（试错也是经验，留不留人自己定）", bad == 1, f"{bad} 条")
        vars_ = await page.eval_on_selector_all(".tr-var", "els => els.map(e => e.value)")
        check("参数化候选可编辑", vars_ == ["{{网址}}", "{{日期}}"], str(vars_))

        # ---- 3b. 面板**布局**（真机第一轮：蹭 .settings-body 的类被它的 display:flex 盖掉，
        #         整块被压成一列逐字换行的竖排字。纯功能断言全绿，得量几何才看得见）----
        geo = await page.evaluate("""() => {
          const modal = document.querySelector('.trace-modal');
          const body = document.querySelector('.trace-body');
          const name = document.getElementById('trace-name');
          const step = document.querySelector('.tr-step');
          return {inputW: Math.round(name.getBoundingClientRect().width),
                  stepH: Math.round(step.getBoundingClientRect().height),
                  bodyOver: body.scrollWidth - body.clientWidth,
                  slack: modal.clientHeight - (body.scrollHeight + 120)};
        }""")
        check("技能名输入框有正常宽度（没被压成窄条）", geo["inputW"] >= 240, f"{geo['inputW']}px")
        check("步骤单行高度正常（没有逐字换行）", geo["stepH"] <= 60, f"{geo['stepH']}px")
        check("面板不横向溢出", geo["bodyOver"] <= 1, f"溢出 {geo['bodyOver']}px")
        check("面板高度贴合内容（底部没空一大块）", geo["slack"] <= 80, f"富余 {geo['slack']}px")

        # 固化面板也在浮层栈里：Esc 该能关掉它（同上，别再漏接一次）
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
        check("固化面板按 Esc 可关闭", await shown(page, "#trace-overlay") is False)
        await page.click("#trace-btn"); await page.wait_for_timeout(120)
        await page.click("#trace-stop"); await page.wait_for_timeout(200)   # 重新打开继续后面的用例
        check("重新停止后固化面板又开着", await shown(page, "#trace-overlay") is True)

        # ---- 4. 校验：一步不留 / 一个变量不留都要拦 ----
        await page.eval_on_selector_all(
            "[data-tr-param]", "els => els.forEach(e => { e.checked = false; e.dispatchEvent(new Event('change', {bubbles:true})); })")
        await page.wait_for_timeout(100)
        warn = await page.eval_on_selector("#trace-warn", "e => e.hidden ? '' : e.textContent")
        check("一个变量都不留 → 提示这只是流水账", "流水账" in warn, warn)
        await page.eval_on_selector_all(
            "[data-tr-param]", "els => els.forEach(e => { e.checked = true; e.dispatchEvent(new Event('change', {bubbles:true})); })")
        await page.wait_for_timeout(100)
        check("勾回来 → 提示消失",
              await page.eval_on_selector("#trace-warn", "e => e.hidden") is True)

        # ---- 5. 人改草案：勾掉一步、改变量名、改名字与范围 ----
        await page.eval_on_selector(
            "[data-tr-step='2']", "e => { e.checked = false; e.dispatchEvent(new Event('change', {bubbles:true})); }")
        await page.fill("[data-tr-name='0']", "{{年报页}}")
        await page.eval_on_selector(
            "[data-tr-name='0']", "e => e.dispatchEvent(new Event('change', {bubbles:true}))")
        await page.fill("#trace-name", "annual-report")
        await page.fill("#trace-desc", "查一家公司的年报并整理成表")
        await page.select_option("#trace-scope", "global")
        await page.wait_for_timeout(120)

        # ---- 6. 生成技能：入参剔掉勾掉项，且提示词走**正常发消息**路径 ----
        await page.click("#trace-make")
        await page.wait_for_timeout(250)
        composed = await page.evaluate("window.__composed")
        check("勾掉的步骤没进入参", len(composed["steps"]) == 3
              and all("news.example.com" not in s["label"] for s in composed["steps"]),
              str(len(composed["steps"])))
        check("改过的变量名进了入参",
              composed["params"][0]["name"] == "{{年报页}}", str(composed["params"]))
        check("技能名/描述/范围都带上了",
              composed["skill_name"] == "annual-report" and composed["scope"] == "global"
              and composed["description"] == "查一家公司的年报并整理成表", str(composed)[:160])
        check("目标（录制时输入框那句）带上了", composed["goal"] == "查公司年报", composed["goal"])
        sent = await page.evaluate("window.__sent")
        check("提示词以正常消息发出（用户看得见、能改能撤）",
              len(sent) == 1 and "skill-creator" in sent[0][0], str(sent)[:120])
        bubbles = await page.eval_on_selector_all(".msg.user .bubble", "els => els.length")
        check("对话流里真出现了这条消息（不是暗箱调用）", bubbles >= 1, f"{bubbles} 条")
        check("面板已关闭", await shown(page, "#trace-overlay") is False)

        # ---- 7. 丢弃：不留档 ----
        await page.click("#trace-btn")
        await page.wait_for_timeout(150)
        await page.click("#trace-drop")
        await page.wait_for_timeout(150)
        check("丢弃 → trajectory_discard 且状态条不再显示",
              await page.evaluate("window.__calls.some(c => c[0] === 'trajectory_discard')")
              and await shown(page, "#trace-bar") is False)

        # ---- 8. 空轨迹不该弹面板 ----
        await page.evaluate("() => { window.__stop = { ok: true, goal: '', steps: [], params: [] }; }")
        await page.click("#trace-btn")
        await page.wait_for_timeout(120)
        await page.click("#trace-stop")
        await page.wait_for_timeout(200)
        check("没录到步骤时不弹固化面板", await shown(page, "#trace-overlay") is False)

        check("无 JS 报错", not errors, str(errors[:2]))
        await page.screenshot(
            path=str(pathlib.Path(tempfile.gettempdir()) / "diag_trace_ui.png"), full_page=True)
        await b.close()

    bad = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
