"""「停止」真跑验证：**真起 MCP server、真出网搜索**，验今天这两处改动是不是真的成立。

    python scripts/diag_stop_realrun.py

不 mock 任何一层——单测只能证明"检查点被调用了"，证明不了"用户按下去真的马上停"。
三段各盯一件事：

 1. **MCP 取消按对话归属**（2026-08-24 真机 bug：停 A 把 B 正在跑的 codex 停了）。
    真起 `scripts/mcp_echo_server.py`，两个 owner 并发各调一次 `sleep(30)`，只停 A。
 2. **web_search 的停止令牌**（阻塞型工具的回退闸）。真搜真网：停止落在搜索途中时，
    已经搜到的结果照常给、后面那串"读正文"的往返不再发生。
 3. **web_fetch 的兜底阶梯**。停了就不再往下押 Firecrawl/浏览器，且**说清是没试而不是试了没成**。

段 2 依赖出网（搜索引擎）；段 1、3 不依赖外网。任何一段判定失败整体返回非零。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.config import MCPConfig, McpServerConfig  # noqa: E402
from agentcore.mcp_client.manager import McpManager  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.web import STOPPED_MSG, WebFetchTool, WebSearchTool  # noqa: E402

CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> bool:
    CHECKS.append((bool(ok), text))
    print(f"  {'✅' if ok else '❌'} {text}")
    return bool(ok)


# ---- 段 1：MCP 取消按对话归属 -------------------------------------------------

def section_mcp() -> None:
    print("\n■ 段 1：MCP 取消按对话归属（真起 stdio server，两个 owner 并发在飞）")
    cfg = MCPConfig(enabled=True, connect_timeout=30, call_timeout=90, servers={
        "echo": McpServerConfig(command=sys.executable,
                                args=[str(ROOT / "scripts" / "mcp_echo_server.py")],
                                trust=True),
    })
    m = McpManager(cfg)
    tools = m.start()
    if not check(any(t.tool_name == "sleep" for t in tools),
                 f"server 连上并挂载了工具（{len(tools)} 个：{[t.tool_name for t in tools]}）"):
        m.close()
        return
    out: dict = {}

    def go(owner: str) -> None:
        t0 = time.time()
        try:
            m.call("echo", "sleep", {"seconds": 30}, owner=owner)
            out[owner] = ("完成", time.time() - t0)
        except Exception as e:  # noqa: BLE001
            out[owner] = (f"{type(e).__name__}: {e}", time.time() - t0)

    threads = [threading.Thread(target=go, args=(o,), daemon=True) for o in ("A", "B")]
    for t in threads:
        t.start()
    deadline = time.time() + 15
    while len(m._inflight) < 2 and time.time() < deadline:   # 等两次调用都真的在飞
        time.sleep(0.05)
    if not check(len(m._inflight) == 2, f"两次调用都在飞（_inflight={len(m._inflight)}）"):
        m.close()
        return

    n = m.cancel_all("A")
    check(n == 1, f"只停 A：cancel_all('A') 取消掉 {n} 个（应为 1）")
    threads[0].join(timeout=10)
    check(not threads[0].is_alive(), "A 立刻结束了（没等到 call_timeout）")
    msg, dt = out.get("A", ("没结束", -1))
    check("已被用户停止" in msg, f"A 收到的是可读文案而不是 CancelledError：{msg}")
    check(0 <= dt < 10, f"A 的耗时 {dt:.1f}s ≪ 30s 的 sleep（真的被打断了）")

    time.sleep(1.0)
    check(threads[1].is_alive() and "B" not in out, "**B 仍在跑**——别人的活没被停 A 顺手带走")

    n2 = m.cancel_all()          # 不带 owner＝全停（关停/退出用）
    threads[1].join(timeout=10)
    check(n2 == 1 and not threads[1].is_alive(), f"不带 owner 全停：又取消掉 {n2} 个，B 随之结束")
    m.close()


# ---- 段 2：web_search 的停止令牌（真出网） -------------------------------------

def section_search() -> None:
    print("\n■ 段 2：web_search 停止令牌（真出网；read_top_n=3，停止落在搜索途中）")
    q = "python asyncio 教程"

    t0 = time.time()
    base = WebSearchTool(widen_pages=3, read_top_n=3).run({"query": q, "max_results": 5})
    t_base = time.time() - t0
    if not check("[已读正文]" in base, f"基线：不带令牌照常读了正文（{t_base:.1f}s）"):
        print("     ↳ 网络异常或引擎改版，段 2 结论不可信")
        return

    ev = threading.Event()
    timer = threading.Timer(0.5, ev.set)     # 搜索跑着的时候按停止
    timer.start()
    t0 = time.time()
    try:
        stopped = WebSearchTool(widen_pages=3, read_top_n=3, cancel=ev).run(
            {"query": q, "max_results": 5})
    except ToolError as e:                   # 停止落在拿到结果之前：报停也是对的
        stopped = f"[ToolError] {e}"
    t_stop = time.time() - t0
    timer.cancel()
    kept = "[已停止]" in stopped and "[已读正文]" not in stopped
    reported = STOPPED_MSG in stopped
    check(reported, f"停止被说出来了（{t_stop:.1f}s）：{stopped.splitlines()[0][:70]}")
    check(kept or stopped.startswith("[ToolError]"),
          "有货就回货、没货才报停" + ("（回了停止前搜到的结果，且没再读正文）" if kept else "（报停）"))
    check(t_stop < t_base, f"停止后明显更快：{t_stop:.1f}s < 基线 {t_base:.1f}s")

    ev2 = threading.Event()
    ev2.set()
    t0 = time.time()
    try:
        WebSearchTool(cancel=ev2).run({"query": q})
        check(False, "停止后排队进来的调用不该出网")
    except ToolError as e:
        check(STOPPED_MSG in str(e) and time.time() - t0 < 0.2,
              f"停止后排队进来的调用一个字节都没出网（{time.time() - t0:.3f}s，{e}）")


# ---- 段 3：web_fetch 的兜底阶梯 -----------------------------------------------

def section_fetch() -> None:
    print("\n■ 段 3：web_fetch 兜底阶梯（真连一个必然连不上的端口）")
    url = "http://127.0.0.1:9/nothing"       # discard 端口：真发起、真失败，不依赖外网
    seen: list[str] = []

    def reader(u: str) -> str:
        seen.append(u)
        time.sleep(2)                        # 浏览器兜底就是这么贵
        return ""

    try:
        WebFetchTool(browser_reader=reader).run({"url": url})
        check(False, "基线：本该抓不到")
    except ToolError as e:
        check(len(seen) == 1, f"基线：不带令牌时浏览器兜底真的被走了一遍（{e}）"[:120])

    seen.clear()
    ev = threading.Event()
    ev.set()
    t0 = time.time()
    try:
        WebFetchTool(browser_reader=reader, cancel=ev).run({"url": url})
        check(False, "带令牌时本该报停")
    except ToolError as e:
        dt = time.time() - t0
        check(STOPPED_MSG in str(e), f"报的是停止：{e}")
        check(not seen and dt < 1.0, f"没再起浏览器兜底（{dt:.2f}s，2s 的兜底没发生）")


def main() -> int:
    section_mcp()
    section_search()
    section_fetch()
    bad = [t for ok, t in CHECKS if not ok]
    print(f"\n{'=' * 62}\n{len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
    for t in bad:
        print(f"  ❌ {t}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
