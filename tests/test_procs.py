"""FR-10.3 后台命令/长进程：ProcessManager + 三工具 + shell background（bash 验，无网络）。

运行：python tests/test_procs.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/_shellenv.py

from _shellenv import (  # noqa: E402
    IS_WIN, RUN_TOOL, SHELL, SHELL_ARGV, big_output, echo, echo_no_newline, read_var, seq, sleep, tick_loop,
)
from agentcore.tools import build_registry  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.procs import (  # noqa: E402
    MAX_PROCS, ProcessManager, extract_localhost_url, url_from_command,
)

# 用共享底座，别写死 bash：Windows 上 `bash` 是 WSL 存根（见 tests/_shellenv.py）。
BASH = SHELL_ARGV


def _wait(cond, timeout=5.0):
    """轮询等待条件成立（读线程/进程退出是异步的）。"""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _reg(tmp: Path, manager: ProcessManager):
    return build_registry(tmp, shell=SHELL, process_manager=manager)


# ---- ProcessManager 核心 ------------------------------------------------------

def test_start_read_incremental_and_exit(tmp: Path):
    m = ProcessManager()
    e = m.start(BASH + [seq(echo("hello"), echo("world"))], str(tmp), "echo×2")
    assert _wait(lambda: "exited" in e.status())
    assert _wait(lambda: "world" in m._get(e.id).buffer)
    r = m.read(e.id)
    assert "hello" in r["new_output"] and r["status"] == "exited(0)"
    r2 = m.read(e.id)                       # 增量：第二次读没有新输出
    assert r2["new_output"] == ""
    m.kill_all()


def test_long_running_stop_kills_tree(tmp: Path):
    if IS_WIN:
        # POSIX 专属：`&` 后台 + `wait` 没有语义等价的 PowerShell 写法（Start-Job 起的是**另一个
        # PowerShell 进程**、不是当前 shell 的子进程，测不到"整树终止"这件事）。
        # 硬凑一个语义不同的版本比跳过更糟——绿了但守的不是同一件事。
        # Windows 侧的整树终止待补，见 ROADMAP「第二档」。
        return
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
    e = m.start(BASH + [big_output(3000, 200)],
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
        m.start(BASH + [sleep(20)], str(tmp), "sleep")
    try:
        m.start(BASH + [sleep(20)], str(tmp), "sleep-overflow")
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
    e = m.start(BASH + [echo("ok")], str(tmp), "echo ok")
    assert _wait(lambda: "exited" in e.status())
    procs = m.list()
    assert len(procs) == 1 and procs[0]["id"] == e.id and procs[0]["command"] == "echo ok"
    m.kill_all()


# ---- 工具层 / 注册表 ----------------------------------------------------------

def test_shell_background_and_tools(tmp: Path):
    m = ProcessManager()
    reg = _reg(tmp, m)
    out = reg.get(RUN_TOOL).run({"command": seq(echo("bg-out"), sleep(10)), "background": True})
    assert "#1" in out and "read_process_output" in out
    assert _wait(lambda: "bg-out" in m._get(1).buffer)
    assert "running" in reg.get("list_processes").run({})
    r = reg.get("read_process_output").run({"id": 1})
    assert "bg-out" in r and "[状态] running" in r
    assert "(无新输出)" in reg.get("read_process_output").run({"id": 1})  # 增量
    assert "已停止" in reg.get("stop_process").run({"id": 1})
    assert _wait(lambda: "exited" in m._get(1).status())
    # 前台命令行为不变
    assert "[exit code] 0" in reg.get(RUN_TOOL).run({"command": "echo fg"})
    m.kill_all()


def test_registry_flags_and_no_manager(tmp: Path):
    m = ProcessManager()
    reg = _reg(tmp, m)
    for name in ("list_processes", "read_process_output", "stop_process"):
        assert name in reg.names() and not reg.is_dangerous(name)
    assert reg.is_dangerous(RUN_TOOL)
    # 不传 manager：三工具不注册、background 给可读错误
    reg2 = build_registry(tmp, shell=SHELL)
    assert "list_processes" not in reg2.names()
    try:
        reg2.get(RUN_TOOL).run({"command": "echo x", "background": True})
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
    e = m.start(BASH + [sleep(30)], str(tmp), "python -m http.server 8000")
    time.sleep(0.2)
    tg = m.preview_targets()
    assert tg and tg[0]["url"] == "http://localhost:8000" and tg[0]["id"] == e.id
    m.stop(e.id)


def test_preview_targets_from_output(tmp: Path):
    """运行中的进程在输出里打了本地 URL → preview_targets 识别到；退出后不再列。"""
    m = ProcessManager()
    e = m.start(BASH + [seq(echo("'Serving on http://localhost:7654/'"), sleep(30))],
                str(tmp), "fake-dev-server")
    assert _wait(lambda: any(t["url"] == "http://localhost:7654/" for t in m.preview_targets()))
    tg = m.preview_targets()
    assert tg and tg[0]["id"] == e.id and tg[0]["command"] == "fake-dev-server"
    m.stop(e.id)
    assert _wait(lambda: m.preview_targets() == [])     # 退出后不列


def test_preview_targets_empty_when_no_url(tmp: Path):
    m = ProcessManager()
    m.start(BASH + [seq(echo("no-url-here"), sleep(30))], str(tmp), "plain")
    time.sleep(0.3)
    assert m.preview_targets() == []


# ---- P3：回答后台进程的交互提示（write_process_input / ADR 0022）----------------

# 两边通用：`echo "[answered:$ans]"` 在 bash 和 PowerShell 里都做变量插值，字面量可原样复用。
_PROMPT_CMD = seq(echo_no_newline("Ok to proceed? (y) "), read_var("ans"),
                  'echo "[answered:$ans]"', sleep(0.2))


def test_prompt_without_newline_is_visible_before_process_exits(tmp: Path):
    # 地基：交互提示**不带换行**。原来读线程按行迭代（for line in stdout），提示会一直压在缓冲里，
    # read_process_output 永远看不到它 → "起后台再回答"这条路根本走不通。现在按 read1 收。
    m = ProcessManager()
    e = m.start(BASH + [_PROMPT_CMD], str(tmp), _PROMPT_CMD)
    assert _wait(lambda: "Ok to proceed?" in m.read(e.id)["new_output"] or "Ok to proceed?" in e.buffer), \
        f"进程还没退出就该能看到不带换行的提示；实际缓冲={e.buffer!r}"
    assert e.proc.poll() is None, "此时进程应仍在等输入（没退出）"
    m.stop(e.id)


def test_waiting_prompt_detected_then_answered_end_to_end(tmp: Path):
    import agentcore.tools.procs as procs
    orig = procs.PROMPT_QUIET_SECONDS
    procs.PROMPT_QUIET_SECONDS = 0.3          # 压缩静止阈值，测试跑得快
    try:
        m = ProcessManager()
        e = m.start(BASH + [_PROMPT_CMD], str(tmp), _PROMPT_CMD)
        assert _wait(lambda: m.waiting_prompt(e.id) is not None), "静止后应判定为'停在提示上等输入'"
        assert "Ok to proceed?" in m.waiting_prompt(e.id)
        # read_process_output 应把出口一起给出来（这是后台相对前台的唯一优势）
        out = _reg(tmp, m).get("read_process_output").run({"id": e.id})
        assert "停在交互提示上等输入" in out and "write_process_input" in out, out
        # 真回答
        msg = m.write_input(e.id, "y")
        assert "已向进程" in msg
        assert _wait(lambda: "[answered:y]" in e.buffer), f"进程应收到 y 并继续；缓冲={e.buffer!r}"
        assert "[已输入] y" in e.buffer, "写进去的内容要回显进日志，否则事后看不出这个 y 是谁答的"
        assert _wait(lambda: e.proc.poll() is not None), "答完后进程应自己结束"
        assert m.waiting_prompt(e.id) is None, "已退出的进程不该再报'在等输入'"
    finally:
        procs.PROMPT_QUIET_SECONDS = orig


def test_no_waiting_hint_while_output_still_flowing(tmp: Path):
    # 防误报：输出里出现过提示样文本、但进程还在刷输出 → 不该说它在等输入。
    import agentcore.tools.procs as procs
    orig = procs.PROMPT_QUIET_SECONDS
    procs.PROMPT_QUIET_SECONDS = 0.3
    cmd = seq(echo("'continue? [y/N]'"), tick_loop(6, "tick", 0.2))
    try:
        m = ProcessManager()
        e = m.start(BASH + [cmd], str(tmp), cmd)
        seen = []
        for _ in range(12):
            seen.append(m.waiting_prompt(e.id))
            time.sleep(0.1)
        assert all(s is None for s in seen), f"仍在刷输出时不该判成等输入：{seen}"
        m.stop(e.id)
    finally:
        procs.PROMPT_QUIET_SECONDS = orig


def test_write_input_errors_are_actionable(tmp: Path):
    m = ProcessManager()
    tool = _reg(tmp, m).get("write_process_input")
    assert tool is not None and tool.dangerous, "write_process_input 必须过权限 gate（每句输入都要用户看得见）"
    # 不存在的进程
    for bad, expect in ((999, "没有进程"), ("x", "整数")):
        try:
            tool.run({"id": bad, "text": "y"})
            raise AssertionError("应报错")
        except ToolError as ex:
            assert expect in str(ex), str(ex)
    # 空 / 多行
    e = m.start(BASH + [sleep(5)], str(tmp), "sleep 5")
    for bad_text, expect in (("", "不能为空"), ("  ", "不能为空"), ("y\nn", "只能是一行")):
        try:
            tool.run({"id": e.id, "text": bad_text})
            raise AssertionError(f"应拒绝 {bad_text!r}")
        except ToolError as ex:
            assert expect in str(ex), str(ex)
    # 已退出的进程
    m.stop(e.id)
    assert _wait(lambda: e.proc.poll() is not None)
    try:
        tool.run({"id": e.id, "text": "y"})
        raise AssertionError("已退出的进程应报错")
    except ToolError as ex:
        assert "已经结束" in str(ex), str(ex)


def test_write_input_submit_false_sends_no_newline(tmp: Path):
    if IS_WIN:
        # POSIX 专属：`read -n 1`（读满一个字符即返回、不等回车）在 PowerShell 里只有
        # `[Console]::ReadKey()`，而它要真控制台、拿不到重定向的管道输入——用它测出来的
        # 是"控制台可用性"而不是"submit=False 不补回车"。Windows 侧待补，见 ROADMAP「第二档」。
        return
    # 少数场景要单键响应（不补回车）。用 `read -n 1` 验：不补回车也应被读到。
    cmd = "printf 'press: '; read -n 1 c; echo \"[got:$c]\"; sleep 0.2"
    m = ProcessManager()
    e = m.start(BASH + [cmd], str(tmp), cmd)
    assert _wait(lambda: "press:" in e.buffer)
    m.write_input(e.id, "k", submit=False)
    assert _wait(lambda: "[got:k]" in e.buffer), f"submit=false 应原样写入不加回车；缓冲={e.buffer!r}"


def test_readonly_roles_cannot_write_process_input():
    # 只读角色（researcher/reviewer/tester）能看后台进程，但**不能往里写**——写输入是有副作用的动作。
    from agentcore.tools.delegate import ROLES
    for r in ("researcher", "reviewer", "tester"):
        assert not ROLES[r].allows("write_process_input"), f"{r} 不该能写进程输入"
    assert ROLES["general"].allows("write_process_input")


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        # ignore_cleanup_errors：Windows 上**还在跑的进程锁着自己的 cwd**，整个临时目录
        # rmdir 不掉（WinError 32）。清理失败发生在断言全过之后，不该判红。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
