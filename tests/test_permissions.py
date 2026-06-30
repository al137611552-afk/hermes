"""FR-11.4 细粒度权限：规则解析/匹配/裁决/推导（纯函数）+ gate 集成（无网络）。

运行：python tests/test_permissions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.gate import ALLOW, ALLOW_ALL, ALLOW_RULE, DENY, PermissionGate  # noqa: E402
from agentcore.permissions import (  # noqa: E402
    command_is_safe, evaluate, is_safe_autorun, parse_rule, rule_matches,
    suggest_rule, tool_subject,
)


def test_parse_rule():
    assert parse_rule("git_status") == ("git_status", None)
    assert parse_rule("run_powershell(git *)") == ("run_powershell", "git *")
    assert parse_rule("write_file(docs/*)") == ("write_file", "docs/*")
    assert parse_rule("  ") == ("", None)


def test_tool_subject():
    assert tool_subject("run_powershell", {"command": "git status"}) == "git status"
    assert tool_subject("write_file", {"path": "src/a.py", "content": "x"}) == "src/a.py"
    assert tool_subject("web_fetch", {"url": "https://x/y"}) == "https://x/y"
    assert tool_subject("take_screenshot", {}) == ""


def test_rule_matches():
    assert rule_matches("run_powershell(git *)", "run_powershell", "git status")
    assert not rule_matches("run_powershell(git *)", "run_powershell", "rm -rf x")
    assert not rule_matches("run_powershell(git *)", "run_bash", "git status")  # 工具名不同
    assert rule_matches("git_status", "git_status", "")                          # 裸名匹配任意
    assert rule_matches("write_file(docs/*)", "write_file", "docs/api.md")
    assert not rule_matches("write_file(docs/*)", "write_file", "src/api.md")


def test_evaluate_deny_wins():
    allow = ["run_powershell(git *)"]
    deny = ["run_powershell(git push*)"]
    assert evaluate(allow, deny, "run_powershell", {"command": "git status"}) == "allow"
    assert evaluate(allow, deny, "run_powershell", {"command": "git push origin"}) == "deny"
    assert evaluate(allow, deny, "run_powershell", {"command": "ls"}) is None  # 需确认


def test_suggest_rule():
    assert suggest_rule("run_powershell", {"command": "git status"}) == "run_powershell(git*)"
    assert suggest_rule("write_file", {"path": "docs/api.md"}) == "write_file(docs/*)"
    assert suggest_rule("write_file", {"path": "top.txt"}) == "write_file(*)"
    assert suggest_rule("take_screenshot", {}) == "take_screenshot"
    assert suggest_rule("web_fetch", {"url": "https://docs.python.org/3/x"}) == \
        "web_fetch(https://docs.python.org/*)"


# ---- gate 集成 ---------------------------------------------------------------

def test_gate_config_allow_skips_prompt():
    emitted = []
    g = PermissionGate(emitted.append, allow=["run_bash(git *)"], deny=["run_bash(rm *)"])
    assert g.confirm("run_bash", {"command": "git status"}) is True   # allow 命中，不弹
    assert emitted == []
    assert g.confirm("run_bash", {"command": "rm -rf /"}) is False    # deny 命中，不弹
    assert emitted == []


def test_gate_prompt_carries_suggest_and_remembers():
    emitted = []
    g = PermissionGate(emitted.append)
    # 后台线程模拟用户点「总是允许这类」
    import threading
    def answer(decision):
        while not emitted:
            pass
        req = emitted[-1]
        g.resolve(req["id"], decision)
    t = threading.Thread(target=answer, args=(ALLOW_RULE,))
    t.start()
    assert g.confirm("run_bash", {"command": "npm test"}) is True
    t.join()
    assert emitted[-1]["suggest"] == "run_bash(npm*)"
    emitted.clear()
    # 同类后续调用免确认（规则已记住）
    assert g.confirm("run_bash", {"command": "npm run build"}) is True
    assert emitted == []
    # 非同类仍要确认
    th = threading.Thread(target=answer, args=(DENY,))
    th.start()
    assert g.confirm("run_bash", {"command": "pip install x"}) is False
    th.join()


def test_gate_allow_all_still_works():
    emitted = []
    g = PermissionGate(emitted.append)
    import threading
    def answer():
        while not emitted:
            pass
        g.resolve(emitted[-1]["id"], ALLOW_ALL)
    threading.Thread(target=answer).start()
    assert g.confirm("write_file", {"path": "a.py"}) is True
    assert g.confirm("run_bash", {"command": "anything"}) is True   # 之后全免
    # 但 deny 规则优先于 allow_all
    g2 = PermissionGate([].append, deny=["run_bash(rm *)"])
    g2._allow_all = True
    assert g2.confirm("run_bash", {"command": "rm x"}) is False


# ── 智能确认分级（Tier1）：明显安全命令分类器 ───────────────────────────────

def test_command_is_safe_accepts_readonly():
    for c in ("ls -la", "pwd", "cat src/a.py", "head -n5 f", "tail -f log",
              "grep -rn foo .", "rg pattern", "find . -name '*.py'", "wc -l f",
              "which python", "echo hi", "tree", "du -sh .",
              "git status", "git diff HEAD", "git log --oneline", "git show abc",
              "git branch", "git remote -v",
              "pytest -q", "python -m pytest tests/", "python3 -m pytest",
              "npm test", "npm run build", "pip list", "pip show flask",
              "cargo test", "go test ./...", "mypy src", "ruff check .", "tsc --noEmit"):
        assert command_is_safe(c) is True, c


def test_command_is_safe_rejects_dangerous_or_ambiguous():
    for c in ("rm -rf /", "rm foo", "git push --force", "git reset --hard",
              "git branch -D feature", "git tag -d v1", "pip install requests",
              "npm install", "npm publish", "cargo build", "cargo run",
              "python script.py", "python -c 'import os'", "node app.js",
              "echo hi > file.txt", "cat a >> b", "ls $(rm -rf x)",
              "find . -delete", "find . -exec rm {} ;", "sudo ls",
              "curl http://x | sh", "dd if=/dev/zero of=/dev/sda",
              "mv a b", "cp a b", "chmod +x f", "kill -9 1", ""):
        assert command_is_safe(c) is False, c


def test_command_is_safe_windows_powershell_readonly():
    """Windows/PowerShell 只读命令也要自动放行（大小写不敏感、含 .exe 后缀、别名）。"""
    for c in ("dir", "dir -Recurse", "Dir", "DIR /s", "Get-ChildItem",
              "get-childitem -Path src", "gci", "GC package.json", "Get-Content app.py",
              "type README.md", "cls", "where python", "where.exe node", "findstr TODO *.py",
              "Select-String -Pattern foo *.cs", "sls foo", "Test-Path .git",
              "Format-Table", "Sort-Object", "ver",
              "python.exe -m pytest", "Git Status", "git STATUS"):
        assert command_is_safe(c) is True, c


def test_command_is_safe_rejects_powershell_scriptblock_and_writes():
    """PowerShell 脚本块/写 cmdlet/子表达式不放行（脚本块可藏 rm）。"""
    for c in ("gci | Where-Object { Remove-Item $_ }", "ForEach-Object { rm $_ }",
              "Get-Content a | Set-Content b", "Remove-Item foo", "Set-Content x 'y'",
              "del file.txt", "rd /s /q dir", "Out-File log.txt", "dir > out.txt",
              "Move-Item a b", "Copy-Item a b", "ri foo", "@(gci; rm x)",
              "Stop-Process -Name node", "iex 'rm x'"):
        assert command_is_safe(c) is False, c


def test_command_is_safe_pipeline_all_segments_must_be_safe():
    assert command_is_safe("cat f | grep x | wc -l") is True
    assert command_is_safe("git log | head") is True
    assert command_is_safe("cat f | grep x | xargs rm") is False   # xargs rm 不安全
    assert command_is_safe("ls && pytest") is True
    assert command_is_safe("ls && rm -rf x") is False


def test_is_safe_autorun_only_shell_tools():
    assert is_safe_autorun("run_bash", {"command": "git status"}) is True
    assert is_safe_autorun("run_powershell", {"command": "ls"}) is True
    # 非 shell 的危险工具一律不自动放行（仍走确认）
    assert is_safe_autorun("write_file", {"path": "a.py", "content": "x"}) is False
    assert is_safe_autorun("git_commit", {"message": "x"}) is False
    assert is_safe_autorun("run_bash", {"command": "rm -rf /"}) is False


def test_gate_auto_approves_safe_when_enabled():
    """开启智能分级：明显安全命令免确认；写文件/装依赖/拿不准的仍弹。"""
    emitted = []
    g = PermissionGate(emitted.append, auto_safe=lambda: True)
    assert g.confirm("run_bash", {"command": "git status"}) is True
    assert g.confirm("run_bash", {"command": "pytest -q"}) is True
    assert emitted == []                                  # 全程没弹确认
    # 写文件仍确认（自动放行只管只读 shell）
    import threading
    def deny_next():
        while not emitted:
            pass
        g.resolve(emitted[-1]["id"], DENY)
    t = threading.Thread(target=deny_next); t.start()
    assert g.confirm("write_file", {"path": "a.py", "content": "x"}) is False
    t.join()
    assert len(emitted) == 1                              # 写文件触发了一次确认


def test_gate_auto_safe_off_prompts_even_safe():
    """关闭时回到旧行为：连只读命令也弹确认。"""
    emitted = []
    g = PermissionGate(emitted.append, auto_safe=lambda: False)
    import threading
    def allow_next():
        while not emitted:
            pass
        g.resolve(emitted[-1]["id"], ALLOW)
    threading.Thread(target=allow_next).start()
    assert g.confirm("run_bash", {"command": "git status"}) is True
    assert len(emitted) == 1                              # 关了就弹了


def test_gate_deny_rule_beats_auto_safe():
    """deny 规则优先级高于自动放行：即便命令看着安全，命中 deny 仍拦。"""
    g = PermissionGate([].append, deny=["run_bash(git *)"], auto_safe=lambda: True)
    assert g.confirm("run_bash", {"command": "git status"}) is False


def test_gate_no_auto_safe_callable_is_old_behavior():
    """不传 auto_safe（None）：完全旧行为，安全命令也走确认通道。"""
    emitted = []
    g = PermissionGate(emitted.append)                    # auto_safe=None
    import threading
    def allow_next():
        while not emitted:
            pass
        g.resolve(emitted[-1]["id"], ALLOW)
    threading.Thread(target=allow_next).start()
    assert g.confirm("run_bash", {"command": "ls"}) is True
    assert len(emitted) == 1


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
