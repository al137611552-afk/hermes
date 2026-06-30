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
