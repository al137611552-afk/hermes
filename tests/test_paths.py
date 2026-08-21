"""paths 模块自测（源码模式；打包/frozen 模式需在 Windows 实测）。

运行：python tests/test_paths.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore import paths  # noqa: E402


def test_source_mode():
    assert paths.IS_FROZEN is False
    assert paths.BUNDLE_DIR == paths.APP_DIR          # 源码模式两者相等
    assert (paths.BUNDLE_DIR / "src" / "agentcore").is_dir()  # 指向项目根


def test_helpers():
    assert paths.bundled("web", "index.html") == paths.BUNDLE_DIR / "web" / "index.html"
    assert paths.app_path("data", "x.db") == paths.APP_DIR / "data" / "x.db"


def test_web_and_config_present():
    assert paths.bundled("web", "index.html").exists()
    assert paths.bundled("config.yaml").exists()       # 默认配置可作打包资源


def test_runtime_state_files_are_gitignored():
    """GUI 在 APP_DIR 写的每个运行时状态档都必须进 .gitignore。

    APP_DIR 在源码模式下就是仓库根，所以在检出里跑起应用、点两下设置面板，
    这些每台机器各不相同的档就会落在工作区里等着被误提交。
    发现口径＝config.py 里的 `*_FILE = "..."` 常量（新增同类文件天然会被这道闸逮到），
    外加 api.py 里以字面量写死的 skill_installs.json。
    真有哪个 `*_FILE` 是该入库的打包资源，就在 _BUNDLED_EXEMPT 里显式豁免——
    要的是"漏掉时红一次"，不是"悄悄放过"。
    """
    import re

    root = paths.BUNDLE_DIR
    _BUNDLED_EXEMPT: set[str] = set()      # 目前一个都没有

    src = (root / "src" / "agentcore" / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'^[A-Z_]+FILE = "([^"]+)"', src, re.M))
    assert len(names) >= 10, f"没扫到 config.py 的 *_FILE 常量（只有 {names}），正则可能失效了"
    names.add("skill_installs.json")       # api.py:_installs_path()，字面量不是常量

    ignored = {
        ln.strip().rstrip("/")
        for ln in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    }
    missing = sorted(n for n in names - _BUNDLED_EXEMPT if n not in ignored)
    assert not missing, f"这些运行时状态档没进 .gitignore：{missing}"


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
