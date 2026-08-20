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


# ---- 自动放行的对抗性回归 ------------------------------------------------------
# 智能确认分级是**免确认**入口，判错方向不对称：多弹一次只是麻烦，误放行一条毁灭性命令是事故。
# 下面这批是照主流 agent 公开修过的绕过手法逐条打的（Claude Code 2.1.214/2.1.218/2.1.222 那批
# bash 权限检查绕过），2026-08-13 首次跑出 **4 类真绕过**：换行分隔符 / 单个 & / env 当执行器 /
# 无长度上限。修法见 permissions.py 对应注释。**新增白名单命令前先回来加一条对抗用例。**

def test_autorun_separator_bypass():
    """藏在分隔符后面的第二条命令必须让整条落回确认。"""
    # 换行——曾经整条被当成一段，首词 ls 命中白名单就放行了
    assert not command_is_safe("ls\nrm -rf /tmp/x")
    assert not command_is_safe("ls\r\nrm -rf /tmp/x")
    assert not command_is_safe("git status\nsudo rm -rf /")
    # 单个 &：bash 后台执行 / PowerShell 调用操作符
    assert not command_is_safe("ls & rm -rf /tmp/x")
    assert not command_is_safe("ls & rm.exe")
    # 已有防线不能被改坏
    assert not command_is_safe("ls; rm -rf /tmp/x")
    assert not command_is_safe("ls | rm")
    assert not command_is_safe("ls && rm -rf /tmp/x")


def test_autorun_env_is_not_an_executor():
    """env 能只读打印，也能当执行器——只在无参/全是 KEY=VALUE 时放行。"""
    assert not command_is_safe("env rm -rf /tmp/x")
    assert not command_is_safe("env FOO=1 rm -rf /tmp/x")
    assert command_is_safe("env")                    # 裸 env 只打印环境
    assert command_is_safe("env FOO=1 BAR=2")        # 只设变量、没有命令名
    assert command_is_safe("printenv PATH")


def test_autorun_length_cap():
    """超长命令一律确认：白名单判定覆盖不了那么大的构造空间。"""
    assert command_is_safe("echo " + "a" * 100)      # 正常长度照旧放行
    assert not command_is_safe("echo " + "a" * 20000)


def test_autorun_git_write_and_exec_flags():
    """只读子命令 + 一个开关就能写文件/执行命令的组合。"""
    assert not command_is_safe("git grep -O rm foo")        # -O 拿匹配文件喂任意命令
    assert not command_is_safe("git grep -Orm foo")         # 值紧跟的粘连写法
    assert not command_is_safe("git diff --output=/tmp/x")  # 等号粘连、写任意路径
    assert not command_is_safe("git -c core.pager=rm status")
    assert not command_is_safe("git branch -d feat")
    # 正常只读用法不能被误伤
    assert command_is_safe("git status")
    assert command_is_safe("git log --oneline -5")
    assert command_is_safe("git grep foo")
    assert command_is_safe("git diff HEAD~1")


def test_autorun_unicode_padding_is_already_safe():
    """填充空白绕过（JS 侧栽过）在这里天然不成立：Python str.split() 是 Unicode-aware。
    留作证据，防将来有人把切词改成 split(' ') 之类把这个性质弄丢。"""
    for pad in (" ", "　", "\t", "\v"):     # NBSP / 全角空格 / TAB / 垂直制表符
        assert not command_is_safe(f"git{pad}push"), pad
        assert not command_is_safe(f"sudo{pad}rm -rf /"), pad


def test_autorun_existing_defenses_hold():
    """命令替换 / 脚本块 / 重定向 / 提权 / find 写开关——回归防线。"""
    assert not command_is_safe("ls $(rm -rf /)")
    assert not command_is_safe("ls `rm -rf /`")
    assert not command_is_safe("gci | where {rm $_}")
    assert not command_is_safe("ls > /tmp/x")
    assert not command_is_safe("cat a.txt >> b.txt")
    assert not command_is_safe("sudo ls")
    assert not command_is_safe("find . -delete")
    assert not command_is_safe("find . -exec rm {} ;")


def test_autorun_readonly_still_auto_approved():
    """防过度收紧：日常只读命令必须继续免确认，否则又回到确认疲劳。"""
    for cmd in ("ls -la", "pwd", "cat README.md", "grep -rn foo src/", "rg foo",
                "find . -name '*.py'", "pytest tests/", "python -m pytest -q",
                "git status && git diff", "cat a.txt | grep foo | wc -l",
                "Get-ChildItem", "whoami", "npm test"):
        assert command_is_safe(cmd), cmd
    assert is_safe_autorun("run_bash", {"command": "ls -la"})
    assert not is_safe_autorun("write_file", {"path": "a.txt", "content": "x"})


# ---- gate 集成 ---------------------------------------------------------------

