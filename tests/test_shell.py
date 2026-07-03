"""RunShellTool 前台执行：成功路径 + 超时杀整棵进程树（真机 bug：前台启动 GUI 卡住关不掉、反复几次）。

独立 runner（不依赖 pytest）。Linux 上用 bash 跑；杀树走 POSIX killpg（Windows 走 taskkill /T，真机验）。
"""
import sys
import time
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.tools.shell import RunShellTool          # noqa: E402
from agentcore.tools.base import ToolError              # noqa: E402


def _tool(timeout=5):
    return RunShellTool(Path.cwd(), shell="bash", timeout=timeout)


def test_foreground_success_returns_stdout_and_exit_code():
    out = _tool().run({"command": "echo hi", "background": False})
    assert "[exit code] 0" in out and "hi" in out


def test_foreground_nonzero_exit_reported():
    out = _tool().run({"command": "exit 3", "background": False})
    assert "[exit code] 3" in out


def test_timeout_terminates_and_steers_to_background():
    # 前台跑一个不会自退的常驻命令：应在 ~timeout 内被终止并抛错，不无限挂起；错误信息强指向 background:true。
    tool = _tool(timeout=1)
    t0 = time.time()
    raised = None
    try:
        tool.run({"command": "sleep 30 & wait", "background": False})
    except ToolError as e:
        raised = str(e)
    elapsed = time.time() - t0
    assert raised is not None, "常驻命令应超时抛 ToolError"
    assert "background:true" in raised                    # 强指向后台启动（减少模型重试前台）
    assert elapsed < 10, f"应在超时附近就终止，别干等（实际 {elapsed:.1f}s）"


def test_timeout_kills_child_tree():
    # 杀树验证：命令启动一个 grandchild sleep 并把它的 pid 写进临时文件；超时杀树后该 pid 不应再存活。
    import tempfile
    pidfile = Path(tempfile.mkdtemp()) / "child.pid"
    cmd = f"(sleep 30 & echo $! > '{pidfile}'; wait)"
    try:
        _tool(timeout=1).run({"command": cmd, "background": False})
    except ToolError:
        pass
    time.sleep(0.5)
    child_pid = int(pidfile.read_text().strip())
    alive = True
    try:
        os.kill(child_pid, 0)                             # 探测存活（不实际发信号）
    except OSError:
        alive = False
    # 收尾：万一没杀掉，别把孤儿 sleep 留给测试机
    if alive:
        try:
            os.kill(child_pid, 9)
        except OSError:
            pass
    assert not alive, f"超时杀树后 grandchild sleep(pid {child_pid}) 不应还活着"


def test_bg_daemon_inheriting_pipe_returns_fast():
    # 压测揪出的隐藏死锁：命令用 `&` 后台起了继承 stdout 的子进程（dev server 常态），shell 本身瞬间
    # echo 完退出，但老实现 communicate() 等管道 EOF => 白挂满 timeout 再被当超时报错。修后应秒回。
    tool = _tool(timeout=8)      # timeout 给大，若仍等 EOF 会挂满 8s
    t0 = time.time()
    out = tool.run({"command": "sleep 20 & echo started", "background": False})
    elapsed = time.time() - t0
    assert "[exit code] 0" in out and "started" in out
    assert elapsed < 3, f"后台子进程继承管道时应等直接子进程退出即返回，别等 EOF（实际 {elapsed:.1f}s）"


def test_runaway_output_is_bounded_not_oom():
    # 压测揪出的 OOM：疯狂刷 stdout 的命令老实现无上限堆内存 => timeout 前先把进程 OOM。修后应有上限、
    # timeout 处被终止且能正常返回超时错误（不崩）。用 200MB 内存上限的子进程隔离验证不 OOM。
    import subprocess as _sp
    # 用独立子进程跑：无上限内存会被 200MB rlimit 触发 MemoryError/OOM(rc!=0)；有上限则正常抛 ToolError。
    script = (
        "import sys, resource\n"
        "resource.setrlimit(resource.RLIMIT_AS, (200*1024*1024, resource.RLIM_INFINITY))\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(r'%s').resolve().parents[1] / 'src'))\n"
        "from agentcore.tools.shell import RunShellTool\n"
        "from agentcore.tools.base import ToolError\n"
        "try:\n"
        "    RunShellTool(Path.cwd(), shell='bash', timeout=3).run({'command':'yes','background':False})\n"
        "    print('NO_TIMEOUT')\n"
        "except ToolError:\n"
        "    print('BOUNDED_OK')\n"
        % __file__
    )
    r = _sp.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert "BOUNDED_OK" in r.stdout, f"疯狂刷屏命令应被上限约束并在 timeout 处终止，不该 OOM/崩溃（stdout={r.stdout!r} rc={r.returncode} err={r.stderr[-200:]!r}）"


