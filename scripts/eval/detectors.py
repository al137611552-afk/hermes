"""块 V5 —— detector 计分板与阈值扫描：把"拍脑袋的数字"变成可对照的三列表。

    python scripts/eval/detectors.py                     # 计分板（当前阈值）
    python scripts/eval/detectors.py --sweep             # 阈值全谱扫描
    python scripts/eval/detectors.py --sweep --json out.json

## 为什么不是 ROADMAP 原案的"replay → 只改阈值 → 对比 Run Record"

那条路**走不通**，理由是块 V3 已经立下的限制：nudge 文案会注入进消息历史。
阈值一改、触发与否就变，**注入文案跟着变 → 请求指纹变 → cassette 当场 miss**。
也就是说"回放下改阈值再跑一遍"这件事本身自相矛盾：能跑通说明什么都没变，
真变了就跑不通。

替代办法更简单也更强：**detector 全是纯函数**（`detect_stuck_edit` / `detect_browse_nudge` /
`detect_repeated_failure` 只吃 `(calls, out_by_id, state, threshold)`），而录音回放出来的
事件流里带着每一次调用的**完整入参与完整输出**。于是可以把它们**离线重放**——
同一条既有轨迹，把阈值从 1 扫到 6，看每个值下会触发几次、在反例里误报几次。
**零模型调用、不烧 key、秒级出全谱。**

## 它能回答什么、不能回答什么（别越界）

能：**触发率**（正例里响了几个）与**误报率**（反例里误响几个）——这两列是确定性的，
因为它们只取决于"给定这条轨迹，detector 会不会响"。

不能：**触发之后模型会不会因此变好**。那需要模型真的看到新文案再走一遍，
而那正是回放做不到的（同上）。第三列只能从**实际发生过的**触发里统计：
nudge 之后那一步的失败信号是否减少。**这一列是观测，不是实验**——样本少时别当结论。

判据（ADR 0027 决策 6 的直接推论）：**误报比漏报贵**。漏报只是少一次帮助；
误报是浪费一整轮 + 用系统口吻把模型从正确的路上推开。所以扫描表里误报列优先看。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from harness import run_task  # noqa: E402
from tasks import TASKS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "cassettes"


class Call:
    """仿 provider 的 tool_use 调用对象（detector 只读 name/id/input）。"""

    __slots__ = ("id", "name", "input")

    def __init__(self, cid, name, params):
        self.id, self.name, self.input = cid, name, params or {}


# ---- 纯逻辑：把事件流重组成"步" -----------------------------------------------

def steps_from_events(events) -> "list[dict]":
    """把事件流重组成 `[{"calls": [Call], "out": {id: 输出}, "evals": {id: eval}, "nudges": [名字]}]`。

    分组规则来自主循环的结构：`_exec_calls(calls)` 一次执行一批 —— 事件流里表现为
    **连续若干个 tool_use，然后是同样多的 tool_result**，之后才是该步的 nudge 事件。
    （实测确认：同一步里两个 read_file 是 use/use/result/result，不是 use/result/use/result。）

    分批口径要对：`detect_stuck_edit` 判"本步有没有失败信号"看的是**整批**输出，
    一步拆成两步会把它判飞。
    """
    from record import NUDGE_EVENTS

    steps: list = []
    cur_calls: list = []
    pending: list = []
    out: dict = {}
    evals: dict = {}

    def flush():
        if cur_calls:
            steps.append({"calls": list(cur_calls), "out": dict(out),
                          "evals": dict(evals), "nudges": []})
        cur_calls.clear()
        pending.clear()
        out.clear()
        evals.clear()

    for name, data in events or []:
        d = data if isinstance(data, dict) else {}
        if name == "tool_use":
            if pending and not cur_calls:
                pass
            if out:            # 上一批已经收完结果 → 新的一步开始
                flush()
            c = Call(d.get("id"), str(d.get("name") or ""), d.get("input"))
            cur_calls.append(c)
            pending.append(c.id)
        elif name == "tool_result":
            out[d.get("id")] = str(d.get("output") or "")
            if d.get("eval"):
                evals[d.get("id")] = d["eval"]
        elif name in NUDGE_EVENTS and steps and not out:
            steps[-1]["nudges"].append(name)
        elif name in NUDGE_EVENTS and out:
            # nudge 紧跟在本批结果之后（还没 flush）→ 先收尾再记
            flush()
            if steps:
                steps[-1]["nudges"].append(name)
    flush()
    return steps


def replay_stuck(steps, threshold: int, trace_available: bool = True) -> int:
    """按给定阈值重放 `detect_stuck_edit`，返回会触发几次（纯函数，不改任何状态）。"""
    from agentcore.agent.loop import detect_stuck_edit

    counts: dict = {}
    nudged: set = set()
    fired = 0
    for st in steps:
        if detect_stuck_edit(st["calls"], st["out"], counts, nudged, threshold, trace_available):
            fired += 1
    return fired


def replay_browse(steps, at: int, enabled: bool = True) -> int:
    """按给定"浏览多少次才提示"重放 `detect_browse_nudge`。

    该阈值是模块常量 `_BROWSE_NUDGE_AT`，没有配置项——**这正是要先量再决定要不要挪的那种数字**。
    """
    from agentcore.agent import loop as loop_mod
    from agentcore.agent.loop import detect_browse_nudge

    old = loop_mod._BROWSE_NUDGE_AT
    loop_mod._BROWSE_NUDGE_AT = at
    try:
        state: dict = {}
        fired = 0
        for st in steps:
            if detect_browse_nudge(st["calls"], state, enabled, True):
                fired += 1
        return fired
    finally:
        loop_mod._BROWSE_NUDGE_AT = old


def replay_deadend(steps, threshold: int) -> int:
    """按给定阈值重放 `detect_repeated_failure`（本会话计数那一路；不接跨会话记忆）。"""
    from agentcore.agent.loop import detect_repeated_failure
    from agentcore.agent.world_state import WorldState

    world = WorldState()
    nudged: set = set()
    fired = 0
    for st in steps:
        if detect_repeated_failure(st["calls"], st["out"], world, None, nudged, threshold):
            fired += 1
    return fired


REPLAYERS = {
    "stuck_hint": ("stuck_edit_threshold", replay_stuck, (1, 2, 3, 4, 5)),
    "search_hint": ("_BROWSE_NUDGE_AT", replay_browse, (3, 4, 6, 8, 12)),
    "deadend_hint": ("deadend_threshold", replay_deadend, (1, 2, 3, 4, 5)),
}


# ---- 纯逻辑：三列表的口径 -----------------------------------------------------

def task_role(name: str) -> str:
    """任务在某个 detector 上的身份：正例（软观测）/ 反例（硬断言不许响）/ 无关。"""
    exp = TASKS[name].expect_nudges or {}
    if exp.get("*") is False:
        return "neg"
    return "pos" if any(v is True for v in exp.values()) else "none"


def positive_for(name: str, detector: str) -> bool:
    return TASKS[name].expect_nudges.get(detector) is True


def improved_after(steps, detector: str) -> "tuple[int, int]":
    """第三列：**触发之后那一步，失败信号是否减少**。返回 (改善次数, 触发次数)。

    判据用 `tool_result.eval.issues` 的条数——它是块B 产出的事实，不是谁的主观感受。
    只统计**实际发生过**的触发（触发后模型的反应无法离线模拟），故样本天然少：
    **这一列是观测，不是实验**，一两个样本说明不了任何事。
    """
    fired = improved = 0
    for i, st in enumerate(steps):
        if detector not in st["nudges"]:
            continue
        fired += 1
        before = sum(len((e or {}).get("issues") or []) for e in st["evals"].values())
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        after = (sum(len((e or {}).get("issues") or []) for e in nxt["evals"].values())
                 if nxt else 0)
        if after < before:
            improved += 1
    return improved, fired


def render_board(rows: "list[dict]") -> str:
    """三列表（纯函数）。误报列排在最前——**误报比漏报贵**（ADR 0027 决策 6 的直接推论）。"""
    out = ["# detector 计分板（块 V5）", "",
           "| detector | 误报（反例里误响） | 触发率（正例里响了） | 触发后改善 |",
           "|---|---|---|---|"]
    for r in rows:
        fp = f"**{r['fp']}/{r['neg_total']}**" if r["fp"] else f"{r['fp']}/{r['neg_total']}"
        imp = "—" if not r["fired"] else f"{r['improved']}/{r['fired']}"
        out.append(f"| `{r['name']}` | {fp} | {r['pos_fired']}/{r['pos_total']} | {imp} |")
    out += ["", "> 误报比漏报贵：漏报只是少一次帮助，误报是浪费一整轮 + 用系统口吻"
            "把模型从正确的路上推开。**先看误报列。**",
            "> 「触发后改善」是**观测**（只能统计实际发生过的触发），样本少时别当结论。"]
    return "\n".join(out) + "\n"


def render_sweep(sweep: dict) -> str:
    out = ["# 阈值扫描（离线纯函数重放，零模型调用）", ""]
    for det, info in sweep.items():
        out += [f"## `{det}`（旋钮 `{info['knob']}`，当前 {info['current']}）", "",
                "| 阈值 | 正例触发 | **反例误报** |", "|---|---|---|"]
        for val, s in info["values"].items():
            mark = " ←当前" if val == info["current"] else ""
            out.append(f"| {val}{mark} | {s['pos']}/{info['pos_total']} | "
                       f"**{s['neg']}**/{info['neg_total']} |")
        out.append("")
    out += ["> 扫描只回答「给定这条既有轨迹，阈值 X 下会不会响」。",
            "> **它不回答「响了之后模型会不会变好」**——那需要模型真看到新文案再走一遍，",
            "> 而回放做不到（阈值一改注入文案就变，cassette 当场 miss）。"]
    return "\n".join(out) + "\n"


# ---- IO 侧 --------------------------------------------------------------------

def collect(names, *, quiet=True) -> dict:
    """离线回放各任务，收集事件流（不烧 key）。"""
    got = {}
    for name in names:
        task = TASKS[name]
        os.environ["HERMES_CASSETTE_MODE"] = "replay"
        os.environ["HERMES_CASSETTE_DIR"] = str(CASSETTES / name)
        with tempfile.TemporaryDirectory(prefix=f"hdet_{name}_") as d:
            ws = Path(d) / "ws"
            ws.mkdir()
            os.environ["HERMES_CASSETTE_WS"] = str(ws)
            task.setup(ws)
            res = run_task(str(ws), task.prompt, model="dsv4", verbose=False,
                           max_steps=task.max_steps, max_tokens=task.max_tokens,
                           world=task.world, deny_tools=task.deny_tools,
                           autonomous=task.autonomous, crazy_rounds=task.crazy_rounds,
                           crazy_seconds=task.crazy_seconds)
        got[name] = res.events
        if not quiet:
            print(f"  {name:<30} 事件 {len(res.events):>4}", flush=True)
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true", help="除计分板外再跑阈值全谱扫描")
    ap.add_argument("--json", default=None, help="把结果同时落成 JSON")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    from record import INJECTING_NUDGES

    names = [n for n, t in TASKS.items() if t.replayable and (CASSETTES / n).is_dir()]
    skipped = [n for n in TASKS if n not in names]
    for n in skipped:
        why = "尚未录制" if TASKS[n].replayable else "不可回放"
        print(f"（跳过 {n}：{why}）")
    print(f"离线回放 {len(names)} 个任务收集轨迹（不烧 key）…")
    events = collect(names, quiet=args.quiet)
    steps = {n: steps_from_events(ev) for n, ev in events.items()}

    rows = []
    for det in INJECTING_NUDGES:
        pos = [n for n in names if positive_for(n, det)]
        neg = [n for n in names if task_role(n) == "neg" and not positive_for(n, det)]
        fired_pos = sum(1 for n in pos if det in {x for st in steps[n] for x in st["nudges"]})
        fp = sum(1 for n in neg if det in {x for st in steps[n] for x in st["nudges"]})
        imp = f_all = 0
        for n in names:
            i, f = improved_after(steps[n], det)
            imp += i
            f_all += f
        rows.append({"name": det, "fp": fp, "neg_total": len(neg), "pos_fired": fired_pos,
                     "pos_total": len(pos), "improved": imp, "fired": f_all})
    board = render_board(rows)
    print("\n" + board)

    sweep: dict = {}
    if args.sweep:
        from agentcore.config import load_config
        from agentcore.agent import loop as loop_mod
        cfg = load_config()
        current = {"stuck_hint": cfg.agent.stuck_edit_threshold,
                   "deadend_hint": cfg.agent.deadend_threshold,
                   "search_hint": loop_mod._BROWSE_NUDGE_AT}
        for det, (knob, fn, values) in REPLAYERS.items():
            pos = [n for n in names if positive_for(n, det)]
            neg = [n for n in names if task_role(n) == "neg" and not positive_for(n, det)]
            info = {"knob": knob, "current": current[det], "values": {},
                    "pos_total": len(pos), "neg_total": len(neg)}
            for v in sorted({*values, current[det]}):
                info["values"][v] = {
                    "pos": sum(1 for n in pos if fn(steps[n], v)),
                    "neg": sum(1 for n in neg if fn(steps[n], v)),
                }
            sweep[det] = info
        print(render_sweep(sweep))

    if args.json:
        Path(args.json).write_text(json.dumps({"board": rows, "sweep": sweep},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
