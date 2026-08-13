"""ADR 0021 块1：工具产物存储 + 摘要/判据纯逻辑 + 清理 + 检索不被污染（无网络）。

运行：python tests/test_artifacts.py
"""
from __future__ import annotations

import inspect
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/_shellenv.py

from agentcore.artifacts import (  # noqa: E402
    DEFAULT_THRESHOLD, HEAD_LINES, TAIL_LINES, MAX_SUMMARY_CHARS,
    ArtifactSink, ArtifactStore, format_with_handle, prune_plan, should_artifact,
    summarize_for_context,
)
from _shellenv import RUN_TOOL, SHELL, python_c  # noqa: E402
from agentcore.tools.search import GlobSearchTool, GrepSearchTool  # noqa: E402
from agentcore.tools.shell import RunShellTool  # noqa: E402
from agentcore.tools.web import WebFetchTool  # noqa: E402


# ---- 判据（纯逻辑）----------------------------------------------------------


def test_should_artifact_needs_truncation():
    # 判「发生截断」而非「输出够大」：没截断的大输出模型已看全，落盘是纯开销
    assert should_artifact(500_000, 500_000) is False
    assert should_artifact(30_000, 30_000) is False
    assert should_artifact(417_000, 200_000) is True


def test_should_artifact_threshold_is_a_floor():
    # 阈值降级成防抖下限：挡住 max_chars=500 这类小截断产生的碎片产物
    assert should_artifact(1_200, 500) is False
    assert should_artifact(DEFAULT_THRESHOLD, 500) is True
    assert should_artifact(DEFAULT_THRESHOLD - 1, 500) is False
    # web_fetch 的真实场景：抓到 46,000、cap 20,000 —— 原判据在这会自相矛盾，新判据落产物
    assert should_artifact(46_000, 20_000) is True


# ---- 摘要（纯逻辑）----------------------------------------------------------


def test_summarize_keeps_head_and_tail():
    text = "\n".join(f"line{i}" for i in range(500))
    s = summarize_for_context(text)
    assert "line0" in s and f"line{HEAD_LINES - 1}" in s      # 头
    assert "line499" in s and f"line{500 - TAIL_LINES}" in s  # 尾（结论通常在这）
    assert f"line{HEAD_LINES + 5}" not in s                   # 中间被省略
    assert f"省略 {500 - HEAD_LINES - TAIL_LINES:,} 行" in s


def test_summarize_short_text_unchanged():
    text = "a\nb\nc"
    assert summarize_for_context(text) == text


def test_summarize_clips_giant_single_line():
    # 单行 40 万字符的 JSON：不做行内截断的话"摘要"会和产物一样大
    s = summarize_for_context("x" * 400_000)
    assert len(s) <= MAX_SUMMARY_CHARS + 200


def test_summarize_caps_total_chars():
    text = "\n".join("y" * 1500 for _ in range(500))   # 头尾行本身就很大
    s = summarize_for_context(text)
    assert len(s) <= MAX_SUMMARY_CHARS + 200


# ---- 清理计划（纯逻辑）------------------------------------------------------


def test_prune_plan_expired_and_oldest_first():
    now = 1_000_000.0
    rows = [
        {"id": "art_0001", "bytes": 10, "created_at": now - 30 * 86400},  # 过期
        {"id": "art_0002", "bytes": 10, "created_at": now - 1 * 86400},
        {"id": "art_0003", "bytes": 10, "created_at": now},
    ]
    assert prune_plan(rows, max_total_bytes=0, keep_days=7, now=now) == ["art_0001"]
    # 总量超限：最旧优先删到不超（0001 已因过期入列，再删 0002 才降到 10 <= 15）
    plan = prune_plan(rows, max_total_bytes=15, keep_days=7, now=now)
    assert plan == ["art_0001", "art_0002"]
    # 两个维度都不限 -> 什么都不删
    assert prune_plan(rows, max_total_bytes=0, keep_days=0, now=now) == []


# ---- 句柄拼装 ---------------------------------------------------------------


def test_format_with_handle_tells_model_not_to_rerun(tmp: Path):
    store = ArtifactStore(tmp)
    art = store.put("a\nb\nc", tool="run_bash", origin="pytest -q")
    out = format_with_handle("摘要正文", art)
    assert art.id in out and art.rel in out
    assert "摘要正文" in out
    assert "不必重跑" in out and "grep_search" in out       # 风险1（模型不下钻）的缓解


