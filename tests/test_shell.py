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


def test_foreground_streams_output_deltas():
    # 前台实时流输出：run() 传 stream 回调时，应边跑边把 stdout 增量推出，且完整拼接≈最终 stdout。
    deltas = []
    out = _tool(timeout=5).run(
        {"command": "printf 'AAA\\nBBB\\nCCC\\n'", "background": False},
        stream=lambda kind, delta: deltas.append((kind, delta)),
    )
    assert deltas, "应收到至少一段流式增量"
    joined = "".join(d for k, d in deltas if k == "stdout")
    assert "AAA" in joined and "CCC" in joined, "流式增量应含命令输出"
    assert "[exit code] 0" in str(out), "结束仍返回完整结果"


def test_foreground_stream_is_actually_realtime_not_buffered_until_exit():
    # 顺带修掉的老问题：原来读的是 TextIOWrapper.read(4096)，**会阻塞到读满或 EOF** → 输出攒不满
    # 4096 字符的命令（绝大多数）根本不会边跑边推，只在退出时一次性吐出来，"实时流输出"名不副实。
    # 现在读 read1()：命令还没退出就该收到第一段。
    stamps = []
    t0 = time.time()

    def _on(kind, delta):
        if "EARLY" in delta:
            stamps.append(time.time())

    _tool(timeout=20).run({"command": "printf 'EARLY\\n'; sleep 3", "background": False}, stream=_on)
    assert stamps, "命令跑完前应已推出第一段增量"
    assert stamps[0] - t0 < 2.0, f"第一段应几乎立刻到达（实测 {stamps[0] - t0:.1f}s），不该等到进程退出"


def test_foreground_stream_optional_backward_compatible():
    # 不传 stream（老调用方式）仍照常工作。
    out = _tool().run({"command": "echo hi", "background": False})
    assert "hi" in str(out) and "[exit code] 0" in str(out)


def test_hardened_env_strips_provider_api_keys():
    # 压测发现：shell 原样透传 os.environ，模型跑 `echo $ARK_API_KEY` 即可把计费密钥打进上下文。
    # 现应在传给子 shell 前剥掉内置 provider 计费密钥（命令用不到、泄露即盗刷）。
    import agentcore.tools.shell as shell
    saved = {k: os.environ.get(k) for k in
             ("ARK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PATH")}
    os.environ["ARK_API_KEY"] = "sk-should-not-leak"
    os.environ["OPENAI_API_KEY"] = "sk-openai-secret"
    try:
        env = shell.hardened_env()
        assert "ARK_API_KEY" not in env, "provider 计费密钥必须从子 shell 环境剥掉"
        assert "OPENAI_API_KEY" not in env
        assert env.get("PATH") == os.environ.get("PATH"), "普通环境变量不能误删"
        assert env.get("GIT_TERMINAL_PROMPT") == "0", "非交互硬化仍要生效"
        # 端到端：跑 echo 拿不到明文
        out = str(_tool().run({"command": "echo v=$ARK_API_KEY", "background": False}))
        assert "sk-should-not-leak" not in out, "实跑 echo 不应回显密钥"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


def test_win_terminate_tree_survives_hanging_taskkill():
    # 真机 >5min 未返回的根因之一：满载时 taskkill 自己卡住，且原 subprocess.run 无 timeout → 收尾挂死、
    # 工具永不返回。现应：taskkill 抛 TimeoutExpired 也不外泄，仍走 proc.kill() 兜底，_terminate_tree 正常返回。
    import agentcore.tools.shell as shell

    killed = {"proc": False}

    class _P:
        pid = 5252
        def poll(self):
            return None          # 仍存活 → 应触发 proc.kill 兜底
        def kill(self):
            killed["proc"] = True

    def _fake_run(*a, **k):
        assert k.get("timeout") is not None, "taskkill 必须带 timeout"
        raise shell.subprocess.TimeoutExpired(cmd="taskkill", timeout=k["timeout"])

    orig_plat, orig_run = shell.sys.platform, shell.subprocess.run
    shell.sys.platform = "win32"
    shell.subprocess.run = _fake_run
    try:
        shell._terminate_tree(_P(), pgid=None, job=None)   # 不应抛、不应挂
    finally:
        shell.sys.platform, shell.subprocess.run = orig_plat, orig_run
    assert killed["proc"] is True, "taskkill 超时后仍须 proc.kill() 兜底"


