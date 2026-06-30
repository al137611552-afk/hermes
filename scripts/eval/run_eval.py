"""一键跑分（FR-11.0）：固定任务集无头评测 hermes-dev 内核（真实模型，需网络与 key）。

在项目根目录运行：
    python scripts/eval/run_eval.py                 # 全部任务
    python scripts/eval/run_eval.py --task bugfix   # 只跑某个任务
    python scripts/eval/run_eval.py --model ark-deepseek  # 换模型对比

退出码：全过=0，有挂=1（可进 CI）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import run_task  # noqa: E402
from tasks import TASKS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=sorted(TASKS), help="只跑指定任务")
    ap.add_argument("--model", default=None, help="模型档案名（默认 config 的 active_model）")
    ap.add_argument("--quiet", action="store_true", help="不打印工具轨迹")
    args = ap.parse_args()

    names = [args.task] if args.task else list(TASKS)
    rows = []
    for name in names:
        task = TASKS[name]
        print(f"\n=== {name}: {task.title} ===", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"heval_{name}_") as d:
            ws = Path(d) / "ws"
            ws.mkdir()
            task.setup(ws)
            result = run_task(str(ws), task.prompt, model=args.model, verbose=not args.quiet)
            if result.error:
                passed, why = False, f"运行出错：{result.error[:200]}"
            else:
                passed, why = task.check(ws, result)
        rows.append((name, passed, why, result))
        print(f"  -> {'✅ PASS' if passed else '❌ FAIL'}  {why}"
              f"（{result.elapsed:.0f}s / 工具 {result.tool_calls} / 子任务 {result.subagents}）",
              flush=True)

    n_pass = sum(1 for _, p, _, _ in rows if p)
    print("\n" + "=" * 64)
    print(f"{'任务':<14}{'结果':<8}{'耗时':>6}{'工具':>5}{'子任务':>5}  说明")
    for name, passed, why, r in rows:
        print(f"{name:<14}{'PASS' if passed else 'FAIL':<8}{r.elapsed:>5.0f}s"
              f"{r.tool_calls:>5}{r.subagents:>5}  {why[:48]}")
    print(f"\n总分：{n_pass}/{len(rows)}")
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
