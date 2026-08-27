"""委派并行 vs 主模型自己干：**真跑测速**，并把模型调用的时间线记下来（FR-12.3）。

    python scripts/diag_delegate_speed_realrun.py <模型档名> [--task "..."] [--cap 1200]

**两段都要给足预算**：2026-08-26 首跑设了 600s，A/B **双双被截断**，于是"墙钟 1.08×"根本不是
完成时间的对比（只有工作量那几行有效）。cap 是止损上限、不该是常态出口——看到墙钟正好贴着 cap，
先把它调大再解读数据。

为什么要它：委派并行的**唯一卖点是省墙钟时间**。如果实测反而更慢，那这个功能就是负价值，
而"感觉慢"是查不出原因的——必须看时间线：到底有没有真的并行、时间花在模型调用还是别处、
有没有被限流退避偷偷吃掉。

两段跑同一个任务：
  A 主模型自己干——把 delegate 从工具表里摘掉，它只能自己搜自己读。
  B 正常委派——提示它拆成 3 个子问题同轮并行派出去。

记的东西（都在 provider 那一层拦，不改内核）：
  · 每次模型调用的起止时刻 + 线程 → 算**真实并发度**（重叠数）。并发度≈1 就说明"并行"没发生。
  · 模型调用总时长 vs 墙钟 → 看时间是花在等模型，还是花在别处（串行等待/工具/退避）。
  · stderr 里的退避重试行 → 并发打同一个账户很容易撞限流，退避是**静默**的，只表现为"慢"。
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.bridge import Api                      # noqa: E402
import agentcore.bridge.conversation as convmod       # noqa: E402
from agentcore.config import load_config              # noqa: E402

TASK = "调研 2026 年能满足本地部署 70B 大模型的装机配置推荐，给出带来源的要点。"
CALLS: list = []          # (t0, t1, thread)
QUERIES: list = []        # (who, tool, query)——**0 命中时唯一能回答"差多远"的证据**
LOCK = threading.Lock()


def _q(who, data) -> str:
    """把检索的查询词/URL 记下来并回一段可打印的后缀。

    上一轮真跑缓存 0 命中，而日志只有工具名——于是"是键太严还是根本没重复"无从判断。
    排查性能的脚本必须把**判据本身**也记下来。
    """
    if not isinstance(data, dict):
        return ""
    name = data.get("name") or ""
    inp = data.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    q = inp.get("query") or inp.get("url") or ""
    if not q or name not in ("web_search", "web_fetch"):
        return ""
    with LOCK:
        QUERIES.append((who, name, str(q)))
    return f"  «{str(q)[:60]}»"


class Timed:
    """包一层 provider，只记时间，不改行为。"""
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, k):
        return getattr(self._inner, k)

    def stream_chat(self, *a, **kw):
        t0 = time.time()
        th = threading.current_thread().name
        try:
            for ev in self._inner.stream_chat(*a, **kw):
                yield ev
        finally:
            with LOCK:
                CALLS.append((t0, time.time(), th))


def _api(profile: str, tmp: Path, with_delegate: bool):
    cfg = load_config()
    cfg.active_model = profile
    cfg.agent.workspaces_root = str(tmp / "ws")
    cfg.agent.auto_conventions = False
    cfg.agent.max_steps = 10
    cfg.storage.db_path = str(tmp / "h.db")
    cfg.memory.enabled = False
    cfg.mcp.enabled = False
    events: list = []
    lk = threading.Lock()
    QUERIES.clear()

    t_boot = time.time()

    def emit(event, data, cid=None):
        with lk:
            events.append((event, data))
        # **实时打点**：上一版把结果攒到最后才打印，跑超时被杀 → 什么都没留下。
        # 排查性能的脚本必须边跑边吐，不然一超时就白跑。
        if event in ("tool_use", "subagent_start", "subagent_done"):
            name = (data or {}).get("name") or (data or {}).get("role") or ""
            print(f"    [{time.time() - t_boot:6.1f}s] {event:14s} {str(name)[:40]}"
                  f"{_q(0, data)}", flush=True)
        elif event == "subagent_event" and isinstance(data, dict) and data.get("event") == "tool_use":
            inner = data.get("data") or {}
            n = inner.get("name", "")
            print(f"    [{time.time() - t_boot:6.1f}s] sub#{data.get('id')} {n}"
                  f"{_q(data.get('id'), inner)}", flush=True)

    api = Api(cfg, emit=emit)
    if not with_delegate:                    # A 段：摘掉 delegate，逼主模型自己干
        conv = api.active
        conv.registry = conv.registry.filtered(lambda n: n != "delegate")
    return api, events


def _report_overlap(qs) -> None:
    """判"近似重复到什么程度"：用 web.py 自己的分词算 Jaccard，**别另发明一套**——
    要回答的是"换成模糊键能不能命中"，那就得用真会被拿来做键的那套分词。
    """
    from agentcore.tools.web import _query_terms
    searches = [(w, q) for w, n, q in qs if n == "web_search"]
    if len(searches) < 2:
        return
    pairs = []
    for i in range(len(searches)):
        for j in range(i + 1, len(searches)):
            (w1, q1), (w2, q2) = searches[i], searches[j]
            t1, t2 = _query_terms(q1), _query_terms(q2)
            if not t1 or not t2:
                continue
            jac = len(t1 & t2) / len(t1 | t2)
            if jac >= 0.4:
                pairs.append((jac, w1, q1, w2, q2))
    exact = len({" ".join(q.split()).lower() for _, q in searches})
    print(f"  查询词         共 {len(searches)} 次搜索，去重后 {exact} 个不同查询"
          f"（逐字重复 {len(searches) - exact} 次 = 精确键能命中的上限）")
    if pairs:
        pairs.sort(reverse=True)
        print(f"  近似重复       {len(pairs)} 对查询词重合度 ≥0.4（精确键抓不到的部分）：")
        for jac, w1, q1, w2, q2 in pairs[:6]:
            a = f"sub#{w1}" if w1 else "主"
            b = f"sub#{w2}" if w2 else "主"
            print(f"      {jac:.2f}  {a}«{q1[:34]}»  ×  {b}«{q2[:34]}»")


def concurrency(calls, t_start, t_end):
    """把 (起,止) 区间铺成时间线，算平均并发度与峰值。平均并发度≈1 ＝ 根本没并行。"""
    if not calls:
        return 0.0, 0, 0.0
    pts = sorted([(t0, 1) for t0, _, _ in calls] + [(t1, -1) for _, t1, _ in calls])
    cur = peak = 0
    area = 0.0
    prev = pts[0][0]
    for t, d in pts:
        area += cur * (t - prev)
        prev = t
        cur += d
        peak = max(peak, cur)
    busy = sum(t1 - t0 for t0, t1, _ in calls)
    span = max(t_end - t_start, 1e-9)
    return area / span, peak, busy


def run_one(label: str, profile: str, task: str, with_delegate: bool,
            cap_s: float = 1200) -> dict:
    CALLS.clear()
    print(f"\n■ {label}", flush=True)
    with tempfile.TemporaryDirectory(prefix="speed_") as td:
        api, events = _api(profile, Path(td), with_delegate)
        orig = convmod.build_provider
        convmod.build_provider = lambda cfg, m=None: Timed(orig(cfg, m))
        err = io.StringIO()
        t0 = time.time()
        killer = threading.Timer(cap_s, api.active.stop)   # 到点喊停，别让一段吃掉整个预算
        killer.daemon = True
        killer.start()
        try:
            with redirect_stderr(err):
                api.active.send_message(task, [])
        finally:
            killer.cancel()
            convmod.build_provider = orig
        wall = time.time() - t0
        calls = list(CALLS)
        # 同回合共享检索缓存的命中账（webcache）：第 1 条改动到底有没有真的省下重复检索，
        # 只有这两个数说了算——"膨胀降了"必须能落到具体机制上，不能只看总时长猜。
        _rc = getattr(api.active, "_retrieval_cache", None)
        cst = _rc.stats() if _rc is not None else {}
    avg, peak, busy = concurrency(calls, t0, t0 + wall)
    tools = [d.get("name") for e, d in events if e == "tool_use" and isinstance(d, dict)]
    subtools = [(d.get("data") or {}).get("name") for e, d in events
                if e == "subagent_event" and isinstance(d, dict) and d.get("event") == "tool_use"]
    subs = len([1 for e, _ in events if e == "subagent_start"])
    retries = err.getvalue().count("瞬时错误")
    answer = "".join(d for e, d in events if e == "chunk")
    print(f"  墙钟           {wall:7.1f}s")
    print(f"  模型调用       {len(calls)} 次，累计 {busy:.1f}s（占墙钟 {busy / wall * 100:.0f}%）")
    print(f"  真实并发度     平均 {avg:.2f}，峰值 {peak}   ← 1.0 = 完全串行")
    print(f"  子 Agent       {subs} 个；工具调用 主{len(tools)} + 子{len(subtools)}")
    print(f"  限流退避重试   {retries} 次")
    print(f"  检索缓存       命中 {cst.get('hits', 0)} 次 / 真跑 {cst.get('misses', 0)} 次"
          f"（命中＝省下的重复检索）")
    print(f"  最终答案       {len(answer)} 字")
    _report_overlap(list(QUERIES))
    return {"wall": wall, "calls": len(calls), "busy": busy, "avg": avg, "peak": peak,
            "subs": subs, "tools": len(tools) + len(subtools), "retries": retries,
            "answer": len(answer), "hits": cst.get("hits", 0), "miss": cst.get("misses", 0),
            "cap": cap_s}


def main() -> int:
    args = list(sys.argv[1:])
    task = TASK
    if "--task" in args:
        i = args.index("--task")
        task = args[i + 1]
        del args[i:i + 2]
    if not args:
        raise SystemExit("用法：python scripts/diag_delegate_speed_realrun.py <模型档名> [--task ...]")
    cap = 1200.0
    if "--cap" in args:
        i = args.index("--cap")
        cap = float(args[i + 1])
        del args[i:i + 2]
    profile = args[0]
    print(f"任务：{task}\n模型档：{profile}\n单段上限：{cap:.0f}s")
    a = run_one("A 段：主模型自己干（无 delegate 工具）", profile, task, False, cap_s=cap)
    b = run_one("B 段：委派并行（提示它同轮派 3 个子任务）", profile,
                task + "\n请把它拆成 3 个互相独立的子问题，**在同一轮里一次发出 3 个 delegate**"
                       "（role 用 researcher）并行调研，拿到摘要后你来汇总成最终答案。", True,
                cap_s=cap)
    print("\n" + "=" * 64)
    print(f"墙钟：A {a['wall']:.1f}s   vs   B {b['wall']:.1f}s   "
          f"→ 委派{'快' if b['wall'] < a['wall'] else '慢'} {abs(b['wall'] - a['wall']):.1f}s "
          f"（{b['wall'] / a['wall']:.2f}×）")
    print(f"模型调用次数：A {a['calls']}  vs  B {b['calls']}")
    print(f"模型调用累计时长：A {a['busy']:.1f}s  vs  B {b['busy']:.1f}s")
    print(f"真实并发度：A {a['avg']:.2f}  vs  B {b['avg']:.2f}（峰值 {a['peak']} / {b['peak']}）")
    print(f"限流退避：A {a['retries']}  vs  B {b['retries']}")
    print(f"检索缓存命中：A {a['hits']}  vs  B {b['hits']}（真跑 A {a['miss']} / B {b['miss']}）")
    for tag, r in (("A", a), ("B", b)):
        if r["wall"] >= r["cap"] - 5:
            print(f"⚠ {tag} 段墙钟贴着上限 {r['cap']:.0f}s ——**这一段被截断了**，"
                  f"完成时间不可比，调大 --cap 重跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