# ---- P1：非交互硬化补齐 + PowerShell 进度条前缀 ----

def test_noninteractive_env_covers_npm_ssh_gh_and_ci():
    # 真机反复撞超时后补的一批：npm 的 "Ok to proceed? (y)"、ssh 首次连主机的 yes/no、gh 交互问答、
    # 以及覆盖面最大的 CI=1。这些拿不到就只能等超时。
    names = ["CI", "NPM_CONFIG_YES", "npm_config_yes", "SSH_ASKPASS_REQUIRE", "GH_PROMPT_DISABLED",
             "GIT_SSH_COMMAND", "HUSKY", "COMPOSER_NO_INTERACTION", "NO_COLOR", "HOMEBREW_NO_AUTO_UPDATE"]
    cmd = "; ".join(f'echo "{n}=$(printenv {n})"' for n in names)
    out = _tool().run({"command": cmd, "background": False})
    for expect in ("CI=1", "NPM_CONFIG_YES=true", "npm_config_yes=true", "SSH_ASKPASS_REQUIRE=never",
                   "GH_PROMPT_DISABLED=1", "HUSKY=0", "COMPOSER_NO_INTERACTION=1", "NO_COLOR=1",
                   "HOMEBREW_NO_AUTO_UPDATE=1"):
        assert expect in out, f"未注入 {expect}；实际：\n{out}"
    assert "BatchMode=yes" in out, "GIT_SSH_COMMAND 应带 BatchMode（ssh 首次连主机的 yes/no 会挂死）"


def test_user_set_ci_is_respected_not_overridden():
    # CI 会改变测试框架行为（jest 不写新快照等），属"改语义"的开关 → 用户显式设过就不许覆盖。
    from agentcore.tools.shell import hardened_env
    os.environ["CI"] = "0"
    try:
        assert hardened_env()["CI"] == "0", "用户显式设的 CI 被硬化覆盖了"
    finally:
        os.environ.pop("CI", None)
    assert hardened_env()["CI"] == "1", "用户没设时应给上 CI=1"


def test_no_term_dumb_injected():
    # 刻意不设 TERM=dumb：git 会打印 "terminal is not fully functional - press RETURN" 反而多一个挂死点。
    from agentcore.tools.shell import hardened_env
    assert hardened_env().get("TERM") != "dumb", "TERM=dumb 会给 git 造出新的挂死点，不该注入"