# ---- 存储 / 台账 / 清理（IO）-------------------------------------------------


def test_put_writes_file_and_ledger(tmp: Path):
    store = ArtifactStore(tmp)
    text = "\n".join(f"row{i}" for i in range(1000))
    art = store.put(text, tool="run_bash", origin="pytest -q", session_id=7)

    p = tmp / art.rel
    assert p.is_file() and p.read_text(encoding="utf-8") == text   # 无损
    assert art.rel.startswith(".hermes/artifacts/") and art.rel.endswith(".txt")
    assert art.chars == len(text) and art.lines == 1000

    rows = store.list()
    assert len(rows) == 1 and rows[0]["id"] == art.id
    assert rows[0]["bytes"] == p.stat().st_size
    assert rows[0]["origin"] == "pytest -q" and rows[0]["session_id"] == 7
    # 台账放在产物目录外。比对要用 resolve 后的 tmp：ArtifactStore 存的是 workspace.resolve()，
    # 而 Windows 临时目录是 8.3 短名（C:\Users\RUNNER~1\...），resolve 会展开成长名、两边对不上。
    assert store.ledger_path == tmp.resolve() / ".hermes" / "artifacts.json"


def test_ids_increment_and_survive_reopen(tmp: Path):
    a = ArtifactStore(tmp).put("x", tool="t").id
    b = ArtifactStore(tmp).put("y", tool="t").id      # 新实例：从台账续号，不覆盖
    assert (a, b) == ("art_0001", "art_0002")
    assert len(ArtifactStore(tmp).list()) == 2


def test_self_gitignore_created(tmp: Path):
    # 绑定真实项目时 .hermes/ 会冒进 git_status 和「改动」面板 -> 自我忽略
    store = ArtifactStore(tmp)
    store.put("x", tool="t")
    assert (tmp / ".hermes" / ".gitignore").read_text(encoding="utf-8").strip() == "*"
    assert not (tmp / ".gitignore").exists()          # 不动用户仓库根的 .gitignore


def test_list_filters_by_session_and_counts_others(tmp: Path):
    store = ArtifactStore(tmp)
    store.put("a", tool="t", session_id=1)
    store.put("b", tool="t", session_id=2)
    store.put("c", tool="t", session_id=2)
    assert [r["session_id"] for r in store.list(session_id=2)] == [2, 2]
    assert store.others_count(2) == 1                 # 「本工作区另有 N 条历史产物」
    assert len(store.list()) == 3                     # 按 id/路径读不设限


def test_prune_by_days_deletes_file_and_row(tmp: Path):
    store = ArtifactStore(tmp, keep_days=1)
    old = store.put("old", tool="t")
    fresh = store.put("fresh", tool="t")
    # 把一条改成 8 天前
    import json
    data = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    data[old.id]["created_at"] = time.time() - 8 * 86400
    store.ledger_path.write_text(json.dumps(data), encoding="utf-8")

    assert store.prune() == [old.id]
    assert not (tmp / old.rel).exists() and (tmp / fresh.rel).exists()
    assert [r["id"] for r in store.list()] == [fresh.id]


def test_prune_drops_rows_for_hand_deleted_files(tmp: Path):
    store = ArtifactStore(tmp)
    art = store.put("x", tool="t")
    (tmp / art.rel).unlink()                          # 用户手删产物
    store.prune()
    assert store.list() == []


def test_corrupt_ledger_is_tolerated(tmp: Path):
    store = ArtifactStore(tmp)
    store.put("x", tool="t")
    store.ledger_path.write_text("{ 坏掉的 JSON", encoding="utf-8")
    assert store.list() == []
    assert store.put("y", tool="t").id == "art_0001"  # 台账没了就重新发号，不崩


def test_tee_writes_while_running(tmp: Path):
    """后台进程 tee：环形缓冲会丢最旧，所以要边收边落盘（ADR 0021 §7）。"""
    store = ArtifactStore(tmp)
    tee = store.open_tee(tool="run_bash", origin="npm run dev")
    assert tee is not None
    tee.write("line1\n")
    tee.write("line2\n")
    assert (tmp / tee.artifact.rel).read_text(encoding="utf-8") == "line1\nline2\n"  # 未 close 已可读
    tee.close()
    assert tee.artifact.chars == 12 and tee.artifact.lines == 2
    row = store.list()[0]
    assert row["id"] == tee.artifact.id and row["chars"] == 12 and row["bytes"] == 12
    tee.write("after close")                          # 关后再写不炸、不落盘
    assert (tmp / tee.artifact.rel).read_text(encoding="utf-8") == "line1\nline2\n"


