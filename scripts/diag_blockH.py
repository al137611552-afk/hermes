"""块H Windows 自测：Research Evaluator（搜索质量评估 + 不达标重搜提示）。

用法（Windows 项目根目录下）：
    python scripts/diag_blockH.py

逐项打 [PASS]/[FAIL]，全过退出码 0，任一失败退出码 1。验确定性机制（不连真模型/真搜索）：
  H1 评估器把"返回了但超预算"判成 blocker issue；H2 据此产出"换词/换源重搜"提示。
真模型据提示**真的重搜**是活体行为，脚本测不了——见末尾活体观察清单。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agentcore.agent.evaluators import evaluate                       # noqa: E402
from agentcore.agent.loop import detect_low_quality_research          # noqa: E402

_results = []


def check(name, cond, extra=""):
    _results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


class _Call:
    def __init__(self, i, name, params):
        self.id, self.name, self.input = str(i), name, params


# 小红书"618 女士睡衣 500 元以内"——返回一堆超预算结果（含中文，验 Windows 编码）
_MISS = ("[搜索结果·bing] 小红书 618 女士睡衣 推荐\n"
         "1. 真丝睡衣套装 高端\n   http://a\n   ¥899 618大促\n"
         "2. 设计师款睡裙\n   http://b\n   1280元 限量\n"
         "3. 进口长袖睡衣\n   http://c\n   ￥699")
_OK = ("[搜索结果·bing] 女士睡衣 500元以内\n"
       "1. 纯棉睡衣\n   http://a\n   ¥199\n"
       "2. 真丝款\n   http://b\n   899元\n"
       "3. 冰丝睡裙\n   http://c\n   ¥359")
_Q = "在小红书搜索618推荐的女士睡衣，500元以内"


def main():
    print("===== 块H 自测：Research Evaluator =====")
    print(f"src = {_ROOT / 'src'}\n")

    # H1：评估器把超预算结果判成不达标
    ev = evaluate("web_search", _MISS, {"query": _Q})
    check("H1 web_search 归 ResearchEvaluator（产 budget_ceiling 指标）",
          ev is not None and "budget_ceiling" in ev.metrics)
    check("H1 解析出预算上限 500", ev.metrics.get("budget_ceiling") == 500,
          f"ceiling={ev.metrics.get('budget_ceiling')}")
    check("H1 算出在预算内条数=0（3 条标价无一 ≤500）", ev.metrics.get("within_budget") == 0,
          f"within={ev.metrics.get('within_budget')}")
    check("★H1 判定不达标 → blocker issue（中文文案在 Windows 不乱码）",
          bool(ev.issues) and "无一在预算内" in ev.issues[0],
          (ev.issues[0][:24] if ev.issues else "无"))

    # H1：达标结果不误报
    ev_ok = evaluate("web_search", _OK, {"query": "女士睡衣 500元以内"})
    check("H1 有在预算内的结果 → 不误报 issue", ev_ok.issues == []
          and ev_ok.metrics.get("within_budget") == 2, f"within={ev_ok.metrics.get('within_budget')}")

    # H2：不达标 → 催重搜提示
    msg = detect_low_quality_research([_Call(1, "web_search", {"query": _Q})], {"1": _MISS}, {}, 1)
    check("★H2 不达标 → 产出换词/换源重搜提示", bool(msg) and "重搜" in msg,
          (msg[:30] if msg else "无"))

    # H2：达标不催
    msg_ok = detect_low_quality_research(
        [_Call(1, "web_search", {"query": "女士睡衣 500元以内"})], {"1": _OK}, {}, 1)
    check("H2 结果达标 → 不催重搜", msg_ok is None)

    # H2：per-query 封顶防无限重搜
    state = {}
    c = [_Call(1, "web_search", {"query": _Q})]
    m1 = detect_low_quality_research(c, {"1": _MISS}, state, 1)
    m2 = detect_low_quality_research(c, {"1": _MISS}, state, 1)
    check("H2 同一 query 催重搜封顶（防无限）", bool(m1) and m2 is None)

    # H2：无预算诉求不触发
    msg_np = detect_low_quality_research(
        [_Call(1, "web_search", {"query": "好看的女士睡衣推荐"})], {"1": _MISS}, {}, 1)
    check("H2 没给预算约束 → 不催重搜（不误扰正常搜索）", msg_np is None)

    ok = all(_results)
    print()
    if ok:
        print(f"===== RESULT: ALL PASS ({len(_results)}/{len(_results)}) =====")
        print("\n[活体观察清单] 机制已验。真模型行为请在 GUI 里跑一次真实搜索观察：")
        print("  1) 给 Hermes：在小红书搜618推荐的女士睡衣，500元以内")
        print("  2) 看它搜完后是否注入了「返回了但不达标…换词/换源重搜」并据此**再搜一次**")
        print("  3) 若 config research_refine=false 则不会催重搜（只在摘要条显示质量）")
        return 0
    failed = len(_results) - sum(_results)
    print(f"===== RESULT: {failed} FAILED （共 {len(_results)} 项）=====")
    sys.stderr.write(f"块H 自测有 {failed} 项失败\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
