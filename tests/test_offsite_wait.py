"""站外协同：进程退出通知 + 条件等待器（ADR 0026 W1）。

跨平台（命令走 tests/_shellenv.py），不联网、不碰真模型。
运行：python tests/test_offsite_wait.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shellenv import SHELL, echo  # noqa: E402
from agentcore.tools.procs import ProcessManager  # noqa: E402
from agentcore.tools.shell import RunShellTool  # noqa: E402


def _wait_for(cond, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---- ① 后台进程退出即通知（调别的 agent 的主场景）--------------------------

def test_notify_on_exit_fires_with_exit_code_and_tail():
    got = []
    pm = ProcessManager(on_event=lambda body, ref: got.append((body, ref)))
    with tempfile.TemporaryDirectory() as tmp:
        tool = RunShellTool(Path(tmp), shell=SHELL, timeout=30, process_manager=pm)
        out = tool.run({"command": echo("AGENT_DONE"), "background": True, "notify_on_exit": True})
        assert "自动通知你" in out, "要明确告诉模型这一轮可以结束了，否则它还是会回来轮询"
        assert _wait_for(lambda: got), "进程退出后应回投事实"
    body, _ = got[0]
    # **退出码必须是真的码，不能是 None**：这个通知发生在 stdout EOF 时，而管道关闭 ≠ 进程退出，
    # 直接 poll() 会拿到 None、把"不知道"说成退出码。CI 在 Windows 上抓到过（Linux 侥幸赢竞争）。
    assert "已退出" in body and "exit=0" in body, f"退出码没等到真值：{body[:120]}"
    assert "exit=未知" not in body
    assert "AGENT_DONE" in body, "尾部输出要带上——不然模型只知道结束了、不知道结果"


def test_no_notify_when_flag_absent():
    """**不设标志就不投**：默认行为零变化，老用法不该突然开始打扰。"""
    got = []
    pm = ProcessManager(on_event=lambda body, ref: got.append(body))
    with tempfile.TemporaryDirectory() as tmp:
        tool = RunShellTool(Path(tmp), shell=SHELL, timeout=30, process_manager=pm)
        tool.run({"command": echo("quiet"), "background": True})
        time.sleep(1.2)
    assert got == []


def test_notify_reports_nonzero_exit_as_fact_not_verdict():
    """失败也只投事实（exit=N + 输出），**不加"你应该去修"**（决策 3）。"""
    got = []
    pm = ProcessManager(on_event=lambda body, ref: got.append(body))
    with tempfile.TemporaryDirectory() as tmp:
        tool = RunShellTool(Path(tmp), shell=SHELL, timeout=30, process_manager=pm)
        tool.run({"command": "exit 3", "background": True, "notify_on_exit": True})
        assert _wait_for(lambda: got)
    assert "exit=3" in got[0]
    for word in ("应该", "建议你", "请修"):
        assert word not in got[0], f"投递的是事实，不该含指导语「{word}」"


# ---- ② 条件等待器（站外的事只能问）-----------------------------------------

def test_waiter_fires_when_condition_becomes_true():
    """条件一开始不成立、后来成立——等待器要在成立时才投。"""
    got = []
    pm = ProcessManager(on_event=lambda body, ref: got.append(body))
    with tempfile.TemporaryDirectory() as tmp:
        flag = Path(tmp) / "ready.txt"
        # 判据：文件存在则 exit 0。**直接给 argv、不套 shell**——等待器每次探测都会起一个进程，
        # 套 PowerShell 就等于每次多付一次冷启动（Windows 上 5~10s，本项目 shell.py 注释里
        # 早记过"PS 5.1 的老毛病"）。这条测的是**等待器逻辑**，不该被 shell 启动速度绑架
        # （首发 CI 就栽在这：Linux 上 25s 够跑两轮，Windows 上不够）。
        probe = [sys.executable, "-c",
                 f"import sys,os;sys.exit(0 if os.path.exists(r'{flag}') else 1)"]
        wid = pm.start_waiter(probe, tmp, "探测文件是否出现", poll_seconds=5, timeout_minutes=1)
        assert wid > 0
        time.sleep(1.0)
        assert not got, "条件还不成立时不该投"
        assert pm.waiters(), "等待中要能被列出来（用户/模型可查）"
        flag.write_text("ok", encoding="utf-8")
        assert _wait_for(lambda: got, timeout=40), "条件成立后应回投"
    assert "条件已成立" in got[0]
    assert not pm.waiters(), "投完要自己摘掉，别泄漏"


def test_waiter_timeout_reports_instead_of_hanging_silently():
    """**不许无声挂死**：超时也要如实说条件未成立（ADR 0026 已知限制）。"""
    got = []
    pm = ProcessManager(on_event=lambda body, ref: got.append(body))
    with tempfile.TemporaryDirectory() as tmp:
        # 同上：直接给 argv，别让 shell 冷启动混进被测时序
        never = [sys.executable, "-c", "raise SystemExit(1)"]
        wid = pm.start_waiter(never, tmp, "永不成立的判据", poll_seconds=5, timeout_minutes=1)
        # 改台账里的 deadline 即刻生效（等待器以它为准），免得真等满一分钟
        pm._waiters[wid]["deadline"] = time.time() + 0.2
        assert _wait_for(lambda: got, timeout=20)
    assert "等待超时" in got[0] and "未成立" in got[0]
    assert not pm.waiters(), "超时后要自己摘掉"


def test_waiter_can_be_cancelled():
    """会话关掉 / 用户喊停时要停得掉，别留幽灵轮询。"""
    pm = ProcessManager(on_event=lambda body, ref: None)
    with tempfile.TemporaryDirectory() as tmp:
        never = [sys.executable, "-c", "raise SystemExit(1)"]
        wid = pm.start_waiter(never, tmp, "永不成立", poll_seconds=5, timeout_minutes=5)
        assert pm.stop_waiter(wid) is True
        assert pm.waiters() == []
        assert pm.stop_waiter(wid) is False       # 再停一次是 no-op，不抛
        assert pm.cancel_all_waiters() == 0


def test_waiter_clamps_poll_and_timeout():
    """轮询下限 / 时长上限要夹住——别把站外接口打成 DDoS，也别无限期占资源。"""
    pm = ProcessManager(on_event=lambda body, ref: None)
    with tempfile.TemporaryDirectory() as tmp:
        never = [sys.executable, "-c", "raise SystemExit(1)"]
        wid = pm.start_waiter(never, tmp, "永不成立", poll_seconds=0, timeout_minutes=99999)
        remaining = pm.waiters()[0]["remaining_s"]
        assert remaining <= 120 * 60, f"时长上限没夹住：{remaining}s"
        pm.stop_waiter(wid)


def test_probe_error_does_not_kill_the_wait():
    """单次探测出错（断网等）不算条件成立，也不该终止等待——它只是这一次没问到。"""
    pm = ProcessManager(on_event=lambda body, ref: None)
    with tempfile.TemporaryDirectory() as tmp:
        wid = pm.start_waiter(["这个可执行文件不存在_xyz"], tmp, "坏命令",
                              poll_seconds=5, timeout_minutes=5)
        time.sleep(0.8)
        assert pm.waiters(), "探测报错后等待器仍应活着"
        pm.stop_waiter(wid)


# ---- ③ 回投接的是「同一个会话」-----------------------------------------------

def test_event_goes_through_enqueue_marked_as_system():
    """回投要走 enqueue（复用其忙/闲分派与 worker 竞态处理），且**标明非用户输入**。"""
    from agentcore.bridge import conversation as convmod

    seen = []

    class _Fake:
        _on_external_event = convmod.Conversation._on_external_event
        def enqueue(self, text, attachments=None):
            seen.append(text)
            return {"ok": True}

    _Fake()._on_external_event("后台进程 #1 已退出（exit=0）：build", 1)
    assert seen and seen[0].startswith("［系统通知·非用户输入］"), seen
    assert "已退出" in seen[0]


def test_event_failure_never_breaks_process_manager():
    """回投抛异常不能把进程管理拖下水——记账/通知永远不该阻断主线。"""
    def boom(body, ref):
        raise RuntimeError("前端挂了")
    pm = ProcessManager(on_event=boom)
    with tempfile.TemporaryDirectory() as tmp:
        tool = RunShellTool(Path(tmp), shell=SHELL, timeout=30, process_manager=pm)
        tool.run({"command": echo("x"), "background": True, "notify_on_exit": True})
        time.sleep(1.2)      # 没有异常逃逸即算通过


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ok  {name}")
    print(f"test_offsite_wait: {n}/{n} 通过")