def test_always_ask_ignores_allow_all_and_rules():
    """agent 型工具（codex 那类）**每次都问**：既不吃 allow 规则，也不吃「本会话全部允许」。

    2026-08-20 真机：用户点过「全部允许」后，Codex 全程零确认地跑完一整轮。
    那个开关的心智模型是"这些零碎命令我都认"——为单点、可逆、几秒钟的操作设计的，
    用它顺带放开一个能改一堆文件的自主 agent，粒度显然不对。
    """
    emitted = []
    g = PermissionGate(emitted.append, allow=["codex__codex"])
    # 先造出「本会话全部允许」的状态
    assert g.explain("codex__codex", {}) == g.BY_RULE          # 普通口径：规则命中，不问
    assert g.explain("codex__codex", {}, always_ask=True) == g.ASK   # 高影响力：照问不误
    g._allow_all = True
    assert g.explain("run_bash", {"command": "ls"}) == g.BY_SESSION
    assert g.explain("codex__codex", {}, always_ask=True) == g.ASK


def test_always_ask_still_loses_to_deny_and_destructive():
    """放行档次只降不升：deny 规则与毁灭性命令仍然优先拦截，不因为"每次都问"就变成可问可放。"""
    g = PermissionGate(lambda r: None, deny=["codex__codex"])
    assert g.explain("codex__codex", {}, always_ask=True) == g.DENY_RULE
    g2 = PermissionGate(lambda r: None)
    g2._allow_all = True
    assert g2.explain("run_bash", {"command": "rm -rf /"}, always_ask=True) == g2.DESTRUCTIVE


def test_always_ask_offers_no_remember_option():
    """不给「总是允许这类」：codex__codex 没有 path/command 参数，suggest_rule 给的是**裸工具名**，
    点一次＝以后这个自主 agent 干什么都不问，而且**会落盘、重启仍生效**。"""
    emitted = []
    g = PermissionGate(emitted.append)
    import threading
    t = threading.Thread(target=lambda: g.confirm("codex__codex", {"prompt": "x"}, always_ask=True))
    t.start()
    while not emitted:
        pass
    req = emitted[0]
    assert req["suggest"] == "" and req["always"] is True, req
    g.resolve(req["id"], "deny")
    t.join(timeout=5)
    # 普通工具照旧给建议规则
    emitted.clear()
    t2 = threading.Thread(target=lambda: g.confirm("run_bash", {"command": "git status"}))
    t2.start()
    while not emitted:
        pass
    assert emitted[0]["suggest"] and emitted[0]["always"] is False
    g.resolve(emitted[0]["id"], "deny")
    t2.join(timeout=5)


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


# ---- FR-11.4b：规则持久化（可见、可撤、重启仍在）--------------------------

def test_user_permissions_overlay():
    """覆盖层读写：去重、撤销、合并进 config 的 allow（与 config.yaml 手编的并存）。"""
    import tempfile
    from pathlib import Path
    from agentcore.config import (
        add_user_permission, merge_user_permissions, read_user_permissions,
        remove_user_permission,
    )
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "user_permissions.json"
        assert read_user_permissions(f) == []
        add_user_permission("run_bash(futures *)", f)
        add_user_permission("git_status", f)
        add_user_permission("git_status", f)                     # 重复不叠加
        assert read_user_permissions(f) == ["run_bash(futures *)", "git_status"]

        data = merge_user_permissions(
            {"agent": {"permissions": {"allow": ["write_file(docs/*)"], "deny": ["run_bash(rm *)"]}}}, f)
        allow = data["agent"]["permissions"]["allow"]
        assert allow == ["write_file(docs/*)", "run_bash(futures *)", "git_status"], allow
        assert data["agent"]["permissions"]["deny"] == ["run_bash(rm *)"]   # deny 不受面板影响

        remove_user_permission("git_status", f)
        assert read_user_permissions(f) == ["run_bash(futures *)"]


def test_allow_rule_persists():
    """点「总是允许这类」要落盘——以前只进本会话、重启就丢，用户以为放行了却照旧弹窗。"""
    import threading
    from agentcore.agent.gate import ALLOW_RULE, PermissionGate

    saved = []
    emitted = []
    g = PermissionGate(lambda req: emitted.append(req), on_rule_added=saved.append)

    def decide():
        while not emitted:
            pass
        g.resolve(emitted[-1]["id"], ALLOW_RULE)
    threading.Thread(target=decide).start()
    assert g.confirm("run_bash", {"command": "futures momentum --json"}) is True
    assert saved == ["run_bash(futures*)"], saved          # 首词通配，不是放行整个 shell
    # 同类命令这次不再弹确认（本会话内立即生效）
    assert g.confirm("run_bash", {"command": "futures hotspot"}) is True
    assert len(emitted) == 1


def test_persist_failure_does_not_break_operation():
    """落盘失败不能让这次操作失败——放行本身已经是用户的决定。"""
    import threading
    from agentcore.agent.gate import ALLOW_RULE, PermissionGate

    def boom(_rule):
        raise OSError("磁盘满了")

    emitted = []
    g = PermissionGate(lambda req: emitted.append(req), on_rule_added=boom)

    def decide():
        while not emitted:
            pass
        g.resolve(emitted[-1]["id"], ALLOW_RULE)
    threading.Thread(target=decide).start()
    assert g.confirm("run_bash", {"command": "ls"}) is True


