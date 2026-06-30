"""写入后零成本语法校验（FR-11.2a）：让 Agent 改完文件立刻知道有没有改坏。

write_file / edit_file / multi_edit 落盘后自动按扩展名做一次轻量校验，失败信息**附加到
工具返回里**回灌模型——不必等模型自己想起来去验，把"改坏了"在当步就暴露。

- Python（.py / .pyi）：标准库 `compile()` 查语法，**无依赖、跨平台、极快**。
- JSON（.json）：`json.loads` 查格式。
- JS/TS 家族（.js/.mjs/.cjs/.jsx/.ts/.tsx）：`node --check` 尽力而为，**无 node 则静默跳过**。
- 其它扩展名：不校验（返回 None）。

纯逻辑（detect_kind / verify_text）与少量受控 IO（make_verifier 读文件、node 子进程）分离，便于单测。
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

_PY = {".py", ".pyi"}
_JSON = {".json"}
_NODE = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
NODE_TIMEOUT = 15


def detect_kind(relpath: str) -> str:
    """按扩展名判定校验类型：'py' / 'json' / 'node' / ''（不校验）。"""
    ext = Path(relpath).suffix.lower()
    if ext in _PY:
        return "py"
    if ext in _JSON:
        return "json"
    if ext in _NODE:
        return "node"
    return ""


def verify_text(relpath: str, text: str) -> "str | None":
    """对内存中的文本做与扩展名匹配的语法校验（纯函数，py/json 部分）。

    返回 None=通过或不适用；返回字符串=可读的错误信息（含行号）。node 类不在此处
    （需子进程，见 make_verifier）。
    """
    kind = detect_kind(relpath)
    # 剥掉开头的 UTF-8 BOM（U+FEFF）：带 BOM 的源码是合法的（Python/解析器都能跑），
    # 但 ast.parse/json.loads 会把它当非法字符误报。Windows 工具（如 PowerShell Set-Content -Encoding utf8）
    # 常给文件加 BOM，不剥会把正常文件误判成语法错误。
    if text[:1] == "\ufeff":   # 开头是 BOM
        text = text[1:]
    if kind == "py":
        try:
            ast.parse(text)
        except SyntaxError as e:
            loc = f"第 {e.lineno} 行" + (f" 第 {e.offset} 列" if e.offset else "")
            return f"⚠ 语法错误（{relpath} {loc}）：{e.msg}。改动已写入，但该文件无法解析，请修正。"
        return None
    if kind == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return (f"⚠ JSON 格式错误（{relpath} 第 {e.lineno} 行 第 {e.colno} 列）：{e.msg}。"
                    "改动已写入，但该文件不是合法 JSON，请修正。")
        return None
    return None


# ============================================================================
# FR-13.C 编辑后跑定向测试（P5 调试能力工程化，第一波）
# 改完文件后：识别**受影响的测试**并直跑，把通过/失败喂回循环——从「语法对不对」
# 升到「测试过不过」，补「每轮改完没即时对错信号」的缺口。
# 纯逻辑（is_test_file / subject_of_test / affected_tests / detect_test_argv）与受控 IO
# （make_affected_test_runner 的发现+子进程）分离，纯逻辑全部可脱环境单测。
# ============================================================================

# 受影响测试一次最多跑几个文件（防一改触发整片测试，拖慢回路）。
MAX_AFFECTED = 4
TEST_TIMEOUT = 60


def _norm(relpath: str) -> str:
    return (relpath or "").replace("\\", "/").strip().lstrip("./")


def subject_of_test(test_relpath: str) -> "str | None":
    """测试文件 → 它测的「主题名」；非测试文件返回 None（纯逻辑）。

    约定：`test_foo.py`→foo、`foo_test.py`→foo、`pure.test.js`→pure。
    """
    name = Path(_norm(test_relpath)).name
    stem = name
    for ext in (".py", ".pyi", ".mjs", ".cjs", ".jsx", ".tsx", ".ts", ".js"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    low = stem.lower()
    if low.startswith("test_") and len(stem) > 5:
        return stem[5:]
    if low.endswith("_test") and len(stem) > 5:
        return stem[:-5]
    if low.endswith(".test") and len(stem) > 5:
        return stem[:-5]
    return None


def is_test_file(relpath: str) -> bool:
    """relpath 看起来是不是测试文件（纯逻辑）。"""
    return subject_of_test(relpath) is not None


def affected_tests(relpath: str, test_files: "list[str]") -> "list[str]":
    """编辑了 relpath，返回应当直跑的测试文件列表（纯逻辑、确定性排序）。

    - 编辑的本身就是测试 → 跑它自己。
    - 否则按「文件主名」匹配同主题测试（`workspace.py` → `test_workspace.py`；
      `pure.js` → `pure.test.js`）。匹配不到 → 空列表（=该文件没有定向测试，跳过）。
    test_files 为工作区内已知测试文件相对路径集合（由调用方发现后传入）。
    """
    rel = _norm(relpath)
    norm_tests = [_norm(t) for t in (test_files or [])]
    if rel in set(norm_tests):
        return [rel]
    subject = Path(rel).stem.lower()  # workspace.py→workspace, pure.js→pure
    if not subject:
        return []
    hits = [t for t in norm_tests if (subject_of_test(t) or "").lower() == subject]
    return sorted(dict.fromkeys(hits))[:MAX_AFFECTED]


def detect_test_argv(test_relpath: str, *, pytest_available: bool,
                     node_available: bool) -> "list[str] | None":
    """为单个测试文件选运行命令 argv；选不出（缺解释器/不支持）返回 None（纯逻辑）。

    .py：有 pytest 用 `pytest -q <file>`，否则按本项目惯例当独立脚本 `python <file>` 跑。
    .js 家族：有 node 用 `node --test <file>`，否则跳过。
    """
    import sys
    ext = Path(_norm(test_relpath)).suffix.lower()
    if ext in _PY:
        if pytest_available:
            return [sys.executable, "-m", "pytest", "-q", test_relpath]
        return [sys.executable, test_relpath]
    if ext in _NODE:
        if node_available:
            return ["node", "--test", test_relpath]
        return None
    return None


def discover_test_files(workspace: Path) -> "list[str]":
    """发现工作区内的候选测试文件相对路径（受控 IO，浅扫，跳过噪声目录）。"""
    workspace = Path(workspace).resolve()
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv",
            "dist", "build", ".pytest_cache", "data", ".hermes"}
    out: list[str] = []
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip for part in p.relative_to(workspace).parts):
            continue
        rel = p.relative_to(workspace).as_posix()
        if is_test_file(rel):
            out.append(rel)
    return sorted(out)


# pytest 退出码 5 = 「没有收集到任何用例」，不是测试失败（如改的是无 test_ 函数的脚本/纯模块），按通过处理。
_PYTEST_NO_TESTS = 5

# pytest 风格特征：有 `def test_*` 函数 / import pytest / @pytest 装饰器。这类测试**当独立脚本跑函数不会执行**，
# 没装 pytest 时绝不能当通过（会假通过、骗过整个定向测试）——要么用 pytest、要么明确报"需装 pytest"。
_PYTEST_STYLE = re.compile(r"^\s*(?:async\s+)?def\s+test\w*\s*\(|^\s*import\s+pytest|^\s*@pytest", re.M)


def is_pytest_style(source: str) -> bool:
    """测试源码是否 pytest 风格（含 def test_ 函数 / import pytest）（纯逻辑）。"""
    return bool(_PYTEST_STYLE.search(source or ""))


def _test_env(workspace: Path) -> "dict[str, str]":
    """跑测试用的环境：把工作区根（及 `src/` 若存在）放进 PYTHONPATH，让 `python tests/test_x.py`
    能 import 到根目录/`src` 下的被测模块（否则 sys.path[0] 只是测试文件所在目录，常 ModuleNotFoundError）。"""
    env = dict(os.environ)
    roots = [str(workspace)]
    src = workspace / "src"
    if src.is_dir():
        roots.append(str(src))
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(roots + ([prev] if prev else []))
    # 不往工作区写 __pycache__：既不污染用户工作区，也避免同秒重改时命中 mtime 粒度的旧 .pyc。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def make_affected_test_runner(workspace: Path, runner: str = "auto"):
    """返回 `run(relpath) -> str|None`：改完文件后跑受影响的定向测试（FR-13.C）。

    通过/无受影响测试 → None（不打扰）；失败 → 把命令 + 截断输出组织成可读信息回灌。
    runner：`auto`（.py 有 pytest 用 pytest 否则独立脚本）/`pytest`/`python`（强制独立脚本）。
    任何自身故障（发现/子进程）都不抛出，绝不因测试把写入搞失败。
    """
    workspace = Path(workspace).resolve()
    if runner == "pytest":
        use_pytest = True
    elif runner == "python":
        use_pytest = False
    else:
        try:
            import importlib.util
            use_pytest = importlib.util.find_spec("pytest") is not None
        except Exception:  # noqa: BLE001
            use_pytest = False
    node_available = shutil.which("node") is not None
    test_env = _test_env(workspace)

    def run(relpath: str) -> "str | None":
        try:
            rel = _norm(relpath)
            if detect_kind(rel) not in ("py", "node"):
                return None
            targets = affected_tests(rel, discover_test_files(workspace))
            if not targets:
                return None
            fails: list[str] = []
            notes: list[str] = []
            for t in targets:
                argv = detect_test_argv(
                    t, pytest_available=use_pytest, node_available=node_available)
                if not argv:
                    continue
                # 防呆：没 pytest 又是 pytest 风格测试 → 当独立脚本跑会假通过，明确报"需装 pytest"
                if not use_pytest and detect_kind(t) == "py":
                    try:
                        src = (workspace / t).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        src = ""
                    if is_pytest_style(src):
                        notes.append(f"  ⚠ {t}：是 pytest 风格测试，但本机未装 pytest，无法自动运行"
                                     "（装 `pip install pytest` 即可；或把断言写成模块级）。")
                        continue
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      cwd=str(workspace), timeout=TEST_TIMEOUT,
                                      env=test_env)
                if proc.returncode == 0 or (use_pytest and proc.returncode == _PYTEST_NO_TESTS):
                    continue
                tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                fails.append(f"  ✗ {t}\n" + _indent(tail[-800:]))
            if not fails and not notes:
                return None
            head = ("🧪 受影响测试未通过（FR-13.C，改动已写入，请据下方失败定位修复）："
                    if fails else "🧪 受影响测试（FR-13.C）：")
            msg = head + "\n" + "\n".join(fails + notes)
            from .diagnose import with_location  # 报错定位（FR-13.B）：附加 file:line+源码上下文
            return with_location(msg, workspace)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None  # 自身故障绝不影响写入
        except Exception:  # noqa: BLE001
            return None

    return run


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + ln for ln in (text or "").splitlines())


def make_post_edit_checker(workspace: Path, *, auto_verify: bool = True,
                           auto_affected_test: bool = False, runner: str = "auto"):
    """组合「落盘后检查」：零成本语法校验（FR-11.2a）+ 受影响定向测试（FR-13.C）。

    返回单个 `verify(relpath) -> str|None`（fs 工具只认这一个回调，无需改 fs.py）。
    语法校验失败 → 短路返回（语法都坏了，没必要再跑测试，跑了也只是 import 失败噪声）；
    语法通过且开了 auto_affected_test → 接着跑受影响测试，失败信息附加回灌。
    两项都关 → 返回 None（调用方据此传 None）。
    """
    if not auto_verify and not auto_affected_test:
        return None
    syntax = make_verifier(workspace) if auto_verify else None
    tests = make_affected_test_runner(workspace, runner) if auto_affected_test else None

    def verify(relpath: str) -> "str | None":
        if syntax is not None:
            problem = syntax(relpath)
            if problem:
                return problem  # 语法坏了：短路，不在坏文件上跑测试
        if tests is not None:
            return tests(relpath)
        return None

    return verify


def make_verifier(workspace: Path):
    """返回 `verify(relpath) -> str|None`：落盘后调用，做零成本语法校验。

    py/json 用纯函数读盘内容校验；node 类用 `node --check`（无 node 则静默跳过）。
    任何校验器自身异常都不抛出（绝不因校验把写入搞失败），返回 None。
    """
    workspace = Path(workspace).resolve()

    def verify(relpath: str) -> "str | None":
        rel = (relpath or "").replace("\\", "/").strip()
        kind = detect_kind(rel)
        if not kind:
            return None
        p = workspace / rel
        try:
            if kind in ("py", "json"):
                return verify_text(rel, p.read_text(encoding="utf-8", errors="replace"))
            if kind == "node":
                proc = subprocess.run(
                    ["node", "--check", str(p)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=NODE_TIMEOUT,
                )
                if proc.returncode != 0:
                    msg = (proc.stderr or proc.stdout or "").strip().splitlines()
                    detail = msg[0] if msg else f"exit {proc.returncode}"
                    return (f"⚠ 语法错误（{rel}）：{detail}。改动已写入，但 node 无法解析，请修正。")
                return None
        except FileNotFoundError:
            return None  # 没装 node：静默跳过（不是错误）
        except (OSError, subprocess.TimeoutExpired):
            return None  # 校验器自身故障绝不影响写入
        return None

    return verify
