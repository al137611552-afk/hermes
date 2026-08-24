"""防黑窗（`winproc.no_window`）：GUI 进程起子进程时别在 Windows 上弹控制台窗口。

运行：python tests/test_winproc.py

**这道闸的发现口径是扫源码、不是手抄清单**——手抄的清单只能守住今天这一批，
而这个 bug 的形状恰恰是"老纪律在，新加的 spawn 点没跟上"（`shell.py`/`procs.py`
早就带着"防黑窗"的注释，后加的 git/verify/hook 那批却全裸着）。
新加一处 `subprocess.run/Popen` 而不防黑窗，下面 `test_every_spawn_site_...` 会当场变红。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.mcp_client import gitwatch  # noqa: E402
from agentcore.winproc import no_window  # noqa: E402

SRC = ROOT / "src" / "agentcore"
_SPAWN = {"run", "Popen", "call", "check_output", "check_call"}

# `subprocess.Popen(**kwargs)` 形态：标志在上面几行按平台塞进 dict，AST 看不进去，
# 只能人工核对一次并记在这里（核对结论：两处都在 `os.name == "nt"` 分支里设了 CREATE_NO_WINDOW）。
_KWARGS_CHECKED = {
    ("tools/procs.py", "kwargs"),    # procs.py:126 —— 注释就写着"防黑窗"
    ("tools/shell.py", "kwargs"),    # shell.py:567 —— 同上
}

# 确实不需要的（给出理由，别用"先放着"）
_EXEMPT = {
    ("workspace.py", "run"): "只在 mac/linux 用（`open`/`xdg-open`）；Windows 那条走 os.startfile，不起子进程",
}


def _spawn_sites():
    """扫出所有 `subprocess.<spawn>()` 调用点：(相对路径, 行号, 所在函数, 分类)。"""
    for f in sorted(SRC.rglob("*.py")):
        rel = str(f.relative_to(SRC)).replace("\\", "/")
        tree = ast.parse(f.read_text(encoding="utf-8"))

        def walk(node, fn=""):
            for ch in ast.iter_child_nodes(node):
                inner = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if (isinstance(ch, ast.Call) and isinstance(ch.func, ast.Attribute)
                        and ch.func.attr in _SPAWN and isinstance(ch.func.value, ast.Name)
                        and ch.func.value.id == "subprocess"):
                    names = [k.arg for k in ch.keywords]
                    dbl = [ast.unparse(k.value) for k in ch.keywords if k.arg is None]
                    if "creationflags" in names:
                        kind = ("ok", "creationflags")
                    elif any("no_window" in d for d in dbl):
                        kind = ("ok", "no_window")
                    elif dbl:
                        kind = ("kwargs", dbl[0])
                    else:
                        kind = ("bare", "")
                    yield rel, ch.lineno, fn, kind
                yield from walk(ch, inner)

        yield from walk(tree)


def test_no_window_only_on_windows():
    """纯逻辑：Windows 给 CREATE_NO_WINDOW，别处空字典（别处传了也无害，但空字典更诚实）。"""
    assert no_window(win=False) == {}
    got = no_window(win=True)
    assert got == {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):           # 真 Windows 上顺带钉住取值
        assert got["creationflags"] == 0x08000000
    a, b = no_window(win=True), no_window(win=True)
    a["creationflags"] = 123
    assert b["creationflags"] != 123                     # 每次新字典，调用方改了不互相污染


def test_every_spawn_site_suppresses_the_console_window():
    """全库扫描：每个 spawn 点要么防了黑窗，要么在两张有理由的名单里。"""
    sites = list(_spawn_sites())
    assert len(sites) >= 15, f"扫描器失灵了？只找到 {len(sites)} 个 spawn 点"
    bad = []
    for rel, line, fn, (kind, detail) in sites:
        if kind == "ok":
            continue
        if kind == "kwargs" and (rel, detail) in _KWARGS_CHECKED:
            continue
        if (rel, fn) in _EXEMPT:
            continue
        bad.append(f"{rel}:{line} 的 {fn}() —— {kind} {detail}".strip())
    assert not bad, ("这些 spawn 点会在 Windows 的 GUI 进程下弹黑框，"
                     "补 `**no_window()`（或说明理由记进 _EXEMPT）：\n  " + "\n  ".join(bad))


def test_exempt_and_checked_lists_stay_honest():
    """名单不许长草：里面每一条都得还对应着一个真实的调用点。"""
    sites = list(_spawn_sites())
    kwargs_now = {(rel, detail) for rel, _l, _f, (kind, detail) in sites if kind == "kwargs"}
    assert _KWARGS_CHECKED <= kwargs_now, f"名单里有已经不存在的项：{_KWARGS_CHECKED - kwargs_now}"
    fns_now = {(rel, fn) for rel, _l, fn, _k in sites}
    assert set(_EXEMPT) <= fns_now, f"豁免名单里有已经不存在的项：{set(_EXEMPT) - fns_now}"


def test_gitwatch_actually_passes_the_flag():
    """最密的那处（每次委派前后各一次 `git status`）真的把标志传下去了——**加了 import 不等于用上了**。"""
    seen: dict = {}

    class _P:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kw):
        seen.update(kw)
        return _P()

    old_run, old_flag = subprocess.run, gitwatch.no_window
    gitwatch.no_window = lambda: {"creationflags": 0x08000000}   # 假装在 Windows 上
    subprocess.run = fake_run
    try:
        gitwatch.status_lines(str(ROOT))
    finally:
        subprocess.run, gitwatch.no_window = old_run, old_flag
    assert seen.get("creationflags") == 0x08000000, seen


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