def test_concurrent_puts_get_distinct_ids(tmp: Path):
    """同轮并行工具（parallel_safe）可能同时落产物：发号与台账要串行。"""
    import threading
    store = ArtifactStore(tmp)
    ids: list[str] = []
    lock = threading.Lock()

    def worker(i: int):
        art = store.put(f"payload-{i}", tool="t")
        with lock:
            ids.append(art.id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 8 and len(store.list()) == 8


# ---- 检索不被产物污染，但显式能下钻 -----------------------------------------


def _seed(tmp: Path) -> ArtifactStore:
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    (tmp / "src" / "app.py").write_text("def run():\n    return 'FAILED marker'\n", encoding="utf-8")
    store = ArtifactStore(tmp)
    store.put("noise\n" * 100 + "FAILED test_x\n", tool="run_bash", origin="pytest")
    return store


def test_grep_skips_artifacts_by_default(tmp: Path):
    _seed(tmp)
    out = GrepSearchTool(tmp).run({"pattern": "FAILED"})
    assert "src/app.py" in out
    assert ".hermes" not in out          # 40 万字符的日志不该污染每一次全库 grep


def test_grep_reaches_artifact_when_path_is_explicit(tmp: Path):
    store = _seed(tmp)
    art = store.list()[0]
    out = GrepSearchTool(tmp).run({"pattern": "FAILED", "path": ".hermes/artifacts"})
    assert art["id"] in out
    # ADR §4 的写法：直接给单个文件
    out2 = GrepSearchTool(tmp).run({"pattern": "FAILED", "path": art["rel"]})
    assert art["id"] in out2


def test_grep_explicit_artifact_ignores_project_gitignore(tmp: Path):
    store = _seed(tmp)
    (tmp / ".gitignore").write_text("*.txt\n.hermes\n", encoding="utf-8")
    art = store.list()[0]
    out = GrepSearchTool(tmp).run({"pattern": "FAILED", "path": art["rel"]})
    assert art["id"] in out              # 用户仓库忽略 *.log/*.txt 不该挡住显式下钻


def test_grep_missing_path_still_errors(tmp: Path):
    from agentcore.tools.base import ToolError
    try:
        GrepSearchTool(tmp).run({"pattern": "x", "path": "nope"})
        assert False, "应当报错"
    except ToolError as e:
        assert "不存在" in str(e)


def test_glob_skips_artifacts_unless_explicit(tmp: Path):
    _seed(tmp)
    assert ".hermes" not in GlobSearchTool(tmp).run({"pattern": "**/*"})
    out = GlobSearchTool(tmp).run({"pattern": ".hermes/artifacts/*.txt"})
    assert ".hermes/artifacts/art_0001.txt" in out


def test_bm25_index_skips_artifacts(tmp: Path):
    from agentcore.retrieval import search_code
    _seed(tmp)
    out = search_code(tmp, "FAILED marker", limit=5)
    assert "src/app.py" in out and ".hermes" not in out


# ---- 块2：接前台 shell 与 web_fetch ------------------------------------------


def _sink(tmp: Path, **kw) -> ArtifactSink:
    return ArtifactSink(ArtifactStore(tmp, **kw), session_id_fn=lambda: 42)


def test_shell_small_output_has_no_artifact(tmp: Path):
    """正常大小的命令：不落产物、无任何间接层（阈值以下零开销）。"""
    tool = RunShellTool(tmp, shell=SHELL, timeout=30, artifacts=_sink(tmp))
    out = tool.run({"command": "echo hello"})
    assert "hello" in out and "产物" not in out
    assert ArtifactStore(tmp).list() == []


def test_shell_overflow_keeps_full_output_in_artifact(tmp: Path):
    """真跑一条输出超上限的命令：内存里被截断，产物里是完整的。"""
    from agentcore.tools import shell as shell_mod
    sink = _sink(tmp)
    tool = RunShellTool(tmp, shell=SHELL, timeout=60, artifacts=sink)
    # 30 万字符 > 20 万上限；末尾放个标记，验证"被截掉的尾部"确实进了产物
    cmd = python_c("import sys;sys.stdout.write('A'*300000);sys.stdout.write('TAIL_MARKER')")
    out = tool.run({"command": cmd})

    # 新行为：回「摘要（头+尾）+ 句柄」，而不是 20 万字符的头部截断
    assert "TAIL_MARKER" in out                          # 结论在尾部——老行为恰恰会把它丢掉
    assert "art_0001" in out and "不必重跑" in out
    assert len(out) < 20_000                             # 工具结果从 ~20 万字符压到几 K
    rows = ArtifactStore(tmp).list()
    assert len(rows) == 1 and rows[0]["session_id"] == 42
    full = (tmp / rows[0]["rel"]).read_text(encoding="utf-8")
    assert len(full) == 300000 + len("TAIL_MARKER")
    assert full.endswith("TAIL_MARKER")                  # 被丢弃的部分不再永久消失
    assert full.startswith("A" * 1000)                   # 头部（已在内存里的）也补进了产物
    assert shell_mod._MAX_OUTPUT_CHARS == 200_000        # 上限本身没被改动


def test_shell_without_sink_keeps_old_behaviour(tmp: Path):
    """没接产物入口＝行为同 3.53：截断 + 老提示，不提产物。"""
    tool = RunShellTool(tmp, shell=SHELL, timeout=60)
    out = tool.run({"command": python_c("import sys;sys.stdout.write('A'*250000)")})
    assert "已截断" in out and "产物" not in out


def test_shell_tee_self_destructs_when_below_threshold(tmp: Path):
    """阈值设得比上限还高时，tee 收尾自我销毁，提示回落到老文案（不给死句柄）。"""
    tool = RunShellTool(tmp, shell=SHELL, timeout=60,
                        artifacts=_sink(tmp, threshold=500_000))
    out = tool.run({"command": python_c("import sys;sys.stdout.write('A'*250000)")})
    assert "已截断" in out and "产物" not in out
    assert ArtifactStore(tmp).list() == []
    assert not list((tmp / ".hermes" / "artifacts").glob("*"))


def _patch_http(monkey_body: str):
    """替掉 HTTP：只测"抓到的原文被 cap 掉之后去哪了"。返回 (模块, 原函数) 供还原。"""
    from agentcore.tools import web as web_mod
    orig = web_mod._http_get
    web_mod._http_get = lambda url, timeout: ("https://example.com/page", monkey_body, "text/plain")
    return web_mod, orig


def test_web_fetch_artifacts_the_precap_text(tmp: Path):
    """web_fetch 的 cap 默认正好等于阈值——判据必须量**原文**，否则永远卡边界。"""
    web_mod, orig = _patch_http("正文" * 23_000)   # 46,000 字符原文
    try:
        tool = WebFetchTool(timeout=5, max_chars=20_000, artifacts=_sink(tmp))
        out = tool.run({"url": "https://example.com/page"})
    finally:
        web_mod._http_get = orig
    assert "已截断至 20000 字符" in out
    assert "[产物 art_0001]" in out and "别重抓" in out
    rows = ArtifactStore(tmp).list()
    assert len(rows) == 1 and rows[0]["tool"] == "web_fetch"
    assert rows[0]["origin"] == "https://example.com/page"
    assert len((tmp / rows[0]["rel"]).read_text(encoding="utf-8")) == 46_000


def test_web_fetch_small_page_no_artifact(tmp: Path):
    web_mod, orig = _patch_http("短正文，没超 cap")
    try:
        out = WebFetchTool(timeout=5, max_chars=20_000, artifacts=_sink(tmp)).run(
            {"url": "https://example.com/page"})
    finally:
        web_mod._http_get = orig
    assert "产物" not in out and ArtifactStore(tmp).list() == []


def test_web_fetch_focus_excerpt_also_keeps_full_text(tmp: Path):
    """给了 focus 只回摘录，但全文照样存下来——摘录漏了的部分还能下钻。"""
    body = "无关段落。\n" * 5000 + "关键结论：价格是 199 元。\n" + "无关段落。\n" * 5000
    web_mod, orig = _patch_http(body)
    try:
        out = WebFetchTool(timeout=5, max_chars=2_000, artifacts=_sink(tmp)).run(
            {"url": "https://example.com/page", "focus": "价格"})
    finally:
        web_mod._http_get = orig
    assert "已按 focus" in out and "[产物 art_0001]" in out
    assert "199 元" in (tmp / ArtifactStore(tmp).list()[0]["rel"]).read_text(encoding="utf-8")


def test_registry_injects_sink_into_shell_and_fetch(tmp: Path):
    """接线：build_registry 把同一个 sink 交给 run_<shell> 和 web_fetch。"""
    from agentcore.config import WebConfig
    from agentcore.tools.registry import build_registry
    sink = _sink(tmp)
    reg = build_registry(tmp, shell=SHELL, web=WebConfig(), artifacts=sink)
    assert reg.get(RUN_TOOL)._artifacts is sink
    assert reg.get("web_fetch")._artifacts is sink
    # 不传就是 None＝老行为
    reg2 = build_registry(tmp, shell=SHELL, web=WebConfig())
    assert reg2.get(RUN_TOOL)._artifacts is None and reg2.get("web_fetch")._artifacts is None


# ---- 块3：后台进程读线程 tee -------------------------------------------------


def _wait(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_background_tee_keeps_output_dropped_by_ring_buffer(tmp: Path):
    """环形缓冲把最旧输出冲掉了，产物里仍在——这是唯一"重跑也拿不回来"的数据。"""
    from agentcore.tools import procs as procs_mod
    sink = _sink(tmp)
    mgr = procs_mod.ProcessManager(artifacts=sink)
    # 打印远超环形缓冲上限（20 万字符）的量，开头放个标记
    code = ("import sys\n"
            "sys.stdout.write('EARLY_MARKER\\n')\n"
            "for i in range(3000): sys.stdout.write('x'*100 + '\\n')\n"
            "sys.stdout.flush()\n")
    entry = mgr.start([sys.executable, "-c", code], str(tmp), "python3 -c <spam>")
    assert _wait(lambda: entry.proc.poll() is not None and entry.trimmed)
    time.sleep(0.3)   # 让读线程收尾（tee.close）

    assert "EARLY_MARKER" not in entry.buffer          # 早期输出确实被冲掉了
    r = mgr.read(entry.id)
    assert r["trimmed"] and r["artifact_id"] == "art_0001"
    full = (tmp / r["artifact_rel"]).read_text(encoding="utf-8")
    assert full.startswith("EARLY_MARKER")             # 但产物里从第一行起完整
    assert len(full) > procs_mod.MAX_BUF_CHARS
    mgr.kill_all()


def test_background_read_tool_gives_handle_only_when_data_lost(tmp: Path):
    """没丢数据时不提产物（不给日常输出添噪音）；丢了才给句柄。"""
    from agentcore.tools.procs import ProcessManager, ProcessOutputTool
    mgr = ProcessManager(artifacts=_sink(tmp))
    entry = mgr.start([sys.executable, "-c", "print('hi')"], str(tmp), "python3 -c print")
    assert _wait(lambda: entry.proc.poll() is not None)
    time.sleep(0.3)
    out = ProcessOutputTool(mgr).run({"id": entry.id})
    assert "hi" in out and "产物" not in out
    # 产物文件本身仍在（句柄给出去就不能中途消失），只是没在提示里吵
    assert len(ArtifactStore(tmp).list()) == 1
    mgr.kill_all()


def test_background_without_sink_unchanged(tmp: Path):
    from agentcore.tools.procs import ProcessManager
    mgr = ProcessManager()
    entry = mgr.start([sys.executable, "-c", "print('hi')"], str(tmp), "python3 -c print")
    assert _wait(lambda: entry.proc.poll() is not None)
    time.sleep(0.2)
    r = mgr.read(entry.id)
    assert r["new_output"].strip() == "hi" and r["artifact_id"] == ""
    assert not (tmp / ".hermes").exists()
    mgr.kill_all()


def test_background_artifact_readable_while_running(tmp: Path):
    """进程还在跑时产物就能读（append + flush），不必等它退出。"""
    from agentcore.tools.procs import ProcessManager
    mgr = ProcessManager(artifacts=_sink(tmp))
    code = ("import sys,time\n"
            "sys.stdout.write('LIVE\\n'); sys.stdout.flush()\n"
            "time.sleep(30)\n")
    entry = mgr.start([sys.executable, "-c", code], str(tmp), "long runner")
    try:
        assert _wait(lambda: entry.tee is not None
                     and (tmp / entry.tee.artifact.rel).read_text(encoding="utf-8").strip() == "LIVE")
        assert entry.proc.poll() is None            # 仍在运行
    finally:
        mgr.kill_all()


def _run_all():
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
