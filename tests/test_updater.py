"""应用内更新（ADR 0020 T1）单测。纯逻辑（版本解析/比较）+ 注入 fetch/run 的 IO 编排 + 一条真 git 集成。
独立 runner，不依赖 pytest。"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore import updater  # noqa: E402


def test_parse_version():
    assert updater.parse_version("v3.51.2") == (3, 51, 2)
    assert updater.parse_version("3.51.2") == (3, 51, 2)
    assert updater.parse_version("v3.51.2-rc1") == (3, 51, 2)   # 忽略预发布后缀
    assert updater.parse_version("garbage") is None
    assert updater.parse_version("") is None


def test_is_newer_conservative():
    assert updater.is_newer("3.51.3", "3.51.2") is True
    assert updater.is_newer("3.52.0", "3.51.9") is True
    assert updater.is_newer("3.51.2", "3.51.2") is False       # 相同不算新
    assert updater.is_newer("3.51.1", "3.51.2") is False       # 更旧
    assert updater.is_newer("garbage", "3.51.2") is False      # 解析不了→保守 False，不误报
    assert updater.is_newer("3.51.3", None) is False


def _fake_fetch(tags):
    return lambda url, timeout: [{"name": t} for t in tags]


def test_check_update_detects_newer():
    r = updater.check_update(local="3.51.2", fetch=_fake_fetch(["v3.50.0", "v3.51.3", "v3.51.2"]))
    assert r["ok"] and r["newer"] is True and r["latest"] == "3.51.3"


def test_check_update_no_newer_when_current_is_latest():
    r = updater.check_update(local="3.51.3", fetch=_fake_fetch(["v3.51.3", "v3.51.2"]))
    assert r["ok"] and r["newer"] is False


def test_check_update_silent_on_network_failure():
    def boom(url, timeout):
        raise OSError("no network")
    r = updater.check_update(local="3.51.2", fetch=boom)
    assert r["ok"] is False and "error" in r          # 前端据此静默不打扰


def test_apply_update_happy_path_runs_pull_then_pip():
    calls = []
    def run(argv, cwd):
        calls.append(argv)
        return 0, "ok"
    r = updater.apply_update("/tmp/x", run=run)
    assert r["ok"] is True
    assert calls[0][:2] == ["git", "rev-parse"]        # 先判是不是 git 仓库
    assert calls[1] == ["git", "pull", "--ff-only"]    # 快进拉取（不自动 stash）
    assert calls[2][1:4] == ["-m", "pip", "install"]   # 再重装拾取依赖


def test_apply_update_not_a_git_repo_steers_to_download():
    def run(argv, cwd):
        return (1, "not a git repo") if argv[:2] == ["git", "rev-parse"] else (0, "")
    r = updater.apply_update("/tmp/x", run=run)
    assert r["ok"] is False and "下载页" in r["message"]


def test_apply_update_pull_failure_does_not_stash():
    def run(argv, cwd):
        if argv[:2] == ["git", "rev-parse"]:
            return 0, ""
        if argv[:2] == ["git", "pull"]:
            return 1, "local changes"
        return 0, ""
    r = updater.apply_update("/tmp/x", run=run)
    assert r["ok"] is False and "stash" in r["message"]  # 明确让用户手动处理，绝不自动丢改动


def test_repo_root_walks_up_to_dot_git():
    # repo_root 应向上逐级找到含 .git 的目录，而非写死层级。
    import agentcore.updater as u
    root = tempfile.mkdtemp()
    (Path(root) / ".git").mkdir()
    deep = Path(root) / "src" / "agentcore"
    deep.mkdir(parents=True)
    fake = deep / "updater.py"
    fake.write_text("x")
    orig = u.__file__
    u.__file__ = str(fake)
    try:
        assert u.repo_root() == Path(root).resolve(), "应回溯到含 .git 的根目录"
    finally:
        u.__file__ = orig


def test_apply_update_git_missing_message():
    # git 未安装（rc 127 + FileNotFoundError 文案）时，报错要点出「未安装 git」而非误导为「非 git 仓库」。
    def run(argv, cwd):
        return 127, "找不到命令：git（请确认已安装 git / python）"
    r = updater.apply_update("/tmp/x", run=run)
    assert r["ok"] is False and "未安装 git" in r["message"]


def test_default_run_git_pull_real_integration():
    # 真 git：建 origin → clone → 在 origin 加提交 → _default_run 走 git pull --ff-only（用硬化环境）应拉到。
    #
    # **别用 `cd X && ...` + shell=True**（2026-08-13 CI 踩过）：cmd.exe 的 `cd` **不跨盘符切换，
    # 且照样返回 0**。GitHub runner 上检出在 D:\a\...、临时目录在 C:\Users\...，于是整串命令
    # 悄悄跑在**检出目录**里——git init/commit 落到 hermes 仓库自己的工作树上，origin 始终是空目录，
    # 下一步 clone 才报 exit 128。走 cwd= 传目录、argv 传列表，两个平台语义一致，也不吃引号/转义。
    origin = Path(tempfile.mkdtemp())
    clone = Path(tempfile.mkdtemp())
    g = ["git", "-c", "user.email=a@b.c", "-c", "user.name=t"]

    def run(argv, cwd=None):
        # check=True 的默认报错只有退出码，stderr 被 capture 吞掉——把它带进异常，
        # 否则下次再红又得靠猜（这次的 exit 128 就是这么白查了一轮）。
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        assert p.returncode == 0, f"{argv} (cwd={cwd}) rc={p.returncode}\nstderr: {p.stderr}\nstdout: {p.stdout}"

    run(["git", "init", "-q", "-b", "main"], cwd=origin)
    (origin / "f").write_text("v1\n", encoding="utf-8")
    run(g + ["add", "-A"], cwd=origin)
    run(g + ["commit", "-q", "-m", "c1"], cwd=origin)
    run(["git", "clone", "-q", str(origin), str(clone)])
    (origin / "f").write_text("v1\nv2\n", encoding="utf-8")
    run(g + ["add", "-A"], cwd=origin)
    run(g + ["commit", "-q", "-m", "c2"], cwd=origin)

    rc, out = updater._default_run(["git", "pull", "--ff-only"], str(clone))
    assert rc == 0, f"git pull 应成功：{out}"
    assert (clone / "f").read_text(encoding="utf-8").count("v2") == 1, "新提交内容应已拉到本地"


def test_api_check_update_disabled_by_default_and_makes_no_request():
    """**应用内更新提醒默认关**（2026-08-07 用户要求）：`Api.check_update` 直接返回 disabled，
    且**一个网络请求都不发**——这是前端启动时唯一的检查入口，拦在这里就等于整条链路停用。
    别"顺手"把 `agent.update_check` 改回 true：那会让每次启动又去连 GitHub、又弹条幅。
    """
    from agentcore.bridge import api as apimod

    called = []
    orig = updater.check_update
    updater.check_update = lambda *a, **k: called.append(1) or {"ok": True, "newer": True}
    try:
        class _Cfg:                      # 只需 agent.update_check 这一个字段
            class agent:
                update_check = False
        stub = object.__new__(apimod.Api)     # 不跑 __init__（避免起对话/存储）
        stub.config = _Cfg
        assert stub.check_update() == {"ok": False, "disabled": True}
        assert not called, "关闭时不该调用 updater、更不该发网络请求"
        _Cfg.agent.update_check = True        # 打开则恢复原行为（能力没删，只是默认关）
        assert stub.check_update()["newer"] is True
        assert called
    finally:
        updater.check_update = orig


def test_package_version_matches_pyproject():
    """`agentcore.__version__` 必须与 pyproject 的 version 一致。

    **为什么要有这条闸**（2026-08-14 真跑用量台账时发现）：`__version__` 落后了整整四个版本
    （3.64.0 vs 3.68.0）——因为定版流程只改 pyproject/CHANGELOG/DEVLOG/PRD，**没人记得改它**。
    平时看不出来，是因为 `current_version()` 优先读 importlib.metadata（正式安装/打包产物里
    都有 dist-info，读到的是对的）；**只有 metadata 查不到时才回退到它**——开发机的
    editable 安装正是这种情况，于是台账里记下了错的 harness 版本。
    靠人记得改的东西迟早会漂，用测试钉住。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text("utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml 里找不到 version"
    from agentcore import __version__
    assert __version__ == m.group(1), (
        f"版本漂了：agentcore.__version__={__version__} 而 pyproject={m.group(1)}。"
        f"定版时两处都要改（见 CLAUDE.md 定版流程）。"
    )


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
