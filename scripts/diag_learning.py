#!/usr/bin/env python3
"""Learning Engine 语料盘点（只读，不改任何状态）。

用法：
    python scripts/diag_learning.py [failures.db 路径]     # 默认 <ROOT>/data/failures.db

回答一个问题：**现在到底有多少失败语料，够不够支撑"策略"这件事**。
块 G 的门槛是「同一错误分类跨 ≥2 条不同的路累计 ≥3 次」——语料稀薄时提不出候选，
那么给决策层接线也没东西可学，反而多一层不确定性。所以接线之前先看这份账。

只读：不写 StrategyStore、不改 failures.db、不碰运行时。

「做法」列：v3.60 起记录 `工具名` 与 `工具名|after_nudge`（提示过仍走同一条路）。
更早的记录没有这个标签，显示为「—」——那正是当初 Learning 提不出有针对性建议的原因。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.learning import aggregate, propose  # noqa: E402
from agentcore.agent.world_state import FailureMemory  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("用法：python scripts/diag_learning.py [failures.db 路径]", file=sys.stderr)
        return 2
    if len(argv) == 2:
        db = Path(argv[1])
    else:
        from agentcore.config import ROOT
        db = ROOT / "data" / "failures.db"
    if not db.is_file():
        print(f"没有找到 {db}——这台机器还没攒下失败语料（功能没开或没跑过失败任务）。")
        return 0

    fm = FailureMemory(db)
    rows = list(fm.rows())
    total_events = sum(int(r.get("count", 0) or 0) for r in rows)
    print(f"语料库：{db}")
    print(f"  去重后的失败行：{len(rows)}　累计失败次数：{total_events}")
    if not rows:
        print("\n结论：**空的**。接线没有意义——先攒语料（真实跑失败任务）再谈。")
        return 0

    aggs = aggregate(fm)
    print(f"\n按错误分类聚合（共 {len(aggs)} 类）：")
    print(f"  {'分类':<22}{'次数':>6}{'涉及路数':>10}   做法（工具 ｜ after_nudge=提示过仍重复）")
    for a in aggs:
        decs = "、".join(f"{k}×{v}" for k, v in sorted(
            a.decisions.items(), key=lambda kv: -kv[1])[:3]) or "—（旧记录没有标签，v3.60 起才记）"
        print(f"  {a.error_class:<22}{a.total:>6}{a.paths:>10}   {decs}")

    cands = propose(aggs)
    print(f"\n达到候选门槛（跨 ≥2 条路、累计 ≥3 次、且非 transient_io）的：{len(cands)} 条")
    for c in cands:
        print(f"\n  ▸ {c.error_class}")
        print(f"    建议：{c.suggestion}")
        print(f"    依据：{c.rationale}")
        ex = (c.evidence.get("examples") or [None])[0]
        if ex:
            print(f"    样例：{ex[:120]}")

    # 差一点的：让人看清"再攒多少就够"
    near = [a for a in aggs if a not in [] and (a.total < 3 or a.paths < 2)
            and a.error_class != "transient_io"]
    if near:
        print(f"\n未达门槛但已有记录的 {len(near)} 类（差在次数或路数）：")
        for a in near[:8]:
            miss = []
            if a.total < 3:
                miss.append(f"还差 {3 - a.total} 次")
            if a.paths < 2:
                miss.append("只出现在 1 条路上")
            print(f"  · {a.error_class}：{a.total} 次 / {a.paths} 条路（{'，'.join(miss)}）")

    print("\n结论：", end="")
    if cands:
        print(f"有 {len(cands)} 条系统性失败够格提候选——接线值得谈，但先按影子模式验证。")
    else:
        print("**提不出任何候选**。此时给决策层接线＝多一层不确定性、零收益；先攒语料。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