def test_noninteractive_env_is_injected():
    # 主流 agent 通病：git log 进 less 等 q / git push 私库等凭据 / git commit 开 vim / apt 问 y/n 静默挂死。
    # 硬化做法=注入非交互环境变量。此处验证子进程确实拿到这些变量（Windows 上正是靠它们避免真挂死）。
    out = _tool().run({"command":
        "for v in GIT_TERMINAL_PROMPT GIT_PAGER PAGER GIT_EDITOR EDITOR DEBIAN_FRONTEND PIP_NO_INPUT; do "
        "echo \"$v=$(printenv $v)\"; done", "background": False})
    for expect in ("GIT_TERMINAL_PROMPT=0", "GIT_PAGER=cat", "PAGER=cat",
                   "GIT_EDITOR=true", "EDITOR=true", "DEBIAN_FRONTEND=noninteractive", "PIP_NO_INPUT=1"):
        assert expect in out, f"未注入非交互硬化变量 {expect}；实际输出：\n{out}"


def test_hardening_does_not_wipe_user_env():
    # 硬化是"叠加"不是"清空"：用户原有环境（如 PATH、模型 key 所在的变量）必须仍在，否则命令找不到程序。
    os.environ["HERMES_STRESS_MARK"] = "keep-me-42"
    try:
        out = _tool().run({"command": "printenv HERMES_STRESS_MARK", "background": False})
        assert "keep-me-42" in out, "硬化环境把用户原有变量清掉了（应叠加而非替换）"
    finally:
        os.environ.pop("HERMES_STRESS_MARK", None)


def test_win_terminate_tree_kills_job_not_just_taskkill():
    # 真机 bug：`Start-Process notepad; Start-Sleep 120` 超时后 powershell 被杀、记事本残留——taskkill /T
    # 靠父子 PID 链遍历，抓不到被 Start-Process 重定父的 GUI。修法：把 shell 并入 Job Object，_terminate_tree
    # 须优先 TerminateJobObject 整组杀（含重定父进程），再 taskkill 兜底。此处模拟 win32 校验分支与 job 透传。
    import agentcore.tools.shell as shell

    class _P:
        pid = 4242
        def poll(self):
            return 0            # 已退出，跳过 proc.kill 兜底

    calls = {"job": None, "taskkill": False}
    orig_plat, orig_kill, orig_run = shell.sys.platform, shell._win_kill_job, shell.subprocess.run
    shell.sys.platform = "win32"
    shell._win_kill_job = lambda j: calls.__setitem__("job", j)
    shell.subprocess.run = lambda *a, **k: calls.__setitem__("taskkill", a and a[0][0] == "taskkill")
    try:
        shell._terminate_tree(_P(), pgid=None, job="JOB#1")
    finally:
        shell.sys.platform, shell._win_kill_job, shell.subprocess.run = orig_plat, orig_kill, orig_run
    assert calls["job"] == "JOB#1", "Windows 收尾必须先按 job 整组杀（否则重定父的 GUI 漏杀）"
    assert calls["taskkill"] is True, "job 之外仍要 taskkill /T 兜底"


def test_looks_long_running_detects_dev_servers_and_watch():
    from agentcore.tools.shell import _looks_long_running
    pos = [
        "streamlit run app.py --server.headless true",
        "python -m streamlit run app.py",
        "uvicorn main:app --reload",
        "flask run",
        "npm run dev",
        "pnpm dev",
        "yarn start",
        "vite",
        "next dev",
        "ng serve",
        "python -m http.server 8000",
        "nodemon server.js",
        "node --watch index.js",
        "tsc --watch",
        "tail -f app.log",
        "mkdocs serve",
    ]
    for c in pos:
        assert _looks_long_running(c), f"应判为常驻服务：{c}"
    neg = [
        "echo hi",
        "ls -la",
        "npm run build",
        "npm install",
        "python script.py",
        "pytest tests/",
        "git status",
        "cat app.py",
    ]
    for c in neg:
        assert not _looks_long_running(c), f"不应判为常驻服务：{c}"


def test_suspected_server_foreground_uses_short_probe_not_full_timeout():
    # 疑似常驻服务前台跑：应在 ~探针窗口内(远小于 timeout)被杀并抛"改 background:true"，不干等满整个 timeout。
    import agentcore.tools.shell as shell
    orig_probe = shell._PROBE_SECONDS
    shell._PROBE_SECONDS = 1                     # 压缩探针窗口，测试要跑得快
    tool = _tool(timeout=30)                      # 大 timeout：若没走探针会等满 30s
    t0 = time.time()
    raised = None
    try:
        tool.run({"command": "python3 -m http.server 0", "background": False})
    except ToolError as e:
        raised = str(e)
    finally:
        shell._PROBE_SECONDS = orig_probe
    elapsed = time.time() - t0
    assert raised is not None and "background:true" in raised, "疑似服务应被探针兜底并强指向 background:true"
    assert elapsed < 10, f"应在探针窗口内(~1s)被杀，而非等满 timeout；实测 {elapsed:.1f}s"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
