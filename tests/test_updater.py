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


def test_default_run_git_pull_real_integration():
    # 真 git：建 origin → clone → 在 origin 加提交 → _default_run 走 git pull --ff-only（用硬化环境）应拉到。
    origin = tempfile.mkdtemp()
    clone = tempfile.mkdtemp()
    g = "git -c user.email=a@b.c -c user.name=t"
    subprocess.run(f"cd {origin} && git init -q -b main && echo v1 > f && {g} add -A && {g} commit -q -m c1",
                   shell=True, capture_output=True, check=True)
    subprocess.run(f"git clone -q {origin} {clone}", shell=True, capture_output=True, check=True)
    subprocess.run(f"cd {origin} && echo v2 >> f && {g} add -A && {g} commit -q -m c2",
                   shell=True, capture_output=True, check=True)
    rc, out = updater._default_run(["git", "pull", "--ff-only"], clone)
    assert rc == 0, f"git pull 应成功：{out}"
    assert (Path(clone) / "f").read_text().count("v2") == 1, "新提交内容应已拉到本地"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
