# -*- mode: python ; coding: utf-8 -*-
# hermes-dev 的 PyInstaller 打包配置（onedir）。
# 在 Windows 上构建：
#   pip install -e .          # 装好运行依赖
#   pip install pyinstaller
#   pyinstaller --noconfirm hermes-dev.spec
# 产物在 dist\hermes-dev\，运行 dist\hermes-dev\hermes-dev.exe
#
# 注：首次构建建议保留 console=True（下方），能看到启动报错；验证无误后改 False 去掉黑窗再重打。

from PyInstaller.utils.hooks import collect_submodules

# 惰性导入 / 动态加载的包要显式收全（mcp 在方法内 import、webview 后端动态加载等）。
# pkg_resources：补它的内置依赖（appdirs/jaraco/packaging…），否则 exe 启动报
# "The 'appdirs' package is required"。
hiddenimports = []
for _pkg in ("webview", "anthropic", "openai", "mcp", "pydantic", "pypdf", "pkg_resources"):
    hiddenimports += collect_submodules(_pkg)
hiddenimports += [
    "PIL", "PIL.Image", "PIL.ImageGrab",
    "appdirs", "jaraco.text", "jaraco.functools", "jaraco.context",
    "packaging", "packaging.version", "packaging.specifiers", "packaging.requirements",
    "more_itertools",
]

# 捆绑的只读资源（解包后在 sys._MEIPASS 下）
datas = [
    ("web", "web"),            # 前端
    ("config.yaml", "."),      # 默认配置（首次运行释放到 exe 旁供用户编辑）
    ("scripts", "scripts"),    # mcp_echo_server / check_compression 等
]

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hermes-dev",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 发布版：GUI 应用无黑窗。（调试构建报错时临时改回 True）
    disable_windowed_traceback=False,
    icon=None,      # 有图标可填 "web/icon.ico"
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="hermes-dev",
)
