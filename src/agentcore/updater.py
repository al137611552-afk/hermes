"""应用内更新（T1：源码自更新）——检查 GitHub 上是否有更新的版本 tag，并用 git pull + pip install 就地更新。
见 ADR 0020。纯逻辑（版本解析/比较）与 IO（GitHub API / 跑命令）分离：前者无头全单测，后者注入 fetch/run 便于测试。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

GITHUB_REPO = "al137611552-afk/hermes"       # 默认仓库
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


# ---- 纯逻辑：版本解析/比较（无 IO，全单测）--------------------------------

def parse_version(s):
    """'v3.51.2' / '3.51.2' -> (3,51,2)；解析不了返回 None。忽略预发布后缀。"""
    if not s:
        return None
    m = _TAG_RE.match(str(s).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(remote, local) -> bool:
    """remote 版本是否严格新于 local。任一解析不了则**保守返回 False**（宁可不提示，也别误报更新）。"""
    r, l = parse_version(remote), parse_version(local)
    if r is None or l is None:
        return False
    return r > l


def current_version() -> str:
    """本地安装版本：优先 importlib.metadata（读 pyproject 定的版本），回退包 __version__。"""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("hermes-dev")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "0.0.0"


def repo_root() -> Path:
    """源码安装的仓库根（src/agentcore/updater.py -> parents[2]）。打包 exe 下此路径不是 git 仓库，
    由 apply_update 首步 git 判定优雅兜底。"""
    return Path(__file__).resolve().parents[2]


# ---- IO：查 GitHub / 跑命令（可注入替身）----------------------------------

def _default_fetch(url, timeout):
    req = urllib.request.Request(
        url, headers={"User-Agent": "hermes-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_latest(repo=GITHUB_REPO, timeout=6, fetch=None):
    """查 GitHub 最新版本 tag。返回 {'version','tag','notes_url'} 或 None（网络失败/无 tag，**静默不打扰**）。
    用 tags API：只要 push 了 git tag 即可，无需另建 GitHub Release。fetch(url,timeout)->解析后 JSON（可注入）。"""
    url = f"https://api.github.com/repos/{repo}/tags?per_page=30"
    try:
        data = (fetch or _default_fetch)(url, timeout)
    except Exception:  # noqa: BLE001 — 网络/解析任何问题都静默
        return None
    best = None  # (版本元组, 原始tag名)
    for t in data or []:
        v = parse_version(t.get("name", ""))
        if v and (best is None or v > best[0]):
            best = (v, t.get("name"))
    if not best:
        return None
    return {"version": ".".join(str(x) for x in best[0]),
            "tag": best[1], "notes_url": f"https://github.com/{repo}/releases"}


def check_update(repo=GITHUB_REPO, local=None, fetch=None) -> dict:
    """给前端的检查结果：{ok, current, latest?, newer?, notes_url?, error?}。网络失败 ok=False（前端静默）。"""
    local = local or current_version()
    latest = check_latest(repo, fetch=fetch)
    if not latest:
        return {"ok": False, "current": local, "error": "无法连接更新服务器"}
    return {"ok": True, "current": local, "latest": latest["version"],
            "newer": is_newer(latest["version"], local), "notes_url": latest["notes_url"]}


def _default_run(argv, cwd):
    """跑一条更新命令，返回 (returncode, 合并输出)。用非交互硬化环境，防 git pull 卡凭据/分页。"""
    from .tools.shell import hardened_env
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300,
                           env=hardened_env(), stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "命令超时（>300s）已放弃"
    except FileNotFoundError:
        return 127, f"找不到命令：{argv[0]}（请确认已安装 git / python）"


def apply_update(repo_dir, run=None) -> dict:
    """就地源码自更新：git pull --ff-only + pip install -e .。返回 {ok, steps, message}。
    run(argv, cwd)->(rc, output) 可注入便于测试。ff-only 失败**不自动 stash**，明确让用户手动处理，绝不丢改动。"""
    runner = run or _default_run
    steps = []

    def do(argv):
        rc, out = runner(argv, str(repo_dir))
        steps.append({"cmd": " ".join(argv), "rc": rc, "output": (out or "")[-2000:]})
        return rc

    if do(["git", "rev-parse", "--is-inside-work-tree"]) != 0:
        return {"ok": False, "steps": steps,
                "message": "当前不是 git 仓库，无法源码自更新（可能是打包版）——请用下载页更新。"}
    if do(["git", "pull", "--ff-only"]) != 0:
        return {"ok": False, "steps": steps,
                "message": "git pull 失败：本地可能有未提交改动或分叉。请先手动 `git stash` 或提交后重试（不会自动丢弃你的改动）。"}
    if do([sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"]) != 0:
        return {"ok": False, "steps": steps,
                "message": "代码已拉取，但依赖安装失败——请查看输出手动跑 `pip install -e .`。"}
    return {"ok": True, "steps": steps, "message": "更新完成，请重启 hermes 生效。"}
