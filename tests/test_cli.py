"""FR-11.7 CLI/headless：参数与 prompt 解析 + run() 输出/退出码（mock Api，无网络）。

运行：python tests/test_cli.py
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore import cli  # noqa: E402


def _args(**kw):
    base = dict(prompt=["任务"], workspace=".", model=None, plan=False,
                json=False, quiet=False, max_steps=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ---- prompt 解析 -------------------------------------------------------------

def test_read_prompt_positional():
    assert cli._read_prompt(_args(prompt=["改", "个", "bug"])) == "改 个 bug"


def test_read_prompt_stdin(monkeypatch_stdin="从管道来的任务"):
    old = sys.stdin
    sys.stdin = io.StringIO(monkeypatch_stdin)
    try:
        # 位置参数里有 "-" → 读 stdin
        assert "从管道来的任务" in cli._read_prompt(_args(prompt=["-"]))
    finally:
        sys.stdin = old


# ---- run()：用假 Api 驱动事件，验证输出/退出码 -------------------------------

class _FakeConv:
    def __init__(self, emit, events, ret):
        self._emit, self._events, self._ret = emit, events, ret
        self.plan_mode = False
        self.gate = SimpleNamespace(_allow_all=False)
    def set_plan_mode(self, on): self.plan_mode = bool(on); return self.plan_mode
    def send_message(self, prompt, attachments=None):
        for e, d in self._events:
            self._emit(e, d)
        return self._ret


def _install_fake_api(monkey_events, ret):
    """返回一个假的 Api 类（构造时把 emit 接到假对话）。"""
    holder = {}
    class FakeApi:
        def __init__(self, cfg, emit=None):
            self.active = _FakeConv(emit, monkey_events, ret)
            holder["conv"] = self.active
        def close(self): holder["closed"] = True
    return FakeApi, holder


def _run_capture(args, events, ret):
    import agentcore.bridge.api as apimod
    FakeApi, holder = _install_fake_api(events, ret)
    orig = apimod.Api
    apimod.Api = FakeApi
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.run(args)
    finally:
        apimod.Api = orig
    return code, out.getvalue(), err.getvalue(), holder


def test_run_human_mode():
    events = [("chunk", "答"), ("tool_use", {"name": "read_file", "input": {}}), ("chunk", "案")]
    code, out, err, holder = _run_capture(_args(), events, {"ok": True})
    assert code == 0 and "答案" in out
    assert "read_file" in err            # 工具活动进 stderr
    assert holder["conv"].gate._allow_all is True   # 非 plan：自动批准
    assert holder.get("closed")


def test_run_json_mode():
    events = [("chunk", "结果文本"), ("tool_use", {"name": "grep_search", "input": {}}),
              ("subagent_start", {"role": "researcher", "task": "x"})]
    code, out, err, _ = _run_capture(_args(json=True), events, {"ok": True})
    obj = json.loads(out.strip())
    assert obj["ok"] and obj["answer"] == "结果文本"
    assert obj["tools"] == ["grep_search"] and obj["subagents"] == 1
    assert out.count("\n") == 1          # JSON 模式 stdout 只有一行


def test_run_plan_mode_sets_flag():
    code, out, err, holder = _run_capture(_args(plan=True), [("chunk", "计划")], {"ok": True})
    assert holder["conv"].plan_mode is True
    assert holder["conv"].gate._allow_all is False   # plan 不自动批准（只读本就无危险）


def test_run_error_exit_code():
    code, out, err, _ = _run_capture(_args(json=True), [("error", "炸了")], {"ok": False, "error": "炸了"})
    obj = json.loads(out.strip())
    assert code == 1 and obj["ok"] is False and "炸" in obj["error"]


def test_run_empty_prompt():
    code, out, err, _ = _run_capture(_args(prompt=[]), [], {"ok": True})
    # 空 prompt 且非管道：返回 2（参数错）。注意测试环境 stdin 可能非 tty，这里直接验证逻辑分支
    # 通过给空 prompt 且 stdin 为空字符串模拟
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        import agentcore.bridge.api as apimod
        FakeApi, _ = _install_fake_api([], {"ok": True})
        orig = apimod.Api; apimod.Api = FakeApi
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = cli.run(_args(prompt=[]))
        apimod.Api = orig
    finally:
        sys.stdin = old
    assert rc == 2


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
