#!/usr/bin/env python3
"""分批评审的分屏渲染自检：真实 index.html + app.js，用真事件驱动一轮两批评审。

    python scripts/diag_review_batch_ui.py   # 需要 pip install playwright && playwright install chromium

**为什么必须真渲染**：这次改的就是渲染本身。分批后同一轮里同一角色会说多次，而发言块原本按
`(轮, 角色)` 索引——第 2 批的流式文本会追加进第 1 批的气泡、第 1 批的结论被覆盖，症状正是"挤成一团"。
纯逻辑单测（`tests/web/debate_batch.test.js`）只能证明键算得对，**证明不了 DOM 真的分成了两块、
两块真的不重叠**。这条教训项目里栽过（v3.62.1：纯逻辑全对、元素都在，坏的是布局流）。

链路：review_started → review_seed → round_start → batch_start(1/2) → delta×2角色 → reviewer_done×2
→ batch_start(2/2) → delta×2角色 → reviewer_done×2 → main_reply_start/delta/done。

自带活性：把 `debateTurnKey` 改成永远返回 `role`（即回到分批之前的行为），本脚本必须变红
——见文末「活性自检」提示。
"""
import asyncio
import pathlib
import sys

from playwright.async_api import async_playwright

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

STUB = """
window.__calls = [];
window.pywebview = { api: new Proxy({}, { get: (t, name) => (...args) => {
  window.__calls.push([name, args]);
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


async def ev(page, event, data):
    await page.evaluate(
        "([event, data]) => window.__onAgentEvent({ event, data, cid: 1 })", [event, data])
    await page.wait_for_timeout(30)


async def boxes(page, sel):
    """量几何：返回每个元素的 {top,bottom,height,text}。空数组 = 压根没渲染出来。"""
    return await page.eval_on_selector_all(sel, """els => els.map(e => {
        const r = e.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, height: r.height,
                 text: (e.textContent || '').trim() };
    })""")


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

        # ---- 一轮两批评审的完整事件流 ----
        await ev(page, "review_started", {"models": {"product": "m1", "technical": "m2"},
                                          "heterogeneous": True})
        await ev(page, "review_seed", {"decisions": [{"id": f"d{i}"} for i in range(12)]})
        await ev(page, "review_round_start", {"round": 1})

        for bi, ids in ((1, [f"d{i}" for i in range(8)]), (2, [f"d{i}" for i in range(8, 12)])):
            await ev(page, "review_batch_start",
                     {"round": 1, "batch": bi, "batches": 2, "ids": ids})
            for role in ("product", "technical"):
                await ev(page, "review_delta",
                         {"reviewer": role, "text": f"第{bi}批{role}的进言内容。"})
                await ev(page, "review_reviewer_done",
                         {"round": 1, "reviewer": role, "batch": bi, "batches": 2,
                          "verdict": f"第{bi}批{role}的进言内容。\n```json\n[]\n```"})

        # ---- 1. 分成了独立的块，而不是挤进同一块 ----
        turns = await boxes(page, ".rvd-stream .rvd-turn")
        check("一轮两批两角色 → 渲染出 4 个独立发言块", len(turns) == 4, f"实际 {len(turns)}")

        # ---- 2. 块与块不重叠（"挤成一团"的直接量法）----
        overlap = []
        for i in range(len(turns) - 1):
            if turns[i]["bottom"] > turns[i + 1]["top"] + 0.5:
                overlap.append((i, turns[i]["bottom"], turns[i + 1]["top"]))
        check("相邻发言块不重叠（量几何，不是查元素存在）", not overlap, str(overlap))
        check("每个发言块都有实际高度（不是塌成 0）",
              all(t["height"] > 8 for t in turns), str([t["height"] for t in turns]))

        # ---- 3. 第 2 批的字没有跑进第 1 批的块（本次 bug 的核心）----
        first = turns[0]["text"]
        check("第 1 批的块里没有第 2 批的内容", "第2批" not in first, first[:80])
        check("第 2 批的内容确实渲染出来了",
              any("第2批" in t["text"] for t in turns))

        # ---- 4. 标签把批次说清楚，否则两段看着像模型重复了一遍 ----
        heads = [h["text"] for h in await boxes(page, ".rvd-stream .rvd-turn-head")]
        check("发言块标题带批次", sum(1 for h in heads if "第 1/2 批" in h) == 2
              and sum(1 for h in heads if "第 2/2 批" in h) == 2, str(heads))

        # ---- 5. 批次分隔条：看得见、写明本批评哪几条、比轮次分隔弱一档 ----
        seps = await boxes(page, ".rvd-stream .rvd-batch-sep")
        check("两条批次分隔条都渲染出来", len(seps) == 2, f"实际 {len(seps)}")
        check("分隔条写明本批评了哪几条决策",
              seps and "d0" in seps[0]["text"] and "d8" in seps[1]["text"],
              str([s["text"] for s in seps]))
        check("分隔条看得见（有高度）", all(s["height"] > 4 for s in seps),
              str([s["height"] for s in seps]))
        round_sep = await boxes(page, ".rvd-stream .rvd-round-sep")
        check("轮次分隔与批次分隔是两种不同元素（层级看得出来）",
              len(round_sep) == 1 and len(seps) == 2)

        # ---- 6. 单批时零变化（日常方案不该看到任何批次痕迹）----
        await ev(page, "review_round_start", {"round": 2})
        for role in ("product", "technical"):
            await ev(page, "review_delta", {"reviewer": role, "text": "第二轮单批进言。"})
            await ev(page, "review_reviewer_done",
                     {"round": 2, "reviewer": role, "verdict": "第二轮单批进言。"})
        seps2 = await boxes(page, ".rvd-stream .rvd-batch-sep")
        check("第 2 轮未分批 → 没有新增批次分隔条", len(seps2) == 2, f"实际 {len(seps2)}")
        heads2 = [h["text"] for h in await boxes(page, ".rvd-stream .rvd-turn-head")]
        r2 = heads2[4:]
        check("未分批的发言块标题不带批次后缀",
              r2 and all("批" not in h for h in r2), str(r2))

        check("页面无 JS 报错", not errors, str(errors[:2]))
        await b.close()

    ok = sum(1 for r, _, _ in results if r)
    print(f"\n{ok}/{len(results)} 通过")
    if ok != len(results):
        return 1
    print("\n活性自检：把 web/pure.js 的 debateTurnKey 改成永远 `return String(role);`"
          "（＝回到分批之前的行为），本脚本应变红。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
