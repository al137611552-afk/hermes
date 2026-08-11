#!/usr/bin/env python3
"""diff 行内定向反馈（v3.60 FR）的 UI 自检：真实 index.html + app.js 驱动，逐行点击核对。

    python scripts/diag_diff_ui.py        # 需要 pip install playwright && playwright install chromium

**不是打桩测纯逻辑**——渲染、点击、键盘、发送全走真实 DOM 代码路径，只把 pywebview 桥换成
记录型 stub（真机上那一端是 Python）。diff 由**真实 ChangeLedger** 生成，不是手写常量。

它抓到过什么：`+/-` 行是 `display:inline-block`，行尾 `\n` 若写在 span **里面**会被吞掉，
父容器又是 `white-space:pre`（不换行）→ 增删行与下一行**挤在同一行互相盖住**。
纯逻辑单测测不出来（annotateDiffLines 全对），只有真渲染 + 量几何位置才看得见。
"""
import asyncio
import difflib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from playwright.async_api import async_playwright  # noqa: E402

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

_BEFORE = "".join(f"# {n}\n" for n in
                  ["demo", "line2", "line3", "line4", "line5", "line6",
                   "line7", "line8", "line9", "line10", "line11", "line12"])


def _build_diff() -> str:
    """走真实 ChangeLedger 造 diff：删第 3 行 + 在 line10 后插两行（两个 hunk，有删有增）。"""
    from agentcore.changes import ChangeLedger
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "demo.py").write_text(_BEFORE, encoding="utf-8")
    led = ChangeLedger(ws)
    led.snapshot("demo.py")
    after = [ln for ln in _BEFORE.splitlines() if ln != "# line3"]
    i = after.index("# line10")
    after[i + 1:i + 1] = ["# added A", "# added B"]
    (ws / "demo.py").write_text("\n".join(after) + "\n", encoding="utf-8")
    diff = led.diff("demo.py")
    assert diff, "ChangeLedger 没产出 diff"
    return diff


DIFF = _build_diff()

STUB = """
window.__sent = [];
window.__calls = [];
window.pywebview = { api: new Proxy({}, { get: (t, name) => (...args) => {
  window.__calls.push([name, args]);
  if (name === 'send_message') { window.__sent.push(args); return Promise.resolve({ok:true}); }
  if (name === 'get_file_diff') return Promise.resolve({ok:true, path:args[0], diff: window.__diff});
  if (name === 'list_sessions') return Promise.resolve([]);
  return Promise.resolve({ok:true});
}})};
"""

# 期望表：diff 行索引 -> (kind, 框标题里的锚点 / None=不可点)
EXPECT = {
    0: ("meta", None), 1: ("meta", None), 2: ("hunk", None),
    3: ("ctx", "demo.py:1"), 4: ("ctx", "demo.py:2"),
    5: ("del", "demo.py（原第 3 行）"),
    6: ("ctx", "demo.py:3"), 7: ("ctx", "demo.py:4"), 8: ("ctx", "demo.py:5"),
    9: ("hunk", None),
    10: ("ctx", "demo.py:7"), 11: ("ctx", "demo.py:8"), 12: ("ctx", "demo.py:9"),
    13: ("add", "demo.py:10"), 14: ("add", "demo.py:11"),
    15: ("ctx", "demo.py:12"), 16: ("ctx", "demo.py:13"),
}

