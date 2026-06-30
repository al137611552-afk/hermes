"""路径解析：兼容源码运行与 PyInstaller 打包（frozen）两种模式（P7）。

- BUNDLE_DIR：只读的捆绑资源目录。打包时 = sys._MEIPASS（onedir 解出的资源），
  否则 = 项目根。放 web/、默认 config.yaml。
- APP_DIR：可写的用户目录。打包时 = exe 所在目录，否则 = 项目根。
  放用户的 config.yaml、.env、data/（会话库/记忆库/工作区）。

这样打包后：前端等只读资源在 exe 里，用户要改的 config/密钥/数据在 exe 旁边、可编辑可持久。
"""
from __future__ import annotations

import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    APP_DIR = Path(sys.executable).resolve().parent
else:
    # 源码模式：项目根 = 本文件上溯两级（src/agentcore/paths.py -> 根）
    BUNDLE_DIR = Path(__file__).resolve().parents[2]
    APP_DIR = BUNDLE_DIR


def bundled(*parts: str) -> Path:
    """只读捆绑资源路径（web/ 等）。"""
    return BUNDLE_DIR.joinpath(*parts)


def app_path(*parts: str) -> Path:
    """可写用户文件路径（config.yaml / .env / data/）。"""
    return APP_DIR.joinpath(*parts)
