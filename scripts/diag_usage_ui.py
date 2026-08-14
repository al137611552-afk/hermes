"""用量面板 UI 自检（ADR 0025 P3）：Chromium 真渲染 + **量对比度和几何**。

    python scripts/diag_usage_ui.py     # 需 pip install playwright && playwright install chromium

**为什么要量而不是看**：初版把卡片写成 `var(--panel-bg, #fafafa)` ——那个变量在本项目里
根本不存在，于是 fallback 生效、暗色主题下白底白字看不清（2026-08-14 真机反馈）。
**这种错单测查不出、肉眼扫代码也容易漏，但一量对比度就现形。**
两个主题各跑一遍，因为本项目默认是暗色、而错误的 fallback 恰恰是浅色。

Chromium 不是 WebView2，**布局/对比度可信，滚动类问题仍需真机**（CLAUDE.md 已知坑）。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

from playwright.async_api import async_playwright

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

# 面板要的三份数据：汇总 / 价目 / 会话列表（切会话会连带刷工作区，桩要给真实形状）
STUB = """
window.pywebview = { api: new Proxy({}, { get: (t, name) => (...args) => {
  if (name === 'usage_summary') return Promise.resolve({
    ok: true, days: 30,
    total: {input_uncached: 174, input_cache_write: 0, input_cache_read: 17152,
            output: 91, rows: 12, estimated_rows: 3},
    by_model: [
      {bucket: 'deepseek-v4-flash', input_uncached: 174, input_cache_write: 0,
       input_cache_read: 17152, output: 91, rows: 10, estimated_rows: 3},
      {bucket: 'claude-opus-5', input_uncached: 5000, input_cache_write: 200,
       input_cache_read: 0, output: 900, rows: 2, estimated_rows: 0}],
    by_role: [
      {bucket: 'main', input_uncached: 174, input_cache_read: 17152, output: 91,
       input_cache_write: 0, rows: 10, estimated_rows: 3},
      {bucket: 'delegate:sub-1', input_uncached: 5000, input_cache_read: 0, output: 900,
       input_cache_write: 200, rows: 2, estimated_rows: 0}],
    by_day: [{bucket: '2026-08-14', input_uncached: 5174, input_cache_read: 17152,
              input_cache_write: 200, output: 991, rows: 12, estimated_rows: 3}],
    by_currency: {CNY: {amount: 0.61, rows: 10, inferred: true},
                  USD: {amount: 1.2345, rows: 2, inferred: false}},
    unpriced_rows: 1, cost_inferred: true,
  });
  if (name === 'get_model_prices') return Promise.resolve({ok: true, bundled: [], user: [
    {model_id: 'deepseek-v4-flash', currency: 'CNY', input: 2, output: 8,
     cache_read: 0.2, cache_write: null, as_of: '2026-08-14', source: 'user', verified: true}]});
  if (name === 'list_sessions') return Promise.resolve([]);
  if (name === 'get_workspace_tree')
    return Promise.resolve({ ok: true, root: '', label: '', tree: { children: [] } });
  return Promise.resolve({ ok: true });
}})};
"""

results = []


def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def _lum(rgb):
    """相对亮度（WCAG）。rgb 是 0-255 三元组。"""
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def parse_rgb(s):
    nums = [int(float(x)) for x in s.replace("rgba(", "").replace("rgb(", "")
            .replace(")", "").split(",")[:3]]
    return tuple(nums)


async def effective_bg(page, sel):
    """元素**实际呈现**的背景色：自身透明就往上找祖先（透明背景会让"看着有色"其实是继承的）。"""
    return await page.eval_on_selector(sel, """e => {
      let n = e;
      while (n) {
        const c = getComputedStyle(n).backgroundColor;
        if (c && c !== 'transparent' && !c.startsWith('rgba(0, 0, 0, 0')) return c;
        n = n.parentElement;
      }
      return 'rgb(255,255,255)';
    }""")


async def run_theme(page, theme: str):
    await page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    await page.evaluate("() => openUsagePanel()")
    await page.wait_for_timeout(300)

    tag = f"[{theme}]"
    vis = await page.eval_on_selector(
        "#usage-overlay", "e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)")
    check(f"{tag} 面板真的显示出来了", vis)

    # ① 对比度——这次真机反馈的病根。正文 4.5:1 是 WCAG AA，标题/大字 3:1
    for sel, label, need in (("#usage-cards .usage-card .v", "卡片数值", 3.0),
                             ("#usage-cards .usage-card .k", "卡片标签", 3.0),
                             ("#usage-tables .usage-table td", "表格正文", 4.5)):
        fg = await page.eval_on_selector(sel, "e => getComputedStyle(e).color")
        bg = await effective_bg(page, sel)
        ratio = contrast(parse_rgb(fg), parse_rgb(bg))
        check(f"{tag} {label}对比度 ≥ {need}", ratio >= need,
              f"{ratio:.2f}:1  fg={fg} bg={bg}")

    # ② 卡片底不能是白的（错误 fallback 的典型症状）
    card_bg = parse_rgb(await effective_bg(page, ".usage-card"))
    if theme == "dark":
        check(f"{tag} 卡片不是浅色底", _lum(card_bg) < 0.3, f"bg={card_bg}")

    # ③ 布局：卡片应横向排（grid），不是压成一列——蹭老 CSS 的典型症状
    tops = await page.eval_on_selector_all(
        ".usage-card", "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    check(f"{tag} 概览卡横向排列（不是竖排一列）", len(set(tops)) == 1 and len(tops) >= 3,
          f"tops={tops}")

    # ④ 宽表格由外层容器滚动（.table-wrap 在本项目只定义在 .bubble 下，面板必须自带）
    ov = await page.eval_on_selector("#usage-tables .table-wrap",
                                     "e => getComputedStyle(e).overflowX")
    check(f"{tag} 表格外层可横向滚动", ov in ("auto", "scroll"), f"overflow-x={ov}")
    disp = await page.eval_on_selector("#usage-tables .usage-table",
                                       "e => getComputedStyle(e).display")
    check(f"{tag} table 保持 display:table（WebView2 滚动坑）", disp == "table", f"display={disp}")

    # ⑤ 内容正确性：估算标记 / 可信度提示 / 分币种两行
    check(f"{tag} 估算轮次有「估」标记", await page.eval_on_selector_all(
        ".usage-est", "els => els.length") > 0)
    check(f"{tag} 可信度提示区可见", await page.eval_on_selector(
        "#usage-caveats", "e => !!(e.offsetWidth || e.offsetHeight)"))
    cur = await page.eval_on_selector_all(
        "#usage-cards .usage-card:last-child .v", "els => els.map(e => e.textContent)")
    check(f"{tag} 多币种各占一行、未相加", len(cur) == 2 and any("CNY" in c for c in cur)
          and any("USD" in c for c in cur), f"{cur}")

    # 截图要在**关掉之前**拍（初版拍在流程末尾，出来的是空界面）
    shot = f"/tmp/usage-panel-{theme}.png"
    await page.screenshot(path=shot)
    print(f"        截图 → {shot}")

    # ⑥ Esc 能关（浮层栈接对了没）
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(150)
    still = await page.eval_on_selector(
        "#usage-overlay", "e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)")
    check(f"{tag} Esc 关得掉（入了浮层栈）", not still)


async def main() -> int:
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        page = await b.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.add_init_script(STUB)
        await page.goto((WEB / "index.html").as_uri())
        await page.wait_for_timeout(400)
        for theme in ("dark", "light"):
            await run_theme(page, theme)
        check("渲染期间无 JS 报错", not errors, "; ".join(errors[:2]))
        await b.close()

    bad = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
