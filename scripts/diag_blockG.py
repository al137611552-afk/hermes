"""块G Windows 自测：Learning Engine（离线聚合 → 候选策略 → 治理）。

用法（Windows 项目根目录下）：
    python scripts/diag_blockG.py

逐项打 [PASS]/[FAIL]，全过退出码 0，任一失败退出码 1（末行 RESULT 一目了然）。
块G 纯离线无 GUI，但有 Windows 专属风险值得真机验：
  ★ StrategyStore 把**中文建议**写进 JSON（ensure_ascii=False + encoding=utf-8），
    跨会话重开后中文不能乱码（这正是项目踩过的 Windows GBK 崩坑）。
全部用临时目录，不污染 data/。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agentcore.agent.learning import aggregate, propose, StrategyStore, Candidate  # noqa: E402
from agentcore.agent.world_state import FailureMemory                              # noqa: E402

_results = []


def check(name, cond, extra=""):
    _results.append(bool(cond))
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({extra})" if extra else ""))


def main():
    print("===== 块G 自测：Learning Engine =====")
    print(f"src = {_ROOT / 'src'}\n")

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # 1) 聚合：FailureMemory.rows() → 按分类归并
        fm = FailureMemory(tmp / "failures.db")
        # external_blocked 在 3 条不同的路上反复（系统性）
        for fp in ("p1", "p2", "p3"):
            fm.record(fp, ["external_blocked"], detail=f"403 blocked at {fp}")
        # not_found 只在 1 条路上（偶发）
        fm.record("q1", ["not_found"], detail="no file")
        aggs = aggregate(fm)
        by = {a.error_class: a for a in aggs}
        check("聚合按分类归并（rows() 在 Windows 读回正常）", "external_blocked" in by)
        check("聚合统计涉及几条路 paths", by["external_blocked"].paths == 3,
              f"paths={by['external_blocked'].paths}")
        check("聚合按总次数降序", aggs[0].error_class == "external_blocked")

        # 2) 候选生成：只升级系统性失败
        cands = propose(aggs)
        classes = sorted(c.error_class for c in cands)
        check("系统性失败（跨3路）→ 生成候选", "external_blocked" in classes)
        check("单条路偶发（not_found）→ 不升级为策略", "not_found" not in classes)
        c = next(x for x in cands if x.error_class == "external_blocked")
        check("候选带语料证据（命中指纹/次数）", c.evidence.get("paths") == 3 and "p1" in c.evidence.get("fingerprints", []),
              f"evidence={c.evidence}")
        check("候选可解释（建议+理由非空）", bool(c.suggestion) and bool(c.rationale))
        fm.close()

        # 3) 瞬时 IO 永不成策略
        fm2 = FailureMemory(tmp / "fm2.db")
        for fp in ("a", "b", "c"):
            fm2.record(fp, ["transient_io"])
        check("瞬时 IO 跨多路也永不成策略", propose(aggregate(fm2)) == [])
        fm2.close()

        # 4) StrategyStore 生命周期：proposed → approve(需Golden) → active → retire/rollback
        store_path = tmp / "data" / "strategies.json"
        store = StrategyStore(store_path)
        s = store.propose(c)
        check("StrategyStore 自动建目录并落候选", store_path.exists() and s.status == "proposed")

        rejected = False
        try:
            store.approve(s.id, golden_passed=False)
        except ValueError:
            rejected = True
        check("★未过 Golden 的 approve 被拒（语料门写进代码）", rejected
              and store.get(s.id).status == "proposed")

        store.approve(s.id, golden_passed=True, by="reviewer")
        check("人审 + Golden 通过 → active", store.get(s.id).status == "active"
              and store.get(s.id).golden_passed is True)
        check("active() 列出生效策略", store.active() and store.active()[0].id == s.id)

        store.retire(s.id, reason="实测无效")
        check("退役 → 不再 active", store.get(s.id).status == "retired" and store.active() == [])
        hist = [h["to"] for h in store.get(s.id).history]
        check("状态变迁留审计 history", hist == ["proposed", "active", "retired"], str(hist))

        # 5) ★Windows 重点：中文建议写进 JSON，跨会话重开不乱码（GBK 崩坑回归验）
        store2 = StrategyStore(tmp / "cn.json")
        cn_sug = "改走浏览器直通或换数据源，正面入口已被外部挡住。"
        store2.propose(Candidate("external_blocked", cn_sug, "系统性：跨3条路反复 403", {"paths": 3}))
        del store2                                   # 丢引用，强制从磁盘文件重读
        store3 = StrategyStore(tmp / "cn.json")
        reloaded = store3.list()
        ok_cn = len(reloaded) == 1 and reloaded[0].suggestion == cn_sug
        check("★中文建议写 JSON 后跨会话重开完整无乱码（Windows UTF-8 round-trip）",
              ok_cn, repr(reloaded[0].suggestion[:16]) if reloaded else "空")

        # 6) 端到端验收：历史轨迹 → 一条可解释、Golden 后生效的策略
        fm3 = FailureMemory(tmp / "e2e.db")
        for fp in ("x1", "x2", "x3"):
            fm3.record(fp, ["external_blocked"], detail=f"blocked {fp}")
        e2e = propose(aggregate(fm3))
        fm3.close()
        st = StrategyStore(tmp / "e2e.json")
        sid = st.propose(e2e[0]).id
        st.approve(sid, golden_passed=True)
        check("端到端：历史失败 → 候选 → 人审+Golden → 生效", len(st.active()) == 1
              and st.active()[0].error_class == "external_blocked")

    ok = all(_results)
    print()
    if ok:
        print(f"===== RESULT: ALL PASS ({len(_results)}/{len(_results)}) =====")
        return 0
    failed = len(_results) - sum(_results)
    print(f"===== RESULT: {failed} FAILED （共 {len(_results)} 项）=====")
    sys.stderr.write(f"块G 自测有 {failed} 项失败\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
