"""工作区情境探测 + 智能默认（情境自启②，产品哲学：少让用户操作）。

绑定工作区时轻量探测项目特征（有没有测试 / 多大 / 是不是 git），据此**自动**给若干「内置自动
行为」设合理默认——例如检测到有测试就自动开「改完跑定向测试」，零基础用户无需知道任何开关。

原则（务必守住）：
- **不覆盖用户的显式选择**：用户在 🛠 面板里设过的（feature_flags.json 里有该键）一律尊重，智能默认让位。
- **只加不减**：智能默认只把"关着的有益行为"在合适情境下打开，绝不替用户关掉任何东西。
- **透明**：自动开了什么由上层告知用户一句（可在面板覆盖）。

探测是受控 IO（浅扫）、`compute_smart_defaults` 是纯逻辑，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitsupport
from .codeindex import is_indexable
from .verify import discover_test_files

_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist",
         "build", ".pytest_cache", "data", ".hermes"}
_SCAN_CAP = 1200  # 数代码文件的上限（够区分小/中/大即可，别在超大库上空转）


@dataclass(frozen=True)
class ProjectProfile:
    has_tests: bool
    n_code_files: int
    is_git: bool

    @property
    def is_large(self) -> bool:
        return self.n_code_files >= 200


def detect_project_profile(workspace: Path) -> ProjectProfile:
    """浅扫工作区，判断有无测试 / 代码文件数 / 是否 git 仓库（受控 IO，带上限）。"""
    workspace = Path(workspace).resolve()
    has_tests = bool(discover_test_files(workspace))
    n = 0
    try:
        for p in workspace.rglob("*"):
            if n >= _SCAN_CAP:
                break
            if not p.is_file():
                continue
            if any(part in _SKIP for part in p.relative_to(workspace).parts):
                continue
            if is_indexable(p):
                n += 1
    except OSError:
        pass
    return ProjectProfile(has_tests, n, gitsupport.is_git_workspace(workspace))


def compute_smart_defaults(profile: ProjectProfile, user_keys, agent) -> dict:
    """据项目特征算出要**自动开启**的内置行为（纯逻辑），返回 {flag: True}。

    user_keys：用户在面板显式设过的键集合（feature_flags.json 的键）——这些一律不动。
    agent：当前 AgentConfig（已含 config + 面板覆盖）；已经开着的不再重复开。
    """
    out: dict = {}
    uk = set(user_keys or ())

    def consider(flag: str, cond: bool) -> None:
        # 用户没在面板设过、且当前没开着、且情境满足 → 自动开
        if cond and flag not in uk and not getattr(agent, flag, False):
            out[flag] = True

    # 有测试 → 自动开「改完跑定向测试」（最高价值：即时对错信号，零基础用户也受益）
    consider("auto_affected_test", profile.has_tests)
    return out


def describe_smart_defaults(applied: dict) -> str:
    """把自动开启项组织成给用户看的一句话（纯逻辑）；空则空串。"""
    labels = {
        "auto_affected_test": "检测到本项目有测试，已自动开启「改完跑定向测试」",
        "auto_review": "已自动开启「收尾自动审 diff」",
    }
    parts = [labels.get(k, k) for k in applied]
    if not parts:
        return ""
    return "🤖 " + "；".join(parts) + "（可在 🛠 功能开关里关闭）"
