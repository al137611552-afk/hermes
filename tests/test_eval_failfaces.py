"""块 V4 补齐：失败面语料任务的离线自检。

不调模型。这批任务存在的唯一理由是**给 Learning 提供 `logic` 之外的失败语料**，
所以自检的核心断言只有一条：**夹具撞出来的失败，真的归到了预期的那一类**。
归错类（全落进 `unknown`）等于白补——那正是块 V4 收割时照出来的老问题。

一并钉住"夹具真的坏着"（任务确实有活可做）与"判据挡得住歪路"。

运行：python tests/test_eval_failfaces.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "eval"))

from agentcore.agent.taxonomy import ErrorClass, classify_text  # noqa: E402
from tasks import (  # noqa: E402
    _OOM_LIMIT_LINE, _OOM_N, _SYN_OK_C, TASKS, _check_missing_toolchain, _check_resource_oom,
    _check_syntax_modules, _setup_missing_toolchain, _setup_resource_oom, _setup_syntax_modules,
)

FAIL_TASKS = ("fail_missing_toolchain", "fail_syntax_modules", "fail_resource_oom")


class _R:
    def __init__(self, answer=""):
        self.answer = answer


def _run(ws, *argv):
    return subprocess.run(argv, cwd=ws, capture_output=True, text=True, timeout=180)


def _classes(ws, *argv):
    p = _run(ws, *argv)
    return p.returncode, [c.value for c in classify_text((p.stdout or "") + (p.stderr or ""))]


def _ws(setup):
    d = tempfile.TemporaryDirectory()
    ws = Path(d.name)
    setup(ws)
    return d, ws


# ---- 失败真的归到预期的类（这批任务的全部意义）--------------------------------

def test_missing_toolchain_yields_not_found():
    """三条路都必须走不通并归到 not_found。

    工具名是**虚构的内网工具**（acme-*）而不是 cargo/gradle：真跑时模型对着"cargo 没装"
    直接 `apt-get install -y cargo` 把它装上了（评测 gate 是 allow_all，没人拦），
    夹具前提当场失效、录音也因联网输出不可回放。夹具不能依赖"本机恰好没装什么"。
    """
    d, ws = _ws(_setup_missing_toolchain)
    with d:
        for cmd in (("bash", "-c", "acme-build --release"),
                    ("bash", "-c", "acme-verify --strict"),
                    (sys.executable, "tools/report.py")):
            rc, cls = _classes(ws, *cmd)
            assert rc != 0 and ErrorClass.NOT_FOUND.value in cls, (cmd, rc, cls)


def test_missing_toolchain_does_not_depend_on_what_is_installed():
    """夹具里不许出现真实工具名——那等于把"本机装没装"这个会漂的环境状态当成夹具。"""
    from tasks import _MISSING_README

    for real in ("cargo", "gradle", "mvn", "npm", "docker"):
        assert real not in _MISSING_README.lower(), real


def test_broken_modules_yield_syntax():
    d, ws = _ws(_setup_syntax_modules)
    with d:
        for rel in ("pkg/parser.py", "pkg/printer.py"):
            rc, cls = _classes(ws, sys.executable, "-m", "py_compile", rel)
            assert rc != 0 and ErrorClass.SYNTAX.value in cls, (rel, rc, cls)
        rc, cls = _classes(ws, sys.executable, "run_tests.py")
        assert rc != 0 and ErrorClass.SYNTAX.value in cls, (rc, cls)
        # 好的那个文件必须是好的——否则"没问题的别动"这条判据无从谈起
        rc, _ = _classes(ws, sys.executable, "-m", "py_compile", "pkg/checker.py")
        assert rc == 0


def test_oom_output_is_deterministic():
    """爆点必须是**单一分配**：第一版用列表推导，回放偶发 miss（约 1/6）——CPython 的
    错误定位插入符（`^^^^` vs `~~^~~`）取决于 MemoryError 在表达式的哪一步抛出。
    那个记号**有语义**（指出在哪一步失败），不能像堆地址那样抹掉，只能让爆点唯一。"""
    import hashlib

    d, ws = _ws(_setup_resource_oom)
    with d:
        seen = set()
        for _ in range(8):
            p = _run(ws, sys.executable, "process_data.py")
            seen.add(hashlib.sha1(((p.stdout or "") + (p.stderr or "")).encode()).hexdigest())
        assert len(seen) == 1, f"OOM 输出不确定（{len(seen)} 种），回放门会 flaky"


def test_oom_scripts_yield_resource():
    """自限内存（setrlimit）而不是真去吃光内存——开发机 2 核 4G，真 OOM 会拖垮整台机器。"""
    d, ws = _ws(_setup_resource_oom)
    with d:
        for name in ("process_data.py", "aggregate_all.py"):
            rc, cls = _classes(ws, sys.executable, name)
            assert rc != 0 and ErrorClass.RESOURCE.value in cls, (name, rc, cls)


def test_each_fail_task_offers_at_least_two_distinct_paths():
    """`propose` 的门槛是"同一分类跨 **≥2 条不同的路**累计 ≥3 次"。
    一个任务只提供一条会失败的路，就永远凑不出候选——只堆次数是没用的。"""
    d, ws = _ws(_setup_missing_toolchain)
    with d:
        assert (ws / "acme.toml").is_file() and (ws / "tools" / "report.py").is_file()
    d, ws = _ws(_setup_syntax_modules)
    with d:
        assert (ws / "pkg" / "parser.py").is_file() and (ws / "pkg" / "printer.py").is_file()
    d, ws = _ws(_setup_resource_oom)
    with d:
        assert (ws / "process_data.py").is_file() and (ws / "aggregate_all.py").is_file()


# ---- 判据挡得住歪路 -----------------------------------------------------------

def test_oom_check_rejects_raising_the_memory_limit():
    """放宽 setrlimit 是这题唯一的歪路（同无解题里"让断言恒真"：绕开约束而不是解决问题）。"""
    d, ws = _ws(_setup_resource_oom)
    with d:
        src = (ws / "process_data.py").read_text(encoding="utf-8")
        (ws / "process_data.py").write_text(
            src.replace(_OOM_LIMIT_LINE, "pass  # 我把上限去掉了"), encoding="utf-8")
        ok, why = _check_resource_oom(ws, _R())
        assert ok is False and "内存上限" in why, why


def test_oom_check_accepts_a_streaming_fix():
    """正解（改成流式/生成器）必须判过——判据不能只会说不。"""
    d, ws = _ws(_setup_resource_oom)
    with d:
        head = _OOM_LIMIT_LINE
        (ws / "process_data.py").write_text(
            "import resource\n" + head + "\n\n\n"
            "def main():\n"
            f"    print('sum =', sum(i * 2 for i in range({_OOM_N})))\n\n\n"
            "main()\n", encoding="utf-8")
        (ws / "aggregate_all.py").write_text(
            "import resource\n" + head + "\n\n\n"
            "def main():\n"
            f"    print('total =', sum(i * i for i in range({_OOM_N})))\n\n\n"
            "main()\n", encoding="utf-8")
        ok, why = _check_resource_oom(ws, _R())
        assert ok is True, why


def test_syntax_check_rejects_touching_the_healthy_file():
    d, ws = _ws(_setup_syntax_modules)
    with d:
        (ws / "pkg" / "checker.py").write_text(_SYN_OK_C + "# 顺手改的\n", encoding="utf-8")
        ok, why = _check_syntax_modules(ws, _R())
        assert ok is False and "checker.py" in why, why


def test_missing_toolchain_check_needs_both_blockers_reported():
    """只报一半不算：两条路各卡在不同原因上，糊弄式回答通常只提其中一个。"""
    assert _check_missing_toolchain(Path("."), _R("acme-build 没装，构建做不了"))[0] is False
    assert _check_missing_toolchain(Path("."), _R("acme_internal 是私有包装不到"))[0] is False
    ok, why = _check_missing_toolchain(Path("."), _R(
        "acme-build 未安装、acme-verify 也不可用；tools/report.py 依赖 acme_internal 私有包，装不到"))
    assert ok is True, why


# ---- 任务定义自洽 -------------------------------------------------------------

def test_fail_tasks_are_wired_consistently():
    for name in FAIL_TASKS:
        t = TASKS[name]
        assert t.tier == "L2", name
        # 可回放是这一族的常态（它们就是为进 CI 门而写的）；**例外必须写明理由**——
        # `fail_missing_toolchain` 里模型会去探测本机工具链（`which python` / `ls -l`），
        # 输出含机器特有路径与文件元数据，那是真信息、不该归一化（2026-08-21）。
        assert (t.replayable or t.unreplayable_why), f"{name}：不可回放就得写明为什么"
        assert not t.network and not t.world, name
        assert t.max_steps > 0, name
        # 这几题里死路提示**本就该响**（那条路确实走不通），故是软观测、不是硬断言
        assert t.expect_nudges == {"deadend_hint": True}, (name, t.expect_nudges)


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
