"""细粒度权限规则（FR-11.4）：按「工具+参数模式」放行/拦截危险操作，治确认疲劳。

规则字符串两种形态：
- `tool`            —— 匹配该工具的任意调用（如 `git_status`、`take_screenshot`）。
- `tool(glob)`      —— 仅当该工具调用的「主体」匹配 glob 时命中
                       （如 `run_powershell(git *)`、`write_file(docs/*)`）。

「主体」按工具取最有判别力的参数：run_<shell> 取 command，文件类取 path，web 取 url，否则空。
glob 用 fnmatch（`*`/`?`/`[]`），大小写敏感。allow 让操作免确认，deny 直接拦截（优先于 allow）。

纯逻辑（无 IO、无线程），由 PermissionGate 调用、便于单测。
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

# 取「主体」时依次尝试的参数名（第一个非空的胜出）
_SUBJECT_KEYS = ("command", "path", "url")


def tool_subject(tool_name: str, params: dict) -> str:
    """取一次工具调用的可匹配主体（命令/路径/URL）；取不到返回空串。"""
    if not isinstance(params, dict):
        return ""
    for k in _SUBJECT_KEYS:
        v = params.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_rule(rule: str) -> "tuple[str, str | None]":
    """`tool` -> (tool, None)；`tool(glob)` -> (tool, glob)。非法/空 -> ('', None)。"""
    rule = (rule or "").strip()
    if not rule:
        return "", None
    if rule.endswith(")") and "(" in rule:
        tool, glob = rule[:-1].split("(", 1)
        return tool.strip(), glob.strip()
    return rule, None


def rule_matches(rule: str, tool_name: str, subject: str) -> bool:
    """单条规则是否命中本次调用。"""
    tool, glob = parse_rule(rule)
    if tool != tool_name:
        return False
    if glob is None:
        return True               # 裸工具名：匹配任意参数
    return fnmatch.fnmatchcase(subject, glob)


def matches_any(rules, tool_name: str, subject: str) -> bool:
    return any(rule_matches(r, tool_name, subject) for r in (rules or ()))


def evaluate(allow, deny, tool_name: str, params: dict) -> "str | None":
    """综合裁决：'deny'（拦截）/ 'allow'（免确认）/ None（需用户确认）。deny 优先。"""
    subject = tool_subject(tool_name, params)
    if matches_any(deny, tool_name, subject):
        return "deny"
    if matches_any(allow, tool_name, subject):
        return "allow"
    return None


def suggest_rule(tool_name: str, params: dict) -> str:
    """为确认条「总是允许这类」推导一条规则：命令→首词通配，路径→父目录通配，否则裸工具名。"""
    subject = tool_subject(tool_name, params)
    if not subject:
        return tool_name
    if tool_name.startswith("run_"):
        first = subject.split()[0] if subject.split() else subject
        return f"{tool_name}({first}*)"          # git status / git push 都命中
    key = next((k for k in _SUBJECT_KEYS if isinstance(params.get(k), str)), None)
    if key == "path":
        parent = PurePosixPath(subject.replace("\\", "/")).parent.as_posix()
        glob = f"{parent}/*" if parent not in (".", "", "/") else "*"
        return f"{tool_name}({glob})"
    if key == "url":
        # 同站点通配：https://docs.python.org/... -> https://docs.python.org/*
        m = subject.split("/")
        if len(m) >= 3:
            return f"{tool_name}({m[0]}//{m[2]}/*)"
    return tool_name


# ── 智能确认分级（自动放行「明显安全」的只读/检视/测试命令，治确认疲劳）──────────
# 仅对 shell 工具（run_*）生效。判定原则 **safe-by-default**：整条命令（含 && || ; |
# 串接的每一段）都属白名单内的只读/检视/测试/静态检查才放行；**拿不准一律不放**（落回
# 逐次确认）。错判宁可多弹一次（误判成"危险"只是多点一下，不会放过真危险命令）。
# 写文件/编辑/commit/MCP 等非 run_ 工具一律返回 False（照旧确认）。

import re as _re2

# 单独成段即安全的命令（只读/检视 + 测试/静态检查——跑项目自己的测试与规则、只读源码）。
# 命令名匹配**大小写不敏感**（见 _norm_cmd），故此处统一小写——兼容 PowerShell（cmdlet/别名
# 大小写不敏感，如 Dir / Get-ChildItem / GC）与 Unix。
_SAFE_LEADING = frozenset({
    # ── Unix / 跨平台 ──
    "ls", "ll", "pwd", "cat", "head", "tail", "wc", "echo", "which", "type",
    "file", "stat", "env", "printenv", "date", "whoami", "hostname", "uname",
    "tree", "du", "df", "basename", "dirname", "realpath", "readlink", "nl",
    "grep", "egrep", "fgrep", "rg", "find", "true", "clear", "id", "groups",
    # ── Windows cmd / PowerShell 只读命令与别名 ──
    "dir", "where", "findstr", "ver", "vol",
    "cls", "gci", "gc", "gi", "gp", "gl", "gm", "gcm", "sls", "select", "ft", "fl",
    "get-childitem", "get-content", "get-item", "get-itemproperty",
    "get-itempropertyvalue", "get-location", "get-process", "get-service",
    "get-date", "get-command", "get-help", "get-member", "get-module",
    "get-alias", "get-host", "get-variable", "get-history",
    "select-string", "select-object", "measure-object", "sort-object",
    "format-table", "format-list", "format-wide", "out-string", "out-host",
    "write-output", "write-host", "test-path", "resolve-path", "split-path",
    "join-path", "compare-object", "convertto-json", "convertfrom-json",
    # ── 测试 / 静态检查 ──
    "pytest", "tox", "jest", "vitest", "mocha", "rspec", "phpunit",
    "mypy", "ruff", "flake8", "pylint", "eslint", "tsc", "luacheck",
})
# 子命令决定安全性的命令：仅放行其只读子命令（其余子命令落回确认）。
_SAFE_SUBCMD = {
    "git":    frozenset({"status", "diff", "log", "show", "branch", "remote",
                         "ls-files", "rev-parse", "describe", "blame", "shortlog",
                         "tag", "grep", "cat-file", "diff-tree", "whatchanged"}),
    "npm":    frozenset({"test", "run", "ls", "list", "view", "outdated", "why"}),
    "pnpm":   frozenset({"test", "list", "outdated", "why"}),
    "yarn":   frozenset({"test", "list", "outdated", "why"}),
    "pip":    frozenset({"list", "show", "freeze", "check"}),
    "pip3":   frozenset({"list", "show", "freeze", "check"}),
    "cargo":  frozenset({"test", "check", "clippy"}),
    "go":     frozenset({"test", "vet", "list"}),
    "dotnet": frozenset({"test"}),
    "poetry": frozenset({"show"}),
}
# git 的删除/改名/强制类开关——即便子命令只读名单内（branch/tag），带这些也不放行。
_GIT_UNSAFE_FLAGS = frozenset({"-d", "-D", "--delete", "-m", "-M", "--move",
                               "--force", "-f", "--prune"})
# find 的写/执行类开关。
_FIND_UNSAFE_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                                "-fprint", "-fprintf", "-fls"})
# 命令替换 / 子表达式（可藏任意命令）——出现即不放行：
#   bash: $(...) `...` <(...) >(...)；PowerShell: $(...) @(...) 子表达式、` 转义。
_SUBST_RE = _re2.compile(r"\$\(|`|<\(|>\(|@\(")
# 段分隔符（管道也算——每段都要安全）。
_SEG_SPLIT_RE = _re2.compile(r"&&|\|\||[;|]")
# 可执行名后缀（Windows）——匹配命令名时剥掉。
_EXE_SUFFIXES = (".exe", ".bat", ".cmd", ".com", ".ps1")


def _norm_cmd(tok: str) -> str:
    """归一化命令名用于白名单匹配：取 basename、转小写、剥 .exe/.bat 等后缀。
    PowerShell cmdlet/别名大小写不敏感（Dir==dir、Get-ChildItem==get-childitem），故统一小写。"""
    t = (tok or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suf in _EXE_SUFFIXES:
        if t.endswith(suf):
            return t[: -len(suf)]
    return t


def _effective_tokens(toks: list) -> "tuple[str, list]":
    """归一化命令名：`python -m pytest …` 把模块当命令；否则取 leading 的 basename（小写、去后缀）。
    返回 (命令名, 该命令后续参数)。"""
    if toks and _norm_cmd(toks[0]) in ("python", "python3", "py") and len(toks) >= 3 and toks[1] == "-m":
        return _norm_cmd(toks[2]), toks[3:]
    return (_norm_cmd(toks[0]) if toks else ""), toks[1:]


def _segment_safe(seg: str) -> bool:
    """单段命令是否「明显安全」。"""
    toks = seg.split()
    if not toks:
        return False
    cmd, rest = _effective_tokens(toks)
    if cmd in ("sudo", "doas", "su"):       # 提权一律确认
        return False
    if cmd == "git":
        sub = rest[0].lower() if rest else ""
        if sub and sub not in _SAFE_SUBCMD["git"]:
            return False
        if any(f in _GIT_UNSAFE_FLAGS for f in rest[1:]):
            return False
        return True                          # 裸 git（打印帮助）或只读子命令、无危险开关
    if cmd == "find":
        return not any(f in _FIND_UNSAFE_FLAGS for f in rest)
    if cmd in _SAFE_LEADING:
        return True
    if cmd in _SAFE_SUBCMD:
        sub = rest[0].lower() if rest else ""
        return (not sub) or (sub in _SAFE_SUBCMD[cmd])
    return False


def command_is_safe(command: str) -> bool:
    """整条 shell 命令是否「明显安全」（每个串接/管道段都安全、无写重定向、无命令替换/脚本块）。"""
    cmd = (command or "").strip()
    if not cmd:
        return False
    if _SUBST_RE.search(cmd):                # 命令替换 / 子表达式
        return False
    if "{" in cmd or "}" in cmd:             # PowerShell 脚本块 { … } 可藏任意命令（如 where {rm $_}）——保守拦
        return False
    if ">" in cmd:                           # 任何写重定向（含 > >> 2>、PS Out-File 的 >）一律确认
        return False
    segments = [s.strip() for s in _SEG_SPLIT_RE.split(cmd) if s.strip()]
    return bool(segments) and all(_segment_safe(s) for s in segments)


def is_safe_autorun(tool_name: str, params: dict) -> bool:
    """智能确认分级：该工具调用是否可「自动放行」。仅 run_* shell 工具且命令明显安全时为真。"""
    if not tool_name.startswith("run_") or not isinstance(params, dict):
        return False
    return command_is_safe(tool_subject(tool_name, params))
