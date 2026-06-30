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

    # —— H3a 模型裁判（用假裁判验机制；真模型在 GUI 活体验）——
    from agentcore.agent.loop import detect_offtarget_research
    from agentcore.agent.judge import judge_research
    _OFF = '{"on_target": false, "off": ["真丝厚睡衣：秋冬款，不符夏季"], "suggestion": "加\'冰丝 短袖\'换平台重搜"}'
    # 夏季睡衣却返回厚款（文字层）——裁判判不对题 → 催重搜
    off_calls = [_Call(1, "web_search", {"query": "618夏季女士睡衣"})]
    off_out = {"1": "1. 真丝厚睡衣套装\n   http://a\n   ¥299 秋冬加厚"}
    m_off = detect_offtarget_research(off_calls, off_out, "搜618夏季女士睡衣并附图",
                                      lambda p, i: _OFF, {}, 1)
    check("★H3a 夏季睡衣返回厚秋冬款 → 裁判判不对题、催重搜", bool(m_off) and "不对题" in m_off,
          (m_off[:28] if m_off else "无"))
    # 对题不催
    m_on = detect_offtarget_research(
        [_Call(2, "web_search", {"query": "夏季睡衣"})], {"2": "1. 冰丝短袖睡衣"},
        "夏季睡衣", lambda p, i: '{"on_target": true}', {}, 1)
    check("H3a 结果对题 → 不催重搜", m_on is None)
    # 裁判故障 → 放行不拦（不因模型出错误触发重搜）
    def _boom(p, i):
        raise RuntimeError("模型超时")
    v_fail = judge_research("夏季睡衣", "厚款", _boom)
    check("H3a 裁判故障 → 放行不拦（不误触发）", v_fail.on_target is True)

    # —— H3b 带图答案多模态裁判（用假裁判验机制；真看图在 GUI 活体验）——
    from agentcore.agent.loop import detect_offtarget_answer
    _IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    _OFFA = '{"on_target": false, "off": ["配图为加厚秋冬款，不符夏季"], "suggestion": "据图改选冰丝短袖"}'
    # 夏季睡衣答案配的却是冬季厚款图 → 裁判看图判不对题、催据图重选
    m_img = detect_offtarget_answer("搜618夏季女士睡衣并附图", "推荐这几款真丝睡衣。",
                                    [dict(_IMG)], lambda p, i: _OFFA)
    check("★H3b 答案配图是冬季款 → 看图判不符、催据图重选",
          bool(m_img) and "配图与目标不符" in m_img, (m_img[:26] if m_img else "无"))
    # 无配图 → 不空跑裁判
    calls_img = {"n": 0}
    def _cnt(p, i):
        calls_img["n"] += 1
        return _OFFA
    m_noimg = detect_offtarget_answer("夏季睡衣", "纯文字答案", [], _cnt)
    check("H3b 答案无配图 → 不触发、也不空跑裁判", m_noimg is None and calls_img["n"] == 0)
    # 裁判故障 → 放行不拦
    m_boom = detect_offtarget_answer("夏季睡衣", "答案", [dict(_IMG)],
                                     lambda p, i: (_ for _ in ()).throw(RuntimeError("视觉超时")))
    check("H3b 裁判故障 → 放行不拦（不误触发）", m_boom is None)

    # —— H3c 萃取（三态）+ 接地/时效闸 ——
    from agentcore.agent.loop import detect_ungrounded_answer
    _SALV = '{"on_target": false, "use": ["冰丝短袖睡衣 ¥199"], "off": ["真丝厚款：秋冬不符夏季"]}'
    m_salv = detect_offtarget_research(off_calls, {"1": "1.厚款\n2.冰丝短袖 ¥199"},
                                       "618夏季女士睡衣", lambda p, i: _SALV, {}, 1)
    check("★H3c 部分污染 → 萃取有效项采用、不整批丢（杀掉'请不要采用这些结果'）",
          bool(m_salv) and "部分有效" in m_salv and "请不要采用这些结果" not in m_salv,
          (m_salv[:24] if m_salv else "无"))
    check("★H3c 接地/时效闸：搜了却凭记忆答时效问题 → 催据来源作答/声明过时",
          bool(detect_ungrounded_answer("查2026最新显卡价格", "大概三千到五千元。", True)))
    check("H3c 答案带来源 → 接地不打扰",
          detect_ungrounded_answer("2026最新价格", "据 http://jd.com/x 售价¥4999", True) is None)
    check("H3c 非时效问题 → 凭常识答不误杀",
          detect_ungrounded_answer("光合作用原理", "植物把光能转化为化学能。", True) is None)

    # —— 全局重搜预算（止血：防"换关键词"无限重搜→交白卷）——
    import inspect as _insp
    from agentcore.agent.loop import AgentLoop as _AL
    check("★全局重搜预算 research_max_rounds 存在（默认3）",
          _insp.signature(_AL.__init__).parameters.get("research_max_rounds") is not None
          and _insp.signature(_AL.__init__).parameters["research_max_rounds"].default == 3)

    # —— Novelty/Progress + 换源策略阶梯（确定性事实，无模型、无分数）——
    from agentcore.agent.loop import extract_domains, switch_strategy_nudge
    check("Novelty 去重：抽域名、去 www.、大小写归一",
          extract_domains("http://www.JD.com/x https://b.tmall.com/y http://JD.com/z")
          == {"jd.com", "b.tmall.com"})
    s0, s1, s2 = (switch_strategy_nudge(i) for i in range(3))
    check("★换源阶梯：NO_PROGRESS 逐级 site→browser→ask_user，越界停",
          bool(s0) and "site:" in s0 and "浏览器直通" in s1 and "ask_user" in s2
          and switch_strategy_nudge(3) is None,
          "site→browser→ask_user")

    ok = all(_results)
    print()
    if ok:
        print(f"===== RESULT: ALL PASS ({len(_results)}/{len(_results)}) =====")
        print("\n[活体观察清单] 机制已验。真模型行为请在 GUI 里跑真实搜索观察：")
        print("  1) 预算（H1/H2 正则）：搜『618女士睡衣 500元以内』→ 看是否因超预算自动重搜")
        print("  2) 语义（H3a 模型裁判）：搜『618夏季女士睡衣』→ 若返回厚秋冬款，看裁判是否判")
        print("     『多数不对题』并据此换词/换源重搜（每次搜索后多一次模型调用）")
        print("  3) 看图（H3b 多模态）：搜『618夏季女士睡衣并附图』走**浏览器穿透**截图配图 →")
        print("     若配图是冬季厚款，看裁判是否**连图判**『配图与目标不符』并据图重选/重搜")
        print("     （带图答案收尾时多一次视觉模型调用；每轮最多重判一次）")
        print("  4) 萃取+接地（H3c）：搜『2026最新显卡价格』，结果有相关有无关时——看是否**挑出相关的用**"
              "（不整批丢），以及最终答案是否**带搜到的来源**而非凭记忆（凭记忆答时效问题会被催据来源/声明过时）")
        print("  5) **全局预算止血**：搜『2026最新显卡价格』这类老搜不到的——看是否最多催重搜 research_max_rounds(默认3)"
              "次后**强制停搜、用现有内容综合作答+声明局限**，而不是无限换词重搜交白卷（调 config 的 research_max_rounds）")
        print("  6) **换源策略阶梯**：同上搜不到时——若某轮重搜**没带来任何新来源**（还是那几个站点），看提示是否"
              "**从『换词重搜』升级为『换检索方式』**：site:官方/github → 浏览器直通 → ask_user 问用户（逐级升），"
              "而不是一直换关键词泛搜；有新来源时才继续换词")
        print("  7) 开关：research_refine=false 关重搜；research_judge=false 只留正则不调裁判")
        return 0
    failed = len(_results) - sum(_results)
    print(f"===== RESULT: {failed} FAILED （共 {len(_results)} 项）=====")
    sys.stderr.write(f"块H 自测有 {failed} 项失败\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
