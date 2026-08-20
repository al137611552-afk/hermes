"""V1 评测对比报告（ADR 0027 决策 3）：吃两次跑的 Run Record，出差异表。

    python scripts/eval/report.py                      # 列出所有已记录的跑
    python scripts/eval/report.py <run_id>             # 单次跑的汇总
    python scripts/eval/report.py <base> <head>        # 两次跑对比（主用法）

**为什么要 `--repeat` 之后再对比**：单跑一次的 pass/fail 是伯努利采样，看不出几个百分点。
报告里 `pass` 一列给的是 pass@1 的**比率**（n 次重复里过了几次），不是布尔。

聚合与对比是**纯函数**（`aggregate` / `compare` / `render`），只有读盘是 IO——
故整套口径可脱离模型与网络单测。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from record import NUDGE_EVENTS, load_run, runs_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

# 展示顺序（也决定报告里的行序）。nudge/err 是动态前缀，见 flat_metrics。
_CORE_METRICS = ("pass", "nudge_violation", "elapsed", "steps", "tool_calls", "tool_retries",
                 "subagents", "subagent_failed", "errors", "tokens", "nudges_total")


def flat_metrics(record: dict) -> dict:
    """一条记录 → 扁平指标字典。**纯函数**。

    nudge 各类拆成 `nudge.<事件名>`、错误分类拆成 `err.<类>`——V5 调阈值时要按类看触发次数，
    合成一个总数就没法归因了。
    """
    m = record.get("metrics") or {}
    tok = m.get("tokens") or {}
    out = {
        "pass": 1.0 if record.get("passed") else 0.0,
        "elapsed": float(record.get("elapsed") or 0.0),
        "steps": float(m.get("steps") or 0),
        "tool_calls": float(m.get("tool_calls") or 0),
        "tool_retries": float(m.get("tool_retries") or 0),
        "subagents": float(m.get("subagents") or 0),
        "subagent_failed": float(m.get("subagent_failed") or 0),
        "errors": float(m.get("errors") or 0),
        "tokens": float(sum(int(v or 0) for v in tok.values())),
        "nudges_total": float(m.get("nudges_total") or 0),
        # 误报（V2）：某次改动**开始让 detector 在正常路径上乱插话**，必须能从 diff 里看出来。
        # 没做 nudge 核验的记录（L1 / V2 前的旧记录）记 0，不污染对比。
        "nudge_violation": 0.0 if (record.get("nudge_check") or {}).get("ok", True) else 1.0,
    }
    for n in NUDGE_EVENTS:
        out[f"nudge.{n}"] = float((m.get("nudges") or {}).get(n, 0))
    for k, v in (m.get("error_classes") or {}).items():
        out[f"err.{k}"] = float(v)
    return out


def aggregate(records) -> dict:
    """按任务归并多次重复跑 → `{task: {"n": 次数, 指标名: 均值}}`。**纯函数**。

    均值而非合计：`--repeat` 次数不同的两次跑也要能比。
    """
    by_task: dict = {}
    for r in records:
        by_task.setdefault(r.get("task", "?"), []).append(r)
    out = {}
    for task, rs in by_task.items():
        flats = [flat_metrics(r) for r in rs]
        keys = sorted({k for f in flats for k in f})
        agg = {"n": float(len(rs))}
        for k in keys:
            vals = [f.get(k, 0.0) for f in flats]
            agg[k] = sum(vals) / len(vals)
        out[task] = agg
    return out


def compare(base: dict, head: dict) -> dict:
    """两份聚合 → `{task: [(指标, base, head, delta), ...]}`。**纯函数**。

    只留**有差异**的指标行（含一边为 0 另一边非 0），否则一屏全是 0 差异、真正的变化被淹没。
    两边都没有的任务不会凭空出现；只在一边出现的任务照列，另一边记 None。
    """
    tasks = sorted(set(base) | set(head))
    out: dict = {}
    for t in tasks:
        b, h = base.get(t), head.get(t)
        rows = []
        keys = sorted({k for d in (b or {}, h or {}) for k in d if k != "n"},
                      key=lambda k: (_CORE_METRICS.index(k) if k in _CORE_METRICS else 99, k))
        for k in keys:
            bv = None if b is None else b.get(k, 0.0)
            hv = None if h is None else h.get(k, 0.0)
            if bv is not None and hv is not None and abs(hv - bv) < 1e-9:
                continue                       # 无差异不占版面
            delta = None if (bv is None or hv is None) else hv - bv
            rows.append((k, bv, hv, delta))
        out[t] = rows
    return out


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}".rstrip("0").rstrip(".") if v % 1 else f"{int(v)}"


def render(cmp_result: dict, base_id: str, head_id: str) -> str:
    """差异表文本。**纯函数**。"""
    lines = [f"评测对比：{base_id}  ->  {head_id}", "=" * 72]
    changed = 0
    for task, rows in cmp_result.items():
        if not rows:
            lines.append(f"\n{task}：无差异")
            continue
        changed += 1
        lines.append(f"\n{task}")
        lines.append(f"  {'指标':<26}{'base':>10}{'head':>10}{'delta':>10}")
        for k, bv, hv, d in rows:
            arrow = "" if d is None else ("  ↑" if d > 0 else "  ↓")
            lines.append(f"  {k:<26}{_fmt(bv):>10}{_fmt(hv):>10}{_fmt(d):>10}{arrow}")
    lines.append("\n" + "=" * 72)
    lines.append(f"{changed}/{len(cmp_result)} 个任务有差异")
    return "\n".join(lines)


def render_single(agg: dict, run_id: str) -> str:
    """单次跑的汇总。**纯函数**。"""
    lines = [f"评测汇总：{run_id}", "=" * 60,
             f"{'任务':<18}{'n':>3}{'pass':>7}{'步数':>7}{'工具':>6}{'nudge':>7}{'耗时':>8}"]
    for task, a in sorted(agg.items()):
        lines.append(f"{task:<18}{int(a['n']):>3}{a.get('pass', 0):>7.2f}"
                     f"{a.get('steps', 0):>7.1f}{a.get('tool_calls', 0):>6.1f}"
                     f"{a.get('nudges_total', 0):>7.1f}{a.get('elapsed', 0):>7.0f}s")
    if agg:
        overall = sum(a.get("pass", 0) for a in agg.values()) / len(agg)
        lines += ["=" * 60, f"总 pass@1 均值：{overall:.2f}"]
    return "\n".join(lines)


# ---- IO --------------------------------------------------------------------

def _resolve(run_id: str) -> Path:
    p = Path(run_id)
    return p if p.is_dir() else runs_root(ROOT) / run_id


def main() -> int:
    ap = argparse.ArgumentParser(description="评测 Run Record 汇总/对比")
    ap.add_argument("runs", nargs="*", help="run_id（0 个=列出全部，1 个=汇总，2 个=对比）")
    args = ap.parse_args()

    root = runs_root(ROOT)
    if not args.runs:
        if not root.is_dir():
            print(f"还没有任何评测记录（{root} 不存在）。先跑：python scripts/eval/run_eval.py --out")
            return 0
        for d in sorted(root.iterdir()):
            if d.is_dir():
                recs = load_run(d)
                n_pass = sum(1 for r in recs if r.get("passed"))
                sha = (recs[0].get("git", {}).get("sha") if recs else None) or "?"
                print(f"  {d.name:<28} {n_pass}/{len(recs)} 过   sha={sha}")
        return 0

    if len(args.runs) == 1:
        d = _resolve(args.runs[0])
        recs = load_run(d)
        if not recs:
            print(f"没读到记录：{d}")
            return 1
        print(render_single(aggregate(recs), d.name))
        return 0

    a, b = _resolve(args.runs[0]), _resolve(args.runs[1])
    ra, rb = load_run(a), load_run(b)
    if not ra or not rb:
        print(f"记录不全：{a}={len(ra)} 条 / {b}={len(rb)} 条")
        return 1
    # 配置/代码版本不同要显式提醒——不然对比出的差异会被误读成"改动的效果"
    for label, recs in (("base", ra), ("head", rb)):
        g = recs[0].get("git", {})
        if g.get("dirty"):
            print(f"⚠ {label} 跑在**有未提交改动**的工作树上（sha={g.get('sha')}），可比性打折")
    if ra[0].get("config") != rb[0].get("config"):
        print("⚠ 两次跑的**配置快照不同**——差异未必来自你以为的那个改动")
    # 换了模型的两次跑，差异里混着模型能力差，指标对比基本没有意义——这条要喊得比配置差异更响
    ma, mb = ra[0].get("model") or {}, rb[0].get("model") or {}
    if ma.get("model_id") != mb.get("model_id"):
        print(f"⚠ **两次跑用的不是同一个模型**（{ma.get('model_id')} -> {mb.get('model_id')}）："
              f"这种对比只能看模型差异，不能用来判断代码改动的效果")
    elif not ma.get("model_id"):
        print("⚠ 记录里**没有真实 model_id**（跑的时候没配模型档案？）——可比性存疑")
    sa, sb = (ra[0].get("git") or {}).get("sha"), (rb[0].get("git") or {}).get("sha")
    if sa and sb and sa == sb:
        print(f"ℹ 两次跑是**同一个 commit**（{sa}）：差异只可能来自配置、环境或模型随机性")
    print(render(compare(aggregate(ra), aggregate(rb)), a.name, b.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