def test_gate_set_rules_hot_update():
    """面板加规则后，运行中的会话立即免确认（不必重启）。"""
    from agentcore.agent.gate import PermissionGate
    g = PermissionGate(lambda req: None)
    g.set_rules(allow=["run_bash(futures*)"])
    assert g.confirm("run_bash", {"command": "futures momentum"}) is True
    a, d = g.rules()
    assert a == ["run_bash(futures*)"] and d == []


def test_api_permission_rules():
    """API 层：加/撤/列，以及给 exec 命令推导规则；撤销只认面板加的那些。"""
    import tempfile
    from pathlib import Path
    import agentcore.config as _cfg
    import agentcore.bridge.api as _apimod
    import agentcore.bridge.conversation as _convmod
    from agentcore.bridge import Api
    from agentcore.config import (
        AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
    )
    _apimod.persist_model_selection = lambda **k: None

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_cfg_app, old_conv_app = _cfg.APP_DIR, _convmod.APP_DIR
        _cfg.APP_DIR = tmp / "app"          # 覆盖层写临时目录，别碰真配置
        _convmod.APP_DIR = tmp / "app"
        try:
            ws = tmp / "ws"
            (ws / ".hermes" / "commands").mkdir(parents=True)
            (ws / ".hermes" / "commands" / "动量.md").write_text(
                "---\nmode: exec\ncommand: futures momentum --top 20 --json\n---\n", encoding="utf-8")
            api = Api(AppConfig(
                active_model="m1",
                models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
                agent=AgentConfig(workspaces_root=str(tmp / "root"), auto_conventions=False,
                                  shell="bash", permissions={"allow": ["git_status"],
                                                             "deny": ["run_bash(rm *)"]}),
                storage=StorageConfig(enabled=True, db_path=str(tmp / "h4.db")),
                memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False),
            ))
            conv = api.active
            conv.workspace = ws

            sg = api.suggest_permission_for_command("动量")
            assert sg["ok"] and sg["rule"] == "run_bash(futures*)", sg

            r = api.add_permission(sg["rule"])
            assert r["ok"] and conv.gate.confirm("run_bash", {"command": "futures hotspot"}) is True
            got = api.get_permissions()
            assert sg["rule"] in got["allow"] and sg["rule"] in got["user_allow"]
            assert "git_status" in got["allow"] and "git_status" not in got["user_allow"]
            assert got["deny"] == ["run_bash(rm *)"]

            # config.yaml 手编的规则不许在面板撤（面板只管自己加的）
            assert api.remove_permission("git_status")["ok"] is False
            assert api.remove_permission(sg["rule"])["ok"] is True
            assert sg["rule"] not in api.get_permissions()["allow"]

            assert api.add_permission("这不是规则(")["ok"] in (True, False)   # 不抛异常即可
            api.close()
        finally:
            _cfg.APP_DIR, _convmod.APP_DIR = old_cfg_app, old_conv_app


def test_explain_reasons():
    """裁决原因要能被 UI 说清楚：规则 / 本会话全部允许 / 只读白名单 / 要问 / 拦截。"""
    from agentcore.agent.gate import PermissionGate as G
    g = G(lambda req: None, allow=["run_bash(futures*)"], deny=["run_bash(rm *)"],
          auto_safe=lambda: True)
    assert g.explain("run_bash", {"command": "futures momentum"}) == G.BY_RULE
    assert g.explain("run_bash", {"command": "rm -rf x"}) == G.DENY_RULE
    assert g.explain("run_bash", {"command": "ls -l"}) == G.BY_SAFE          # 只读白名单
    assert g.explain("run_bash", {"command": "python x.py"}) == G.ASK        # 不在白名单 → 要问
    assert g.auto_reason("run_bash", {"command": "python x.py"}) == ""       # 要问就没有"免确认原因"
    assert "只读" in g.auto_reason("run_bash", {"command": "ls -l"})

    # 本会话「全部允许」：原因要与规则区分开——用户点过一次就全免，最该被说明白
    g2 = G(lambda req: None, auto_safe=lambda: False)
    g2._allow_all = True
    assert g2.explain("run_bash", {"command": "python x.py"}) == G.BY_SESSION
    assert "本会话" in g2.auto_reason("run_bash", {"command": "python x.py"})
    # 免确认态下毁灭性命令仍强制拦
    assert g2.explain("run_bash", {"command": "rm -rf /"}) == G.DESTRUCTIVE

    # 关掉智能分级：只读命令也要问（与 C1 用例里"想全部确认就关它"一致）
    g3 = G(lambda req: None, auto_safe=lambda: False)
    assert g3.explain("run_bash", {"command": "ls -l"}) == G.ASK


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
