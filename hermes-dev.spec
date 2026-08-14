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
def _skip_cli(name: str) -> bool:
    """跳过各包的 `cli` 子包。

    **为什么必须跳**（2026-08-13 CI 首次跑通打包时炸的）：`collect_submodules` 是靠**逐个
    `__import__` 子模块**来发现依赖的，而 `mcp.cli.cli` 顶层 `import typer`——那是 mcp 的可选
    extra（`mcp[cli]`），我们没装；更要命的是它 import 失败时直接 `sys.exit(1)`，
    把 PyInstaller 的探测子进程一并带走，整个打包中断。hermes 只用 MCP **客户端**，
    CLI 一行都不碰，收它纯属自找麻烦（顺带还会把 typer 塞进产物）。

    **按路径分量匹配，别写 `".cli" not in name`**：那个子串写法会把 **`mcp.client`** 也一起干掉
    （"mcp.client" 里就含 ".cli"）——打包照样成功，但产物里没有 MCP 客户端，**静默坏掉**。

    **只有 filter 能救，`on_error` 救不了**：`collect_submodules` 的重试兜底是
    `except Exception`，而 `sys.exit()` 抛的 `SystemExit` 是 `BaseException`，压根不进那条分支。
    真正起作用的是 filter 挡在**递归之前**（源码：`for sub in subpackages: if filter(sub): todo.append(sub)`），
    子包连 `__import__` 都不会发生。
    """
    return "cli" not in name.split(".")


hiddenimports = []
for _pkg in ("webview", "anthropic", "openai", "mcp", "pydantic", "pypdf", "pkg_resources"):
    # on_error="warn"：默认是 "warn once"，只报第一个失败的包、后面的静默吞掉。
    # 这里要的是**全都报出来**（如 openai.helpers 缺 numpy），出问题时日志里能一眼看全。
    hiddenimports += collect_submodules(_pkg, filter=_skip_cli, on_error="warn")
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
    ("skills", "skills"),      # 内置技能包（FR-13.S）；用户可在 exe 旁 skills/ 放同名技能覆盖
    ("skill_catalog.json", "."),  # 内置精选技能市场清单（FR-13.S2）
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
