"""本机浏览器探测与 channel 选型（不碰真实文件系统、不起浏览器）。

运行：python tests/test_browsers.py

盯的是 2026-08-26 真机那条：没装 Chrome → 有头模式弹不出浏览器 → 症状只是"点了没反应"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.browsers import (CHROME, EDGE, explain,  # noqa: E402
                                find_browsers, pick_channel)

WIN_ENV = {"ProgramFiles": r"C:\Program Files",
           "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}


def _win(have):
    """have = 判断某路径是否存在的谓词"""
    return find_browsers(exists=have, env=WIN_ENV, windows=True)


def test_chrome_wins_when_both_present():
    """Chrome 优先——穿透效果与 UA 伪装此前都是按 Chrome 调的。"""
    found = _win(lambda p: True)
    assert pick_channel(found) == CHROME
    assert "已找到 Google Chrome" in explain(found, windows=True)


def test_falls_back_to_edge_when_chrome_missing():
    """**本次 bug 的正解**：Windows 一定有 Edge，没装 Chrome 不该让用户去装。"""
    found = _win(lambda p: "Edge" in p)
    assert found[CHROME] == ""
    assert pick_channel(found) == EDGE
    why = explain(found, windows=True)
    assert "改用本机的 Microsoft Edge" in why


def test_user_level_chrome_install_is_found():
    """没有管理员权限装的 Chrome 在 LOCALAPPDATA——只看 Program Files 会把它误判成没装。"""
    found = _win(lambda p: p.startswith(r"C:\Users\me\AppData\Local") and "Chrome" in p)
    assert found[CHROME].startswith(r"C:\Users\me\AppData\Local")
    assert pick_channel(found) == CHROME


def test_none_found_says_so_with_a_way_out():
    """两个都没有时**不许假装能用**，且报错必须带可执行的出路（没出路的报错等于没报）。"""
    found = _win(lambda p: False)
    assert pick_channel(found) is None
    why = explain(found, windows=True)
    assert "没有找到 Chrome" in why
    assert "playwright install chrome" in why or "装 Google Chrome" in why


def test_missing_env_vars_do_not_crash():
    """环境变量缺失（非常规环境/精简容器）时按"该候选不可用"处理，不炸。"""
    found = find_browsers(exists=lambda p: True, env={}, windows=True)
    assert found[CHROME] == "" and found[EDGE] == ""
    assert pick_channel(found) is None


def test_args_use_the_detected_channel(monkey=None):
    """`browser_mcp_args` 不再写死 chrome——本机没装时它必然起不来（本次根因）。"""
    from agentcore import browsers, config
    orig = browsers.find_browsers
    try:
        browsers.find_browsers = lambda *a, **k: {CHROME: "", EDGE: r"C:\Edge\msedge.exe"}
        args = config.browser_mcp_args(headed=True)
        assert "--browser" in args and args[args.index("--browser") + 1] == EDGE
        assert "--headless" not in args          # 有头就是不加这个开关
        browsers.find_browsers = lambda *a, **k: {CHROME: "", EDGE: ""}
        args = config.browser_mcp_args(headed=False)
        assert args[args.index("--browser") + 1] == CHROME   # 都没有→回落旧行为
        assert "--headless" in args
    finally:
        browsers.find_browsers = orig


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
