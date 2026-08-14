"""startup 模块自测：启动期故障的人话翻译。

纯逻辑，不碰 GUI/.NET，Linux 上可跑（真实弹窗需 Windows 实测）。
运行：python tests/test_startup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.startup import clr_load_hint, looks_like_blocked_clr  # noqa: E402


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


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ok  {name}")
    print(f"test_startup: {n}/{n} 通过")
