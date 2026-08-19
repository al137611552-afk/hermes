"""一键跑分（FR-11.0）：固定任务集无头评测 hermes-dev 内核（真实模型，需网络与 key）。

在项目根目录运行：
    python scripts/eval/run_eval.py                      # 全部任务，跑一遍
    python scripts/eval/run_eval.py --task bugfix        # 只跑某个任务
    python scripts/eval/run_eval.py --model ark-deepseek # 换模型对比
    python scripts/eval/run_eval.py --repeat 3 --tag base  # **对比用**：重复 3 次并打标签

**每次跑都会落 Run Record**（`data/eval_runs/<run_id>/`，ADR 0027 决策 3）——不落盘就无法回答
"这次改动到底有没有用"。对比用 `python scripts/eval/report.py <base> <head>`。

`--repeat` 不是可选的讲究：单跑一次的 pass/fail 是伯努利采样，几个百分点的变化看不出来。

退出码：全过=0，有挂=1（可进 CI）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import run_task  # noqa: E402
from record import build_record, git_sha, new_run_id, runs_root, write_record  # noqa: E402
from tasks import TASKS, TIERS, verify_nudges  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=sorted(TASKS), help="只跑指定任务")
    ap.add_argument("--model", default=None, help="模型档案名（默认 config 的 active_model）")
    ap.add_argument("--quiet", action="store_true", help="不打印工具轨迹")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="每个任务重复跑 N 次（对比/调阈值时建议 ≥3）")
    ap.add_argument("--tag", default="", help="给这次跑打标签，进 run_id 便于辨认（如 base / after-fix）")
    ap.add_argument("--out", default=None, help="Run Record 落盘目录（默认 data/eval_runs/<run_id>）")
    ap.add_argument("--no-record", action="store_true", help="不落盘（临时试跑用；正式对比别用）")
    ap.add_argument("--tier", choices=TIERS, help="只跑某一层（L1 冒烟 / L2 能力面 / L3 复合）")
    ap.add_argument("--offline", action="store_true", help="跳过需要联网的任务")
    args = ap.parse_args()

    names = [args.task] if args.task else list(TASKS)
    if args.tier:
        names = [n for n in names if TASKS[n].tier == args.tier]
    if args.offline:
        skipped = [n for n in names if TASKS[n].network]
        names = [n for n in names if not TASKS[n].network]
        if skipped:
            print(f"（--offline 跳过需联网任务：{', '.join(skipped)}）")
    if not names:
        print("没有符合条件的任务")
        return 0
    repeat = max(1, args.repeat)
    run_id = new_run_id(args.tag)
    out_dir = Path(args.out) if args.out else runs_root(ROOT) / run_id
    git = git_sha(ROOT)

    tiers = ", ".join(sorted({TASKS[n].tier for n in names}))
    print(f"run_id = {run_id}   sha = {git.get('sha') or '?'}"
          f"{' (工作树有未提交改动)' if git.get('dirty') else ''}"
          f"   任务 {len(names)} × {repeat} 次   层 [{tiers}]")

    rows = []
    for name in names:
        task = TASKS[name]
        for i in range(repeat):
            suffix = f"  [{i + 1}/{repeat}]" if repeat > 1 else ""
            print(f"\n=== {name}: {task.title}{suffix} ===", flush=True)
            started = time.time()
            with tempfile.TemporaryDirectory(prefix=f"heval_{name}_") as d:
                ws = Path(d) / "ws"
                ws.mkdir()
                task.setup(ws)
                result = run_task(str(ws), task.prompt, model=args.model, verbose=not args.quiet)
                if result.error:
                    passed, why = False, f"运行出错：{result.error[:200]}"
                else:
                    passed, why = task.check(ws, result)
            # nudge 期望核验（V2）：**误报是硬失败**——本不该响的 nudge 响了，
            # 说明 detector 在正常路径上乱插话，代价是浪费一轮 + 把模型从对的路上推开。
            n_ok, n_why, _fired = verify_nudges(result.events, task.expect_nudges)
            nudge_check = {"ok": n_ok, "why": n_why}
            if not n_ok:
                passed, why = False, (f"{why}；但{n_why}" if why else n_why)
            elif n_why:
                why = f"{why}（{n_why}）" if why else n_why
            rows.append((name, passed, why, result))
            print(f"  -> {'✅ PASS' if passed else '❌ FAIL'}  {why}"
                  f"（{result.elapsed:.0f}s / 工具 {result.tool_calls} / 子任务 {result.subagents}）",
                  flush=True)

            if not args.no_record and result.cfg is not None:
                try:
                    rec = build_record(task=name, title=task.title, prompt=task.prompt,
                                       passed=passed, why=why, result=result, cfg=result.cfg,
                                       model_name=args.model, git=git, tag=args.tag,
                                       started_at=started, tier=task.tier,
                                       nudge_check=nudge_check)
                    write_record(out_dir, rec)
                except Exception as e:  # noqa: BLE001 — 落盘失败不该毁掉这一跑的结果
                    print(f"  ⚠ Run Record 落盘失败：{type(e).__name__}: {e}")

    n_pass = sum(1 for _, p, _, _ in rows if p)
    print("\n" + "=" * 64)
    print(f"{'任务':<14}{'结果':<8}{'耗时':>6}{'工具':>5}{'子任务':>5}  说明")
    for name, passed, why, r in rows:
        print(f"{name:<14}{'PASS' if passed else 'FAIL':<8}{r.elapsed:>5.0f}s"
              f"{r.tool_calls:>5}{r.subagents:>5}  {why[:48]}")
    print(f"\n总分：{n_pass}/{len(rows)}")
    if not args.no_record:
        print(f"Run Record → {out_dir}")
        print(f"对比：python scripts/eval/report.py <另一个 run_id> {run_id}")
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
