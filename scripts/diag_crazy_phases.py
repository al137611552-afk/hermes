"""crazy 阶段化（块1–4）真模型自测：拆阶段 → 逐阶段做+验收 → 阶段后重规划 → 收尾过验收门。

用法（项目根目录下，需要 config.yaml 里当前模型的 key 可用、能联网）：
    python scripts/diag_crazy_phases.py [--rounds 8]

为什么要这个：块1–4 的单测喂的都是**脚本化假回复**（`[[PHASE_DONE: …]]` 是测试写死的），
只能证明"外层循环收到这个标记会怎么走"，证明不了**真模型会不会按阶段走、会不会发这个标记**。
本项目反复吃过这个亏（单测绕过真实入口 → 掩盖路径级 bug），故补这一环。

逐项打 [PASS]/[FAIL]，末行 RESULT。用临时目录，不污染 data/。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

from agentcore.config import load_config          # noqa: E402
from agentcore.bridge.api import Api              # noqa: E402

GOAL_SMALL = ("做一个带测试的命令行待办工具 todo.py：支持 add / list / done 三个子命令，"
              "数据存 JSON 文件；用 pytest 写测试，覆盖数据层和 CLI。")
# 大目标：单轮步数吃不下，才可能真的停在阶段边界（块4 的触发前提）
GOAL_BIG = ("做一个 Markdown 静态站点生成器 ssg.py（纯标准库）："
            "P1 Markdown 解析（标题/列表/代码块/链接/强调）；P2 模板渲染（页面模板 + 首页索引）；"
            "P3 CLI（build/serve 两个子命令 + 增量构建：只重建变化的文件）。"
            "每个阶段都要有 pytest 测试并跑绿。")

events: list = []
_results: list = []


def check(name, cond, extra=""):
    _results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def emit(kind, data, cid=None):
    events.append((kind, data))
    if kind in ("crazy_round", "crazy_replan", "crazy_gate", "crazy_need", "crazy_done"):
        print(f"  · {kind}: {str(data)[:160]}")
    if kind == "tasks_updated":
        items = (data or {}).get("tasks") or []
        print(f"  · 任务清单({len(items)}): "
              + " | ".join(f"{t.get('status', '?')[:4]}:{str(t.get('content', ''))[:28]}"
                           for t in items[:6]))
    if kind == "error":
        print("  · error:", str(data)[:200])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--steps", type=int, default=20, help="单轮步数上限（调小才逼得出阶段边界）")
    ap.add_argument("--big", action="store_true", help="用更大的多阶段目标")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="crazy-phases-"))
    ws = tmp / "proj"
    ws.mkdir()

    cfg = load_config()
    cfg.agent.workspace = str(ws)
    cfg.agent.per_session_workspace = False
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.max_steps = args.steps
    cfg.agent.screenshot = False
    cfg.mcp.enabled = False
    cfg.memory.enabled = False
    cfg.storage.db_path = str(tmp / "h.db")
    cfg.agent.permissions.allow = [f"run_{cfg.agent.shell}(*)"]   # headless：免确认
    cfg.agent.crazy_gate_ask = False          # 无人值守：撞岔路自己定，别卡住等人
    cfg.agent.crazy_replan = True             # 块4 就是要验它
    cfg.web.enabled = False                   # 本任务不需要联网，省时间

    GOAL = GOAL_BIG if args.big else GOAL_SMALL
    print(f"工作区：{ws}\n目标：{GOAL}\n预算：{args.rounds} 轮\n")
    api = Api(cfg, emit=emit)
    t0 = time.time()
    try:
        out = api.active.run_autonomous(GOAL, max_rounds=args.rounds)
    finally:
        api.close()
    print(f"\n外层循环返回：{out}  用时 {time.time() - t0:.0f}s\n")

    prompts = [str(d) for k, d in events if k == "chunk"]           # 仅用于兜底
    rounds = [d for k, d in events if k == "crazy_round"]
    replans = [d for k, d in events if k == "crazy_replan"]
    needs = [(d or {}).get("need") for k, d in events if k == "crazy_need"]
    tasks_snapshots = [(d or {}).get("tasks") or [] for k, d in events if k == "tasks_updated"]

    # 块1：开局把目标拆成多个阶段，且阶段带验收标准
    first_tasks = tasks_snapshots[0] if tasks_snapshots else []
    check("块1 开局拆出多阶段任务清单", len(first_tasks) >= 2, f"{len(first_tasks)} 项")
    joined = " ".join(str(t.get("content", "")) for t in first_tasks)
    check("块1 阶段带验收/测试字样",
          bool(re.search(r"验收|测试|pytest|通过|tests?/|test_", joined, re.I)), joined[:90])

    # 块4：真模型确实发出了阶段完成标记 → 触发重规划
    check("块4 出现 PHASE_DONE 并触发重规划", bool(replans) or "phase_done" in needs,
          f"replan {len(replans)} 次，need 序列 {needs}")

    # 阶段推进：任务清单确实在演进（有项目从 pending 变 completed）
    done_counts = [sum(1 for t in s if t.get("status") == "completed") for s in tasks_snapshots]
    check("阶段逐个推进（完成数递增）", bool(done_counts) and max(done_counts) >= 1,
          f"完成数轨迹 {done_counts}")

    # 产物与验收：真的产出了代码和测试，且测试能跑过
    files = sorted(p.name for p in ws.rglob("*.py"))
    main_py = "ssg.py" if args.big else "todo.py"
    check(f"产出了 {main_py}", any(f == main_py for f in files), str(files[:8]))
    check("产出了测试文件", any(f.startswith("test") for f in files), str(files[:8]))
    if any(f.startswith("test") for f in files):
        import subprocess
        r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(ws),
                           capture_output=True, text=True, timeout=180)
        check("独立复跑测试全绿（不信模型自报）", r.returncode == 0,
              (r.stdout or r.stderr).strip().splitlines()[-1][:100] if (r.stdout or r.stderr) else "")

    # 故意把单轮步数压低（--steps）时跑不完是必然的，那时只要产物可用即可，不苛求 goal_reached
    if args.steps >= 20:
        check("收尾原因是达成目标", out.get("reason") == "goal_reached", str(out.get("reason")))
    else:
        print(f"[INFO] 收尾原因 {out.get('reason')}（--steps={args.steps} 刻意压低预算，不计入判定）")

    ok = all(_results)
    print("\nRESULT:", "ALL PASS" if ok else f"{sum(_results)}/{len(_results)} PASS")
    print(f"（工作区留在 {ws} 供查看）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
