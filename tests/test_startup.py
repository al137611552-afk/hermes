"""startup 模块自测：启动期故障的人话翻译。

纯逻辑，不碰 GUI/.NET，Linux 上可跑（真实弹窗需 Windows 实测）。
运行：python tests/test_startup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.startup import (  # noqa: E402
    ZONE_STREAM,
    clr_load_hint,
    looks_like_blocked_clr,
    unblock_result_message,
    unblock_tree,
)


def _real_shape() -> Exception:
    """复刻真机那次失败的异常形状：RuntimeError 挂在一串 import 链下面。

    真实报文（v3.67.1 的包，2026-08-14）：
      RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
      D:\\...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll
    """
    try:
        try:
            raise RuntimeError(
                "Failed to resolve Python.Runtime.Loader.Initialize from "
                r"D:\hermes-dev\_internal\pythonnet\runtime\Python.Runtime.dll"
            )
        except RuntimeError as inner:
            raise ImportError("cannot import name 'clr'") from inner
    except ImportError as e:
        return e


def test_recognises_real_failure():
    e = _real_shape()
    assert looks_like_blocked_clr(e) is True
    hint = clr_load_hint(e, r"D:\hermes-dev")
    assert hint is not None
    # 提示必须给出**可照抄的命令**，且路径是用户自己的目录——泛泛说"解除锁定"没用
    assert "Unblock-File" in hint
    assert r"D:\hermes-dev" in hint


def test_marker_only_in_cause_still_counts():
    """真因常被包在下层：只有 __cause__ 里带特征串也要认出来。"""
    try:
        try:
            raise RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize")
        except RuntimeError as inner:
            raise ImportError("winforms 挂了") from inner   # 外层只字不提 .NET
    except ImportError as e:
        assert looks_like_blocked_clr(e) is True


def test_unrelated_failure_not_swallowed():
    """认不出来的必须返回 None——调用方据此原样抛出，别把真异常吃掉。"""
    for exc in (
        RuntimeError("WebView2 运行时未安装"),
        ValueError("config.yaml 解析失败"),
        OSError("端口被占用"),
    ):
        assert looks_like_blocked_clr(exc) is False
        assert clr_load_hint(exc, "C:\\x") is None


def test_cycle_in_exception_chain_terminates():
    """异常链自引用时不能死循环（__context__ 成环是可能的）。"""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert looks_like_blocked_clr(a) is False   # 关键是**能返回**，不是挂住


def _fake_tree():
    """两层目录、4 个文件——模拟 onedir 产物（exe 在根、其余在 _internal）。"""
    return [
        ("C:\\app", ["_internal"], ["hermes-dev.exe"]),
        ("C:\\app\\_internal", [], ["base_library.zip", "Python.Runtime.dll", "clr.py"]),
    ]


def test_unblock_only_touches_zone_stream():
    """只删 :Zone.Identifier，绝不碰文件本身——删错了就是毁产物。"""
    asked = []

    def remove(path):
        asked.append(path)

    cleared, failed = unblock_tree("C:\\app", walk=lambda r: _fake_tree(), remove=remove)
    assert (cleared, failed) == (4, 0)
    assert len(asked) == 4
    assert all(p.endswith(":" + ZONE_STREAM) for p in asked)
    # 去掉流后缀应恰好还原成原文件路径（没有多删/少删别的东西）。
    # 期望值用 os.path.join 拼——本测试在 Linux 上跑，写死反斜杠会假红。
    import os

    assert {p.rsplit(":", 1)[0] for p in asked} == {
        os.path.join("C:\\app", "hermes-dev.exe"),
        os.path.join("C:\\app\\_internal", "base_library.zip"),
        os.path.join("C:\\app\\_internal", "Python.Runtime.dll"),
        os.path.join("C:\\app\\_internal", "clr.py"),
    }


def test_unblock_counts_missing_marker_as_neither():
    """没标记的文件既不算成功也不算失败（多数文件本来就没有）。"""
    def remove(path):
        if "exe" not in path:
            raise FileNotFoundError(path)

    cleared, failed = unblock_tree("C:\\app", walk=lambda r: _fake_tree(), remove=remove)
    assert (cleared, failed) == (1, 0)


def test_unblock_reports_permission_failures():
    """没权限要如实计数，不能假装成功——不然用户会以为解决了。"""
    def remove(path):
        raise PermissionError(path)

    cleared, failed = unblock_tree("C:\\app", walk=lambda r: _fake_tree(), remove=remove)
    assert (cleared, failed) == (0, 4)


def test_result_message_distinguishes_three_outcomes():
    """三种结局给的下一步动作必须不同。"""
    ok = unblock_result_message(12, 0, "C:\\app")
    part = unblock_result_message(3, 9, "C:\\app")
    none = unblock_result_message(0, 0, "C:\\app")

    assert "重新启动" in ok
    assert "管理员" in part and "9" in part      # 失败要给出可执行的下一步
    assert "另有原因" in none                    # 没找到标记≠已解决，别误导
    assert len({ok, part, none}) == 3


def test_real_walk_on_this_machine_is_harmless():
    """拿真 os.walk 在临时目录上跑一遍：非 NTFS 上应安全无操作（不抛、不误删）。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.txt"
        f.write_text("x", encoding="utf-8")
        cleared, failed = unblock_tree(d)
        assert (cleared, failed) == (0, 0)
        assert f.read_text(encoding="utf-8") == "x"   # 文件原封不动


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ok  {name}")
    print(f"test_startup: {n}/{n} 通过")