def test_powershell_gets_progress_prefix_other_shells_untouched():
    # PS 5.1 的进度条在无窗口环境下能把 Invoke-WebRequest 拖慢一个数量级 → 慢到撞 timeout 像"卡死"。
    # 只关进度显示；bash 等不受影响；**不注入 $ConfirmPreference（那是替用户 auto-yes，越界）**。
    from agentcore.tools.shell import build_argv
    ps = build_argv("powershell", "Invoke-WebRequest http://x -OutFile a.zip")
    assert ps[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert ps[-1].startswith("$ProgressPreference='SilentlyContinue'; ")
    assert "Invoke-WebRequest" in ps[-1]
    assert "ConfirmPreference" not in ps[-1] and "ErrorActionPreference" not in ps[-1]
    assert build_argv("bash", "echo hi") == ["bash", "-lc", "echo hi"]
    assert build_argv("cmd", "dir") == ["cmd", "/c", "dir"]


# ---- P2：交互提示识别（认出"在等你敲字"，别干等满 timeout 再报一句笼统超时）----

def test_looks_waiting_input_recognizes_real_prompts():
    from agentcore.tools.shell import looks_waiting_input
    pos = [
        "Need to install the following packages:\n  create-vite@5.2.3\nOk to proceed? (y)",
        "Overwrite existing file? [y/N]",
        "Do you want to continue? [Y/n] ",
        "The authenticity of host 'github.com' can't be established.\n"
        "Are you sure you want to continue connecting (yes/no)?",
        "Password:",
        "Enter passphrase for key '/root/.ssh/id_rsa':",
        "Username for 'https://github.com':",
        "Press any key to continue . . .",
        "请按任意键继续. . .",
        "? Select a framework › - Use arrow-keys.",
        "--More--",
        "是否继续？",
    ]
    for t in pos:
        assert looks_waiting_input(t), f"应认出交互提示：{t!r}"


def test_looks_waiting_input_ignores_prompts_that_are_not_the_last_line():
    # 关键防误伤：--help 文本里就有 [y/N]、日志里也会出现 Password:——只要后面还有别的输出就不算。
    from agentcore.tools.shell import looks_waiting_input
    neg = [
        "usage: rm [-f | -i] ...\n  -i  prompt [y/N] before every removal\nDone.",
        "Password: ok\nauthenticated, fetching...",
        "added 231 packages in 12s",
        "note: pass --yes to skip confirmation",
        "",
        None,
        "x" * 400 + " [y/N]",          # 超长行多半是数据/日志，不当提示
    ]
    for t in neg:
        assert not looks_waiting_input(t), f"不该判成交互提示：{str(t)[:60]!r}"


def test_foreground_prompt_is_detected_and_killed_before_timeout():
    # 端到端：命令打出提示后干等 → 应在"静止阈值"附近被识别并终止，报错点名提示原文 + 指向非交互参数，
    # 而不是等满 timeout 报一句笼统的超时。
    import agentcore.tools.shell as shell
    orig_quiet = shell._PROMPT_QUIET_SECONDS
    shell._PROMPT_QUIET_SECONDS = 1.0
    tool = _tool(timeout=30)                     # 大 timeout：没识别出来就会等满 30s
    t0 = time.time()
    raised = None
    try:
        tool.run({"command": "printf 'Ok to proceed? (y)'; sleep 30", "background": False})
    except ToolError as e:
        raised = str(e)
    finally:
        shell._PROMPT_QUIET_SECONDS = orig_quiet
    elapsed = time.time() - t0
    assert raised is not None, "停在提示上的命令应被识别并抛错"
    assert "Ok to proceed? (y)" in raised, f"报错应带上提示原文，便于模型改写命令；实际：{raised}"
    assert "--yes" in raised and "别原样重试" in raised
    assert "background:true" not in raised, "等输入 ≠ 常驻服务，别把模型引去后台起（后台照样没人回答）"
    assert elapsed < 12, f"应在静止阈值附近就终止，而非等满 timeout；实测 {elapsed:.1f}s"


def test_busy_command_printing_prompt_like_text_is_not_killed():
    # 防误杀：输出里出现过 [y/N] 但命令一直在刷输出（没静止）→ 不该被当成在等输入。
    import agentcore.tools.shell as shell
    orig_quiet = shell._PROMPT_QUIET_SECONDS
    shell._PROMPT_QUIET_SECONDS = 1.0
    try:
        out = _tool(timeout=20).run(
            {"command": "echo 'prompt [y/N]'; for i in 1 2 3 4 5 6; do echo working $i; sleep 0.5; done",
             "background": False})
    finally:
        shell._PROMPT_QUIET_SECONDS = orig_quiet
    assert "[exit code] 0" in out and "working 6" in out, f"仍在刷输出的命令被误杀了：{out}"


def test_plain_timeout_message_separates_three_causes():
    # 老文案把"不自退"和"等输入"挤在一句 → 模型把交互式命令也丢去 background、在后台继续没人理。
    tool = _tool(timeout=1)
    raised = None
    try:
        tool.run({"command": "sleep 30", "background": False})
    except ToolError as e:
        raised = str(e)
    assert raised is not None
    assert "①" in raised and "②" in raised and "③" in raised, f"超时文案应分列成因；实际：{raised}"
    assert "background:true" in raised and "--yes" in raised


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
