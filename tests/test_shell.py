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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