results = []
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
        await page.add_init_script("window.__diff = " + json.dumps(DIFF))
        await page.goto((WEB / "index.html").as_uri())
        await page.wait_for_timeout(400)
        # 造一个活动会话，然后走真实的 previewDiff（它会调 get_file_diff → 渲染可点的 diff）
        await page.evaluate("() => { activeCid = 1; mountView(1); }")
        await page.evaluate("() => previewDiff('demo.py')")
        await page.wait_for_selector(".ws-diff .diff-line")

        rows = await page.eval_on_selector_all(
            ".ws-diff .diff-line",
            "els => els.map(e => ({text: e.textContent.replace(/\\n$/,''), cls: e.className, title: e.title}))")
        check("渲染出的行数 = diff 行数", len(rows) == len(DIFF.split("\n")),
              f"渲染 {len(rows)} vs diff {len(DIFF.split(chr(10)))}")

        # ---- 1. 可点性 + 类名 ----
        for i, (kind, anchor) in EXPECT.items():
            r = rows[i]
            has_click = "dl-click" in r["cls"]
            check(f"[{i}] {r['text'][:22]!r} kind={kind} 可点={anchor is not None}",
                  (f" {kind}" in " " + r["cls"]) and has_click == (anchor is not None),
                  f"cls={r['cls']} title={r['title']}")

        # ---- 2. 逐行点击 → 框标题锚点 ----
        for i, (kind, anchor) in EXPECT.items():
            if anchor is None:
                # 先把上一行留下的框关掉，否则测的是"上一个框还在"而不是"这行开了框"
                if await page.eval_on_selector_all(".dl-feedback", "els => els.length"):
                    await page.press(".dl-fb-input", "Escape")
                    await page.wait_for_timeout(60)
                await page.eval_on_selector_all(
                    ".ws-diff .diff-line", "(els, i) => els[i].click()", i)
                n = await page.eval_on_selector_all(".dl-feedback", "els => els.length")
                check(f"[{i}] 不可点的行点了不出框", n == 0, f"出现 {n} 个框")
                # 另一面：框开着时点不可点的行，不该把框弄没（点了等于没点）
                await page.eval_on_selector_all(".ws-diff .diff-line", "(els) => els[3].click()")
                await page.eval_on_selector_all(
                    ".ws-diff .diff-line", "(els, i) => els[i].click()", i)
                n2 = await page.eval_on_selector_all(".dl-feedback", "els => els.length")
                check(f"[{i}] 框开着时点它：框不受影响", n2 == 1, f"框 {n2} 个")
                continue
            await page.eval_on_selector_all(".ws-diff .diff-line", "(els, i) => els[i].click()", i)
            head = await page.eval_on_selector(".dl-fb-head", "e => e.textContent")
            check(f"[{i}] 框标题锚点 = {anchor}", anchor in head, f"实际：{head}")
            n_box = await page.eval_on_selector_all(".dl-feedback", "els => els.length")
            n_sel = await page.eval_on_selector_all(".dl-sel", "els => els.length")
            check(f"[{i}] 同时只有一个框 + 一处高亮", n_box == 1 and n_sel == 1, f"框{n_box} 高亮{n_sel}")

        # ---- 3. 空内容按 Enter：不发、不关框 ----
        await page.eval_on_selector_all(".ws-diff .diff-line", "(els) => els[13].click()")
        await page.press(".dl-fb-input", "Enter")
        await page.wait_for_timeout(100)
        n_box = await page.eval_on_selector_all(".dl-feedback", "els => els.length")
        sent = await page.evaluate("window.__sent.length")
        check("空内容 Enter：框还在且没发送", n_box == 1 and sent == 0, f"框{n_box} 已发{sent}")

        # ---- 4. Shift+Enter 换行不发送 ----
        await page.fill(".dl-fb-input", "第一行")
        await page.press(".dl-fb-input", "Shift+Enter")
        await page.wait_for_timeout(80)
        val = await page.eval_on_selector(".dl-fb-input", "e => e.value")
        sent = await page.evaluate("window.__sent.length")
        check("Shift+Enter：换行且不发送", val.endswith("\n") and sent == 0, f"值={val!r} 已发{sent}")

        # ---- 5. Esc 只关框，不动 diff/预览 ----
        await page.press(".dl-fb-input", "Escape")
        await page.wait_for_timeout(100)
        n_box = await page.eval_on_selector_all(".dl-feedback", "els => els.length")
        n_lines = await page.eval_on_selector_all(".ws-diff .diff-line", "els => els.length")
        n_sel = await page.eval_on_selector_all(".dl-sel", "els => els.length")
        check("Esc：关框 / diff 仍在 / 高亮撤掉",
              n_box == 0 and n_lines == len(rows) and n_sel == 0, f"框{n_box} 行{n_lines} 高亮{n_sel}")

        # ---- 6. 新增行发出的消息逐字对 ----
        await page.eval_on_selector_all(".ws-diff .diff-line", "(els) => els[13].click()")
        await page.fill(".dl-fb-input", "  这两行注释多余  ")
        await page.press(".dl-fb-input", "Enter")
        await page.wait_for_timeout(150)
        sent = await page.evaluate("window.__sent")
        expect_add = ("关于 `demo.py:10` 这一行：\n\n```diff\n+# added A\n```\n\n这两行注释多余")
        check("新增行消息逐字一致", len(sent) == 1 and sent[0][0] == expect_add,
              f"实际：{sent[0][0]!r}" if sent else "没发出")
        n_box = await page.eval_on_selector_all(".dl-feedback", "els => els.length")
        check("发送后框自动关闭", n_box == 0)

        # ---- 7. 删除行发出的消息逐字对（锚点必须标明是旧行号）----
        await page.eval_on_selector_all(".ws-diff .diff-line", "(els) => els[5].click()")
        await page.fill(".dl-fb-input", "这行不该删")
        await page.press(".dl-fb-input", "Enter")
        await page.wait_for_timeout(150)
        sent = await page.evaluate("window.__sent")
        expect_del = ("关于 `demo.py（原第 3 行，已删除）` 这一行：\n\n```diff\n-# line3\n```\n\n这行不该删")
        check("删除行消息逐字一致（标明原行号+已删除）",
              len(sent) == 2 and sent[1][0] == expect_del,
              f"实际：{sent[1][0]!r}" if len(sent) > 1 else "没发出")

        # ---- 8. 消息真的进了对话流（不是只调了桥）----
        bubbles = await page.eval_on_selector_all(
            ".msg.user .bubble", "els => els.map(e => e.textContent)")
        check("两条反馈都渲染进了对话流", len(bubbles) >= 2 and "demo.py:10" in " ".join(bubbles),
              f"气泡数 {len(bubbles)}")

        # ---- 9. 每行独占一行（inline-block 吞换行的那个 bug 的直接判据）----
        tops = await page.eval_on_selector_all(
            ".ws-diff .diff-line", "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
        check("每行独占一行（+/- 行没和下一行挤在一起）", len(set(tops)) == len(tops),
              f"tops={tops}")
        txt = await page.eval_on_selector(".ws-diff", "e => e.textContent")
        check("选中复制得到的文本仍是原始 diff", txt.rstrip("\n") == DIFF.rstrip("\n"))

        check("无 JS 报错", not errors, str(errors[:2]))
        await page.screenshot(path=str(pathlib.Path(tempfile.gettempdir()) / "diag_diff_ui.png"), full_page=True)
        await b.close()

    bad = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
