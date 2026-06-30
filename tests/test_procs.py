"""FR-10.3 后台命令/长进程：ProcessManager + 三工具 + shell background（bash 验，无网络）。

运行：python tests/test_procs.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.procs import (  # noqa: E402
    MAX_PROCS, ProcessManager, extract_localhost_url, url_from_command,
)

BASH = ["bash", "-lc"]


def _wait(cond, timeout=5.0):
    """轮询等待条件成立（读线程/进程退出是异步的）。"""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _reg(tmp: Path, manager: ProcessManager):
    return build_registry(tmp, shell="bash", process_manager=manager)


# ---- ProcessManager 核心 ------------------------------------------------------

def test_start_read_incremental_and_exit(tmp: Path):
    m = ProcessManager()
    e = m.start(BASH + ["echo hello; echo world"], str(tmp), "echo×2")
    assert _wait(lambda: "exited" in e.status())
    assert _wait(lambda: "world" in m._get(e.id).buffer)
    r = m.read(e.id)
    assert "hello" in r["new_output"] and r["status"] == "exited(0)"
    r2 = m.read(e.id)                       # 增量：第二次读没有新输出
    assert r2["new_output"] == ""
    m.kill_all()


def test_long_running_stop_kills_tree(tmp: Path):
    m = ProcessManager()
    # bash 再起一个 sleep 子进程：stop 应整树终止
    e = m.start(BASH + ["sleep 30 & echo started; wait"], str(tmp), "sleep-tree")
    assert _wait(lambda: "started" in m._get(e.id).buffer)
    assert e.status() == "running"
    out = m.stop(e.id)
    assert f"#{e.id}" in out
    assert _wait(lambda: "exited" in e.status())
    assert "早已结束" in m.stop(e.id)       # 幂等
    m.kill_all()


def test_buffer_trim_marks(tmp: Path):
    m = ProcessManager()
    # 产出约 60 万字符 > 20 万缓冲上限：最旧被丢、读到 trimmed 提示
    e = m.start(BASH + ["for i in $(seq 1 3000); do printf 'x%.0s' {1..200}; echo; done"],
                str(tmp), "spam")
    assert _wait(lambda: "exited" in e.status(), timeout=10)
    time.sleep(0.2)                          # 等读线程收尾
    r = m.read(e.id)
    assert r["trimmed"] is True
    assert len(r["new_output"]) <= 50_000    # 单次返回上限
    m.kill_all()


def test_max_procs_cap(tmp: Path):
    m = ProcessManager()
    for _ in range(MAX_PROCS):
        m.start(BASH + ["sleep 20"], str(tmp), "sleep")
    try:
        m.start(BASH + ["sleep 20"], str(tmp), "sleep-overflow")
        assert False, "应达上限"
    except ToolError as e:
        assert "上限" in str(e)
    assert m.kill_all() == MAX_PROCS


def test_unknown_id_and_list(tmp: Path):
    m = ProcessManager()
    try:
        m.read(99)
        assert False
    except ToolError as e:
        assert "#99" in str(e)
    e = m.start(BASH + ["echo ok"], str(tmp), "echo ok")
    assert _wait(lambda: "exited" in e.status())
    procs = m.list()
    assert len(procs) == 1 and procs[0]["id"] == e.id and procs[0]["command"] == "echo ok"
    m.kill_all()


# ---- 工具层 / 注册表 ----------------------------------------------------------

def test_shell_background_and_tools(tmp: Path):
    m = ProcessManager()
    reg = _reg(tmp, m)
    out = reg.get("run_bash").run({"command": "echo bg-out; sleep 10", "background": True})
    assert "#1" in out and "read_process_output" in out
    assert _wait(lambda: "bg-out" in m._get(1).buffer)
    assert "running" in reg.get("list_processes").run({})
    r = reg.get("read_process_output").run({"id": 1})
    assert "bg-out" in r and "[状态] running" in r
    assert "(无新输出)" in reg.get("read_process_output").run({"id": 1})  # 增量
    assert "已停止" in reg.get("stop_process").run({"id": 1})
    assert _wait(lambda: "exited" in m._get(1).status())
    # 前台命令行为不变
    assert "[exit code] 0" in reg.get("run_bash").run({"command": "echo fg"})
    m.kill_all()


def test_registry_flags_and_no_manager(tmp: Path):
    m = ProcessManager()
    reg = _reg(tmp, m)
    for name in ("list_processes", "read_process_output", "stop_process"):
        assert name in reg.names() and not reg.is_dangerous(name)
    assert reg.is_dangerous("run_bash")
    # 不传 manager：三工具不注册、background 给可读错误
    reg2 = build_registry(tmp, shell="bash")
    assert "list_processes" not in reg2.names()
    try:
        reg2.get("run_bash").run({"command": "echo x", "background": True})
        assert False
    except ToolError as e:
        assert "未启用" in str(e)
    m.kill_all()


def test_readonly_roles_see_list_read_not_stop():
    from agentcore.tools.delegate import ROLES
    for r in ("researcher", "reviewer", "tester"):
        assert ROLES[r].allows("list_processes") and ROLES[r].allows("read_process_output")
        assert not ROLES[r].allows("stop_process") or r == "general"
    assert ROLES["general"].allows("stop_process")


# ---- 实时预览面板（UX Tier1-②）：本地 URL 识别 ------------------------------

def test_extract_localhost_url():
    assert extract_localhost_url("Local:   http://localhost:3000/") == "http://localhost:3000/"
    assert extract_localhost_url("Running on http://127.0.0.1:5000") == "http://127.0.0.1:5000"
    # 0.0.0.0 归一成 localhost（0.0.0.0 浏览器里打不开）
    assert extract_localhost_url("Serving at http://0.0.0.0:8000/") == "http://localhost:8000/"
    # 去尾随标点
    assert extract_localhost_url("see (http://localhost:8080).") == "http://localhost:8080"
    # 非本地 / 无 URL → None
    assert extract_localhost_url("https://example.com") is None
    assert extract_localhost_url("just some text") is None
    assert extract_localhost_url("") is None


def test_url_from_command():
    assert url_from_command("python -m http.server 8000") == "http://localhost:8000"
    assert url_from_command("python -m http.server") is None          # 没写端口
    assert url_from_command("npm run dev -- --port 5173") == "http://localhost:5173"
    assert url_from_command("http-server -p 8080") == "http://localhost:8080"
    assert url_from_command("python manage.py runserver 0.0.0.0:8000") == "http://localhost:8000"
    assert url_from_command("flask run --port=5000") == "http://localhost:5000"
    assert url_from_command("vite --host localhost:3000") == "http://localhost:3000"
    assert url_from_command("pytest -q") is None                      # 不是 server、别瞎拼


def test_preview_targets_command_fallback(tmp: Path):
    """输出 buffer 里没有 URL（如 http.server 的行卡在 stdout 缓冲）时，从命令抽端口兜底。"""
    m = ProcessManager()
    e = m.start(BASH + ["sleep 30"], str(tmp), "python -m http.server 8000")
    time.sleep(0.2)
    tg = m.preview_targets()
    assert tg and tg[0]["url"] == "http://localhost:8000" and tg[0]["id"] == e.id
    m.stop(e.id)


def test_preview_targets_from_output(tmp: Path):
    """运行中的进程在输出里打了本地 URL → preview_targets 识别到；退出后不再列。"""
    m = ProcessManager()
    e = m.start(BASH + ["echo 'Serving on http://localhost:7654/'; sleep 30"],
                str(tmp), "fake-dev-server")
    assert _wait(lambda: any(t["url"] == "http://localhost:7654/" for t in m.preview_targets()))
    tg = m.preview_targets()
    assert tg and tg[0]["id"] == e.id and tg[0]["command"] == "fake-dev-server"
    m.stop(e.id)
    assert _wait(lambda: m.preview_targets() == [])     # 退出后不列


def test_preview_targets_empty_when_no_url(tmp: Path):
    m = ProcessManager()
    m.start(BASH + ["echo no-url-here; sleep 30"], str(tmp), "plain")
    time.sleep(0.3)
    assert m.preview_targets() == []


def _run_all():
    import inspect
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
