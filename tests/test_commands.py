"""自定义斜杠命令（FR-13.C1）自检：解析 / 校验 / 参数展开 / 发现与覆盖 / 内置保护。

独立 runner，不依赖 pytest：`python tests/test_commands.py`
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.commands import (  # noqa: E402
    BUILTIN_NAMES, Command, CommandError, command_dirs, discover_commands,
    expand_arguments, parse_command_md, render_exec, render_prompt, validate_name,
)

PROMPT_MD = """---
description: 查今天的期货盯盘数据
skill: futures-monitor
argument-hint: "[动量|热点|资金|形态]"
---
用 futures-monitor 技能查 $ARGUMENTS。没给参数就把动量排名和热点品种都查一遍。
"""

EXEC_MD = """---
description: 直接跑动量排名
mode: exec
command: futures momentum --top $ARGUMENTS --json
---
把输出整理成表，标明数据时点。
"""


def test_parse_prompt_command():
    c = parse_command_md(PROMPT_MD, name="盯盘")
    assert c.name == "盯盘" and c.slash == "/盯盘", c
    assert c.mode == "prompt"                      # 不写 mode 就是 prompt
    assert c.skill == "futures-monitor"
    assert c.argument_hint == "[动量|热点|资金|形态]"
    assert "$ARGUMENTS" in c.body
    print("✓ prompt 命令解析（中文命令名 / 默认 mode / 绑定技能）")


def test_parse_exec_command():
    c = parse_command_md(EXEC_MD, name="动量")
    assert c.mode == "exec"
    assert c.command == "futures momentum --top $ARGUMENTS --json"
    assert c.body.startswith("把输出整理成表")       # 正文＝结果回来后的指示
    print("✓ exec 命令解析")


def test_parse_without_frontmatter():
    # 没 frontmatter 完全合法：整份文件就是提示词模板
    c = parse_command_md("帮我看看今天的持仓变化", name="持仓")
    assert c.mode == "prompt" and c.description == ""
    assert c.body == "帮我看看今天的持仓变化"
    print("✓ 无 frontmatter 时整份文件当模板")


def test_parse_rejects_bad():
    bad = [
        ("exec 模式没写 command", "---\nmode: exec\n---\n随便写点", "动量"),
        ("prompt 模式正文为空", "---\ndescription: 空的\n---\n   \n", "空"),
        ("mode 取值非法", "---\nmode: run\ncommand: ls\n---\n正文", "跑"),
        ("frontmatter 不是键值对", "---\n- a\n- b\n---\n正文", "怪"),
        ("正文超长", "x" * 9000, "长"),
    ]
    for label, text, name in bad:
        try:
            parse_command_md(text, name=name)
        except CommandError:
            continue
        raise AssertionError(f"应当拒绝：{label}")
    print("✓ 非法命令文件一律拒绝（5 种）")


def test_validate_name():
    assert validate_name(" 盯盘 ") == "盯盘"
    assert validate_name("check-deploy") == "check-deploy"
    for bad in ["", "a b", "a/b", "x" * 40, "盯盘.md", "/盯盘"]:
        try:
            validate_name(bad)
        except CommandError:
            continue
        raise AssertionError(f"命令名应被拒绝：{bad!r}")
    print("✓ 命令名校验（中文可用；空白/斜杠/点/超长拒绝）")


def test_expand_arguments():
    assert expand_arguments("查 $ARGUMENTS 排名", "动量") == "查 动量 排名"
    assert expand_arguments("查 $ARGUMENTS 排名", "") == "查  排名"
    # 模板没写占位符但用户给了参数 → 追加到末尾，绝不丢
    out = expand_arguments("看看今天期货", "重点看黑色系")
    assert out.endswith("重点看黑色系"), out
    assert expand_arguments("看看今天期货", "") == "看看今天期货"
    print("✓ $ARGUMENTS 展开（含「模板没占位符也不丢参数」）")


def test_render():
    c = parse_command_md(PROMPT_MD, name="盯盘")
    p = render_prompt(c, "动量")
    assert p.startswith("（使用 `futures-monitor` 技能）"), p     # 绑定技能要点名
    assert "查 动量 排名" not in p and "查 动量。" in p, p
    e = render_exec(parse_command_md(EXEC_MD, name="动量"), "20")
    assert e == "futures momentum --top 20 --json", e
    print("✓ 渲染：prompt 点名技能 / exec 展开命令行")


def test_discover_and_override(tmp: Path):
    glob_dir = tmp / "app" / "commands"
    proj_dir = tmp / "ws" / ".hermes" / "commands"
    glob_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    (glob_dir / "盯盘.md").write_text("---\ndescription: 全局版\n---\n全局提示词", encoding="utf-8")
    (glob_dir / "报表.md").write_text("---\ndescription: 只有全局有\n---\n出报表", encoding="utf-8")
    (proj_dir / "盯盘.md").write_text("---\ndescription: 项目版\n---\n项目提示词", encoding="utf-8")

    dirs = command_dirs(tmp / "ws", tmp / "app")
    cmds, errors = discover_commands(dirs)
    by_name = {c.name: c for c in cmds}
    assert set(by_name) == {"盯盘", "报表"}, by_name
    assert by_name["盯盘"].description == "项目版"      # 项目级覆盖全局
    assert by_name["盯盘"].source == "project"
    assert by_name["报表"].source == "global"
    assert errors == [], errors
    print("✓ 发现与覆盖（项目级 > 全局）")


def test_discover_isolates_bad(tmp: Path):
    d = tmp / "ws" / ".hermes" / "commands"
    d.mkdir(parents=True)
    (d / "好的.md").write_text("正常模板", encoding="utf-8")
    (d / "坏的.md").write_text("---\nmode: exec\n---\n没有 command", encoding="utf-8")
    (d / "readme.txt").write_text("不是 md，忽略", encoding="utf-8")
    cmds, errors = discover_commands(command_dirs(tmp / "ws", tmp / "app"))
    assert [c.name for c in cmds] == ["好的"], cmds
    assert len(errors) == 1 and "坏的.md" in errors[0], errors
    print("✓ 坏命令文件隔离，不拖垮其余")


def test_builtin_not_overridable(tmp: Path):
    d = tmp / "ws" / ".hermes" / "commands"
    d.mkdir(parents=True)
    for n in BUILTIN_NAMES:
        (d / f"{n}.md").write_text("试图覆盖内置命令", encoding="utf-8")
    (d / "自己的.md").write_text("正常模板", encoding="utf-8")
    cmds, errors = discover_commands(command_dirs(tmp / "ws", tmp / "app"))
    assert [c.name for c in cmds] == ["自己的"], cmds
    assert len(errors) == len(BUILTIN_NAMES), errors
    # 关键：/crazy 是免确认自主模式的入口，绝不能被同名文件顶掉
    assert any("crazy" in e for e in errors), errors
    print("✓ 内置命令不可被同名自定义命令覆盖（含 /crazy）")


def test_limit(tmp: Path):
    d = tmp / "ws" / ".hermes" / "commands"
    d.mkdir(parents=True)
    for i in range(5):
        (d / f"c{i}.md").write_text("模板", encoding="utf-8")
    cmds, errors = discover_commands(command_dirs(tmp / "ws", tmp / "app"), limit=3)
    assert len(cmds) == 3, cmds
    assert any("上限" in e for e in errors), errors
    print("✓ 命令数量上限生效")


def test_to_dict_shape():
    d = parse_command_md(PROMPT_MD, name="盯盘").to_dict()
    for k in ("name", "slash", "description", "mode", "body", "command",
              "skill", "argument_hint", "source", "path"):
        assert k in d, k
    assert d["slash"] == "/盯盘"
    print("✓ to_dict 字段齐全（前端按这个形状渲染）")


def test_conversation_integration(tmp: Path):
    """集成：命令真接进 Conversation——发现 / 展开 / exec 过权限 gate 并回事件。"""
    from agentcore.bridge import Api
    from agentcore.config import (
        AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
    )
    import agentcore.bridge.api as _apimod
    _apimod.persist_model_selection = lambda **k: None

    ws = tmp / "ws"
    (ws / ".hermes" / "commands").mkdir(parents=True)
    (ws / ".hermes" / "commands" / "盯盘.md").write_text(
        "---\ndescription: 查盯盘\nskill: futures-monitor\n---\n查 $ARGUMENTS 排名。", encoding="utf-8")
    (ws / ".hermes" / "commands" / "回声.md").write_text(
        "---\nmode: exec\ncommand: echo $ARGUMENTS\n---\n", encoding="utf-8")

    events: list = []
    api = Api(AppConfig(
        active_model="m1",
        models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
        agent=AgentConfig(workspaces_root=str(tmp / "root"), auto_conventions=False,
                          shell="bash", permissions={"allow": ["run_bash(echo *)"]}),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
        memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False),
    ))
    conv = api.active
    conv.workspace = ws
    conv._build_registry()
    conv.emit = lambda ev, data=None: events.append((ev, data))

    r = api.get_commands()
    names = sorted(c["name"] for c in r["commands"])
    assert names == ["回声", "盯盘"], r
    assert r["errors"] == [], r

    # prompt 模式：展开 + 绑定技能点名
    e = api.expand_command("盯盘", "动量")
    assert e["ok"] and e["mode"] == "prompt", e
    assert "查 动量 排名。" in e["text"] and "futures-monitor" in e["text"], e

    # exec 模式：真跑一条命令（allow 规则已放行 echo，不弹确认），事件如实回来
    e2 = api.expand_command("回声", "hi")
    assert e2["mode"] == "exec" and e2["command"] == "echo hi", e2
    assert api.run_command("回声", "hi")["ok"]
    for _ in range(100):
        if any(ev == "command_done" for ev, _d in events):
            break
        time.sleep(0.05)
    done = [d for ev, d in events if ev == "command_done"]
    assert done and done[0]["ok"], events
    assert "hi" in done[0]["output"], done
    assert any(ev == "command_start" for ev, _d in events), events

    # 不存在的命令要如实报错，不能静默当成没事
    assert api.expand_command("不存在", "")["ok"] is False
    api.close()
    print("✓ 集成：Conversation 发现/展开/执行（exec 走 gate + 事件）")


def test_exec_denied_by_gate(tmp: Path):
    """权限 gate 拒绝时，命令不执行且如实回报——不能悄悄当成'跑完没输出'。"""
    from agentcore.bridge import Api
    from agentcore.config import (
        AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
    )
    import agentcore.bridge.api as _apimod
    _apimod.persist_model_selection = lambda **k: None

    ws = tmp / "ws"
    (ws / ".hermes" / "commands").mkdir(parents=True)
    (ws / ".hermes" / "commands" / "危险.md").write_text(
        "---\nmode: exec\ncommand: echo nope\n---\n", encoding="utf-8")

    events: list = []
    api = Api(AppConfig(
        active_model="m1",
        models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
        agent=AgentConfig(workspaces_root=str(tmp / "root"), auto_conventions=False,
                          shell="bash", permissions={"deny": ["run_bash(echo *)"]}),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h2.db")),
        memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False),
    ))
    conv = api.active
    conv.workspace = ws
    conv._build_registry()
    conv.emit = lambda ev, data=None: events.append((ev, data))

    assert api.run_command("危险", "")["ok"]
    for _ in range(100):
        if any(ev == "command_done" for ev, _d in events):
            break
        time.sleep(0.05)
    done = [d for ev, d in events if ev == "command_done"]
    assert done and done[0]["ok"] is False and "拒绝" in done[0]["error"], events
    api.close()
    print("✓ gate 拒绝时如实回报（命令不因为是自己写的就免确认）")


def test_build_command_md_roundtrip():
    """管理面拼出来的文件必须能被自己解析回去（写盘前会回读校验，这里钉住这个性质）。"""
    from agentcore.commands import build_command_md
    spec = {"description": '带"引号"和: 冒号的说明', "mode": "prompt",
            "skill": "futures-monitor", "argument_hint": "[动量|热点]",
            "body": "查 $ARGUMENTS 排名。"}
    c = parse_command_md(build_command_md(spec), name="盯盘")
    assert c.description == '带"引号"和: 冒号的说明', c.description   # YAML 元字符不能把文件写坏
    assert c.skill == "futures-monitor" and c.argument_hint == "[动量|热点]"
    assert c.mode == "prompt" and "$ARGUMENTS" in c.body

    e = parse_command_md(build_command_md(
        {"mode": "exec", "command": "futures momentum --json", "body": ""}), name="动量")
    assert e.mode == "exec" and e.command == "futures momentum --json" and e.body == ""
    # 空字段不写进 frontmatter（留空键是噪音）
    assert "skill:" not in build_command_md({"mode": "prompt", "body": "x"})
    print("✓ build_command_md 往返（含 YAML 元字符）")


def test_save_and_delete(tmp: Path):
    """管理面存/删：项目级与全局各存一条、坏内容拒绝落盘、删除有越界检查。"""
    from agentcore.bridge import Api
    from agentcore.config import (
        AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
    )
    import agentcore.bridge.api as _apimod
    import agentcore.bridge.conversation as _convmod
    _apimod.persist_model_selection = lambda **k: None
    _convmod.APP_DIR = tmp / "app"          # 全局命令目录指到临时目录，别写进真项目

    ws = tmp / "ws"
    ws.mkdir(parents=True)
    api = Api(AppConfig(
        active_model="m1",
        models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
        agent=AgentConfig(workspaces_root=str(tmp / "root"), auto_conventions=False, shell="bash"),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h3.db")),
        memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False),
    ))
    conv = api.active
    conv.workspace = ws

    r = api.save_command("盯盘", {"description": "查盯盘", "body": "查 $ARGUMENTS 排名。"}, "project")
    assert r["ok"] and (ws / ".hermes" / "commands" / "盯盘.md").is_file(), r
    r2 = api.save_command("报表", {"description": "全局的", "body": "出报表"}, "global")
    assert r2["ok"] and (tmp / "app" / "commands" / "报表.md").is_file(), r2

    got = {c["name"]: c for c in api.get_commands()["commands"]}
    assert set(got) == {"盯盘", "报表"}, got
    assert got["盯盘"]["source"] == "project" and got["报表"]["source"] == "global"

    # 存不出合法命令的内容：直接拒，不留幽灵文件
    bad = api.save_command("坏的", {"mode": "exec", "command": "", "body": "x"}, "project")
    assert not bad["ok"] and "command" in bad["error"], bad
    assert not (ws / ".hermes" / "commands" / "坏的.md").exists()

    # 内置命令名不许占用
    assert not api.save_command("crazy", {"body": "冒充"}, "project")["ok"]

    # 删除：正常删 + 越界名拒绝 + 删不存在的如实报错
    assert api.delete_command("盯盘")["ok"]
    assert not (ws / ".hermes" / "commands" / "盯盘.md").exists()
    assert not api.delete_command("../../etc/passwd")["ok"]
    assert not api.delete_command("没有的")["ok"]
    api.close()
    print("✓ 管理面存/删（项目级+全局、拒绝坏内容与内置名、删除越界检查）")


def main() -> int:
    import tempfile

    tests = [
        test_parse_prompt_command, test_parse_exec_command, test_parse_without_frontmatter,
        test_parse_rejects_bad, test_validate_name, test_expand_arguments, test_render,
        test_to_dict_shape, test_build_command_md_roundtrip,
    ]
    tmp_tests = [
        test_discover_and_override, test_discover_isolates_bad,
        test_builtin_not_overridable, test_limit,
        test_conversation_integration, test_exec_denied_by_gate, test_save_and_delete,
    ]
    n = 0
    for t in tests:
        t()
        n += 1
    for t in tmp_tests:
        with tempfile.TemporaryDirectory() as td:
            t(Path(td))
        n += 1
    print(f"\ntest_commands: {n}/{n} 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
