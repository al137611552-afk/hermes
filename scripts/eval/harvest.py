"""块 V4 —— 喂饱 Learning：把评测跑出来的失败语料收成候选策略报告。

    python scripts/eval/harvest.py                    # 离线回放收割（免费、不需要 key）
    python scripts/eval/harvest.py --tier L3          # 只收某一层
    python scripts/eval/harvest.py --live --repeat 3  # 真跑收割（烧 key，样本才是真独立的）

产物：`data/eval_harvest/<run_id>/`（report.md + candidates.json + failures.harvest.db）。

## 为什么不是 `run_eval.py --accumulate`

ROADMAP 原本写的是"批跑 `--repeat 3` 写满 `failures.eval.db`"。**共用一个库跑不通**——
块 V3 的第三个发现：死路提示的文案里嵌着**跨会话累计次数**（「这条路已累计 N 次失败」），
共用库时 N 每跑都在涨 → 模型看到的文本每跑都不同 → cassette 请求指纹每跑都变 → **回放必 miss**。

所以收割走另一条路：**每个任务在自己的纯净库里回放**（与录制时条件逐字一致，回放才成立），
跑完再把行**合并**进汇总库。合并是纯数据操作，不碰任何轨迹。

## 为什么回放收割**不**做 `--repeat`

回放里模型输出是固定的：同一个任务重复 N 遍产生的是**同一条轨迹、同一批失败**，
把它乘 N 只是在伪造证据（`propose()` 的门槛 min_count/min_paths 会被灌水骗过）。
要真正的独立样本只能真跑（`--live --repeat N`）——那时模型每次走的路不同，失败才是新样本。

## 不自动采纳（ADR 0027 决策 7 / ADR 0014、0017）

本脚本只产**候选**。生命周期仍是：人审 → Golden 追加语料 → `approve(golden_passed=True)` → active。
"喂饱"的定义是"让 `propose()` 产出有证据的候选"，不是"让候选自动上线"。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from harness import run_task  # noqa: E402
from record import new_run_id  # noqa: E402
from tasks import TASKS, TIERS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "cassettes"


# ---- 纯逻辑（可脱离模型/IO 单测）--------------------------------------------

def merge_rows(base: "list[dict]", incoming: "list[dict]") -> "list[dict]":
    """按 (fingerprint, error_class, decision) 合并失败行——与 FailureMemory 的主键同口径。

    count 相加、first_at 取最早、last_at 取最晚、detail 优先保留非空的（样例给人审看）。
    **纯函数**：合并只影响事后分析，绝不回写进任何一次跑的库（回写就会污染下一次回放）。
    """
    out: dict = {}
    for row in list(base) + list(incoming):
        key = (row.get("fingerprint", ""), row.get("error_class", ""), row.get("decision", ""))
        cur = out.get(key)
        if cur is None:
            out[key] = dict(row)
            continue
        cur["count"] = int(cur.get("count", 0)) + int(row.get("count", 0))
        cur["first_at"] = min(cur.get("first_at", 0) or 0, row.get("first_at", 0) or 0)
        cur["last_at"] = max(cur.get("last_at", 0) or 0, row.get("last_at", 0) or 0)
        if not (cur.get("detail") or "").strip():
            cur["detail"] = row.get("detail", "")
    return sorted(out.values(), key=lambda r: (-int(r.get("count", 0)), r.get("error_class", "")))


def pick_tasks(names, *, tier: "str | None", live: bool) -> "tuple[list, list]":
    """挑要收割的任务，返回 (可收割, 跳过的(名字, 原因))。

    回放收割只能跑**录过音**的任务；真跑收割则连不可回放的也能跑（它们照样产语料）。
    """
    take, skip = [], []
    for n in names:
        t = TASKS[n]
        if tier and t.tier != tier:
            continue
        if not live:
            if not t.replayable:
                skip.append((n, f"不可回放（{t.unreplayable_why}）"))
                continue
            if not (CASSETTES / n).is_dir():
                skip.append((n, "尚未录制"))
                continue
        take.append(n)
    return take, skip


def render_report(candidates, aggregates, meta: dict) -> str:
    """把聚合与候选渲染成人审用的 Markdown（纯函数）。"""
    lines = [
        "# Learning 候选策略报告（块 V4）", "",
        f"- 收割方式：**{'真跑' if meta.get('live') else '离线回放'}**"
        f"（{meta.get('tasks', 0)} 个任务 × {meta.get('repeat', 1)} 遍）",
        f"- 失败语料：{meta.get('rows', 0)} 行 / {meta.get('failures', 0)} 次失败",
        f"- 生成时间：{meta.get('run_id', '')}", "",
        "> 本报告只产**候选**。生命周期：人审 → Golden 追加语料 → "
        "`approve(golden_passed=True)` → active（ADR 0027 决策 7，不自动采纳）。", "",
        "## 候选策略", "",
    ]
    if not candidates:
        lines += [
            "**一条都没产出。** 按 ADR 0027 的验收判据，这说明**任务集的失败面不够宽**"
            "（`propose` 门槛：同一分类跨 ≥2 条不同的路累计 ≥3 次），",
            "而不是 Learning 坏了。对策是回 V2 补任务，别调低门槛——"
            "门槛调低只会批量生成垃圾候选（ADR 0014 已论证）。", "",
        ]
    for i, c in enumerate(candidates, 1):
        ev = c.evidence
        lines += [
            f"### {i}. `{c.error_class}`", "",
            f"- **建议**：{c.suggestion}",
            f"- **依据**：{c.rationale}",
            f"- **证据**：{ev.get('total', 0)} 次失败 / {ev.get('paths', 0)} 条不同的路",
            f"- **做法标签**（工具｜是否被提示过仍走同一条路）：{ev.get('decisions', {})}",
            f"- **指纹**：{', '.join(ev.get('fingerprints', [])[:5])}", "",
            "样例：", "",
        ]
        lines += [f"  - `{e}`" for e in ev.get("examples", [])[:3]] + [""]
    lines += ["## 全部聚合（含未过门的）", "",
              "| 分类 | 失败次数 | 路数 | 过门? |", "|---|---|---|---|"]
    passed = {c.error_class for c in candidates}
    for a in aggregates:
        lines.append(f"| `{a.error_class}` | {a.total} | {a.paths} | "
                     f"{'✅' if a.error_class in passed else '—'} |")
    return "\n".join(lines) + "\n"


# ---- IO 侧 --------------------------------------------------------------------

def _harvest_one(name: str, db_path: Path, *, live: bool, quiet: bool) -> dict:
    """跑一个任务、把它**自己那份**失败库的行读出来（不共用库，理由见模块头）。"""
    task = TASKS[name]
    if not live:
        os.environ["HERMES_CASSETTE_MODE"] = "replay"
        os.environ["HERMES_CASSETTE_DIR"] = str(CASSETTES / name)
    else:
        os.environ.pop("HERMES_CASSETTE_MODE", None)
        os.environ.pop("HERMES_CASSETTE_DIR", None)
    with tempfile.TemporaryDirectory(prefix=f"hharv_{name}_") as d:
        ws = Path(d) / "ws"
        ws.mkdir()
        if not live:
            os.environ["HERMES_CASSETTE_WS"] = str(ws)
        task.setup(ws)
        res = run_task(str(ws), task.prompt, model="dsv4" if not live else None,
                       verbose=False, failure_db=str(db_path),
                       max_steps=task.max_steps, max_tokens=task.max_tokens,
                       world=task.world, deny_tools=task.deny_tools,
                       autonomous=task.autonomous, crazy_rounds=task.crazy_rounds,
                       crazy_seconds=task.crazy_seconds)
    from agentcore.agent.world_state import FailureMemory
    fm = FailureMemory(db_path, source="eval")
    try:
        rows = fm.rows()
    finally:
        fm.close()
    if not quiet:
        n = sum(int(r.get("count", 0)) for r in rows)
        print(f"  {name:<26} 失败语料 {len(rows):>3} 行 / {n:>3} 次"
              f"{'  ⚠ ' + res.error[:60] if res.error else ''}", flush=True)
    return {"rows": rows, "error": res.error}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=sorted(TASKS), help="只收割指定任务")
    ap.add_argument("--tier", choices=TIERS, help="只收割某一层")
    ap.add_argument("--live", action="store_true",
                    help="真跑收割（烧 key）。只有真跑的重复才是独立样本")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="真跑收割时每个任务跑几遍（回放模式下强制 1，理由见模块头）")
    ap.add_argument("--min-count", type=int, default=3, help="propose 门槛：累计失败次数")
    ap.add_argument("--min-paths", type=int, default=2, help="propose 门槛：不同的路数")
    ap.add_argument("--out", default=None, help="报告落盘目录")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    repeat = max(1, args.repeat) if args.live else 1
    if args.repeat > 1 and not args.live:
        print("（回放模式下 --repeat 无意义：模型输出已固定，重复 N 遍是同一条轨迹、"
              "同一批失败，乘 N 只是伪造证据。已按 1 遍处理。）")

    names = [args.task] if args.task else list(TASKS)
    take, skip = pick_tasks(names, tier=args.tier, live=args.live)
    for n, why in skip:
        print(f"（跳过 {n}：{why}）")
    if not take:
        print("没有可收割的任务")
        return 2

    run_id = new_run_id("harvest")
    out_dir = Path(args.out) if args.out else (ROOT / "data" / "eval_harvest" / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"收割 {len(take)} 个任务 × {repeat} 遍"
          f"（{'真跑' if args.live else '离线回放'}）→ {out_dir}")

    merged: list = []
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="hharv_dbs_") as dbd:
        for i in range(repeat):
            for name in take:
                db = Path(dbd) / f"{name}.{i}.db"
                got = _harvest_one(name, db, live=args.live, quiet=args.quiet)
                merged = merge_rows(merged, got["rows"])

    # 汇总库：**另建一个新库**再灌进去，绝不回写任何一次跑用过的库
    from agentcore.agent.learning import aggregate, propose
    from agentcore.agent.world_state import FailureMemory
    hdb = out_dir / "failures.harvest.db"
    if hdb.exists():
        hdb.unlink()
    fm = FailureMemory(hdb, source="eval")
    try:
        for row in merged:
            for _ in range(int(row.get("count", 1))):
                fm.record(row.get("fingerprint", ""), [row.get("error_class", "unknown")],
                          decision=row.get("decision", ""), detail=row.get("detail", ""))
        aggs = aggregate(fm)
    finally:
        fm.close()
    cands = propose(aggs, min_count=args.min_count, min_paths=args.min_paths)

    meta = {"live": args.live, "tasks": len(take), "repeat": repeat, "run_id": run_id,
            "rows": len(merged), "failures": sum(int(r.get("count", 0)) for r in merged)}
    (out_dir / "report.md").write_text(render_report(cands, aggs, meta), encoding="utf-8")
    (out_dir / "candidates.json").write_text(
        json.dumps([{"error_class": c.error_class, "suggestion": c.suggestion,
                     "rationale": c.rationale, "evidence": c.evidence} for c in cands],
                   ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n语料 {meta['rows']} 行 / {meta['failures']} 次失败，"
          f"聚合 {len(aggs)} 类，候选 **{len(cands)}** 条（{time.time() - t0:.0f}s）")
    for a in aggs:
        print(f"  {a.error_class:<16} {a.total:>3} 次 / {a.paths:>2} 条路"
              f"{'   → 候选' if any(c.error_class == a.error_class for c in cands) else ''}")
    print(f"\n报告 → {out_dir / 'report.md'}")
    if not cands:
        print("⚠ 一条候选都没产出 = 任务集失败面不够宽（回 V2 补任务），**别调低门槛**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
