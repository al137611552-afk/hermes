"""技能包（FR-13.S）自检：SKILL.md 解析/校验、发现与覆盖、渐进披露的两层拼块、load_skill 工具。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.skills import (  # noqa: E402
    MAX_BODY_CHARS,
    Skill,
    SkillError,
    build_skill_body_block,
    build_skills_block,
    discover_skills,
    list_skill_files,
    load_skill_body,
    normalize_name,
    parse_skill_md,
    skill_dirs,
    truncate_body,
    validate_name,
)
from agentcore.tools.base import ToolError  # noqa: E402
from agentcore.tools.skills import LoadSkillTool, SkillBinding  # noqa: E402

MINIMAL = """---
name: demo-skill
description: 演示技能。用于自检。
---

# 步骤
1. 读取输入
2. 产出结果
"""


def write_skill(root: Path, name: str, *, desc: str = "演示技能。用于自检。", body: str = "正文",
                extra_fm: str = "") -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {desc}\n{extra_fm}---\n\n{body}\n"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    return d


def test_parse_minimal():
    s = parse_skill_md(MINIMAL)
    assert s.name == "demo-skill"
    assert s.description == "演示技能。用于自检。"
    assert "读取输入" in s.body
    assert s.allowed_tools == () and s.metadata == {}
    print("✓ 最小 SKILL.md 解析")


def test_parse_optional_fields():
    text = """---
name: pdf-processing
description: 处理 PDF。
license: Apache-2.0
compatibility: 需要 python3 与 pypdf
metadata:
  author: example-org
  version: "1.0"
allowed-tools: read_file run_bash
---

body
"""
    s = parse_skill_md(text)
    assert s.license == "Apache-2.0"
    assert s.compatibility.startswith("需要 python3")
    assert s.metadata == {"author": "example-org", "version": "1.0"}
    assert s.allowed_tools == ("read_file", "run_bash")
    # 容忍 YAML 列表写法（社区里两种都有）
    s2 = parse_skill_md(text.replace(
        "allowed-tools: read_file run_bash", "allowed-tools:\n  - read_file\n  - run_bash"))
    assert s2.allowed_tools == ("read_file", "run_bash")
    print("✓ 可选字段 + allowed-tools 两种写法")


def test_parse_rejects_invalid():
    bad = [
        ("没有 frontmatter", "# 只有正文"),
        ("缺 name", "---\ndescription: x\n---\n"),
        ("缺 description", "---\nname: a\n---\n"),
        ("frontmatter 非映射", "---\n- a\n- b\n---\n"),
        ("name 归一化后为空", "---\nname: 「」\ndescription: x\n---\n"),
    ]
    for label, text in bad:
        try:
            parse_skill_md(text)
        except SkillError:
            continue
        raise AssertionError(f"应拒绝：{label}")
    # strict=True（校验我们自己写的技能）时，命名不合规范仍要拒
    for label, text in [
        ("name 大写", "---\nname: Demo\ndescription: x\n---\n"),
        ("name 连续连字符", "---\nname: a--b\ndescription: x\n---\n"),
        ("name 起止连字符", "---\nname: -a\ndescription: x\n---\n"),
    ]:
        try:
            parse_skill_md(text, strict=True)
        except SkillError:
            continue
        raise AssertionError(f"strict 模式应拒绝：{label}")
    # 超长 description：**自己的技能**（strict）仍必须报错
    try:
        parse_skill_md(f"---\nname: a\ndescription: {'x' * 1025}\n---\n", strict=True)
        raise AssertionError("strict 应拒绝超长 description")
    except SkillError:
        pass
    assert validate_name("pdf-processing") == "pdf-processing"
    print("✓ 不合规范的 SKILL.md 被拒（5 类基本 + 3 类 strict 命名 + 超长 description）")


def test_long_description_truncated_for_third_party():
    """**第三方超长 description 截断而不是拒收**（接收宽容、产出严格，同 name 的处理）。

    2026-08-07 实测：Anthropic 官方 `anthropics/skills` 里的 `claude-api` 技能 description 超 1024，
    原来直接 SkillError＝整个技能装不上（官方仓库 17 个技能里被挡掉 1 个）。description 会进
    system prompt，限长是为守上下文预算——截断同样守得住，没必要把技能整个丢掉。
    **别改回拒收**。
    """
    long_desc = "这是一句说明。" * 200          # 1400 字符，远超 1024
    s = parse_skill_md(f"---\nname: a\ndescription: {long_desc}\n---\n")
    assert len(s.description) <= 1024, len(s.description)
    assert s.description.endswith("…"), "要能看出被裁过"
    assert s.description.startswith("这是一句说明。")
    # 在句末断开，不留半截句子
    assert s.description.rstrip("…").endswith("。")
    # 没有句末标点可断时也不能崩，硬截即可
    s2 = parse_skill_md(f"---\nname: b\ndescription: {'x' * 2000}\n---\n")
    assert len(s2.description) <= 1024 and s2.description.endswith("…")
    # compatibility 同理（上限 500）：第三方超长截断、自己的报错
    s3 = parse_skill_md(f"---\nname: c\ndescription: d\ncompatibility: {'y' * 600}\n---\n")
    assert len(s3.compatibility) <= 501 and s3.compatibility.endswith("…")
    try:
        parse_skill_md(f"---\nname: c\ndescription: d\ncompatibility: {'y' * 600}\n---\n",
                       strict=True)
        raise AssertionError("strict 应拒绝超长 compatibility")
    except SkillError:
        pass


def test_name_normalized_for_third_party():
    """真跑实测：Anthropic 官方 plugin-dev 的 7 个技能 name 全是「Agent Development」这种写法，
    严格按规范拒绝＝装不了绝大多数真实技能。故读第三方时归一化，写自己的仍守规范。"""
    s = parse_skill_md("---\nname: Agent Development\ndescription: x\n---\n")
    assert s.name == "agent-development", s.name
    for raw, want in [
        ("MCP Integration", "mcp-integration"),
        ("plugin_settings", "plugin-settings"),
        ("  Hook   Development  ", "hook-development"),
        ("a--b", "a-b"),                  # 连续连字符压成一个
        ("-lead-", "lead"),               # 起止连字符去掉
        ("PDF处理", "pdf"),                # 非 ASCII 丢掉，剩余部分仍可用
        ("already-ok", "already-ok"),     # 本就合规的不动
    ]:
        assert normalize_name(raw) == want, (raw, normalize_name(raw))
    # 全是非 ASCII → 归一不出来，返回空串由调用方报错（不能瞎编一个名字）
    assert normalize_name("中文技能") == ""
    try:
        parse_skill_md("---\nname: 中文技能\ndescription: x\n---\n")
        raise AssertionError("归一化后为空应报错")
    except SkillError as e:
        assert "归一化后为空" in str(e)
    print("✓ 第三方技能名归一化（治「官方插件自己都不守规范」的现实）")


def test_parse_tolerates_bom_and_crlf():
    s = parse_skill_md("﻿---\r\nname: a-b\r\ndescription: x\r\n---\r\n\r\n正文\r\n")
    assert s.name == "a-b" and "正文" in s.body
    print("✓ 容忍 BOM 与 CRLF（Windows 记事本存的技能包）")


def test_discover_and_override(tmp: Path):
    g, p = tmp / "global" / "skills", tmp / "ws" / ".hermes" / "skills"
    write_skill(g, "alpha", desc="全局版本。")
    write_skill(g, "beta", desc="只有全局有。")
    write_skill(p, "alpha", desc="项目版本。")
    skills, errors = discover_skills([(g, "global"), (p, "project")])
    assert [s.name for s in skills] == ["alpha", "beta"]
    by = {s.name: s for s in skills}
    assert by["alpha"].description == "项目版本。" and by["alpha"].source == "project"
    assert by["beta"].source == "global"
    assert errors == []
    print("✓ 多目录发现 + 项目级同名覆盖全局")


def test_discover_isolates_bad_skill(tmp: Path):
    root = tmp / "skills"
    write_skill(root, "good")
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "SKILL.md").write_text("# 没有 frontmatter", encoding="utf-8")
    (root / "mismatch").mkdir(parents=True)
    (root / "mismatch" / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: x\n---\n", encoding="utf-8")
    (root / "not-a-skill").mkdir(parents=True)   # 没有 SKILL.md，静默跳过
    skills, errors = discover_skills([(root, "project")])
    # name 与目录名不一致 → 回退用目录名（同 Claude Code），而不是把技能丢掉
    assert sorted(s.name for s in skills) == ["good", "mismatch"], [s.name for s in skills]
    assert len(errors) == 1 and "broken" in errors[0], errors
    # 目录不存在不报错、返回空
    assert discover_skills([(tmp / "nope", "global")]) == ([], [])
    print("✓ 坏技能包被隔离跳过（不拖垮其余）+ 目录不存在不报错")


def test_progressive_disclosure_blocks(tmp: Path):
    d = write_skill(tmp / "skills", "report-writing", desc="写调研报告。需要出报告时用。",
                    body="## 步骤\n先检索再落笔。")
    (d / "scripts" / "build.py").write_text("print(1)", encoding="utf-8")
    (d / "references").mkdir()
    (d / "references" / "REFERENCE.md").write_text("细节", encoding="utf-8")
    skills, _ = discover_skills([(tmp / "skills", "project")])

    # 第一层：只有 name + description，不含正文
    block = build_skills_block(skills)
    assert "report-writing" in block and "写调研报告" in block
    assert "先检落笔" not in block and "先检索再落笔" not in block
    assert build_skills_block([]) is None

    # 第二层：正文 + 附带文件清单
    loaded = load_skill_body(skills[0])
    files = list_skill_files(loaded)
    assert any(f.endswith("build.py") for f in files)
    assert any(f.endswith("REFERENCE.md") for f in files)
    body_block = build_skill_body_block(loaded, files)
    assert "先检索再落笔" in body_block and "build.py" in body_block
    assert "参考资料" in body_block   # 不可信内容标注
    print("✓ 渐进披露两层：清单块不含正文、正文块含资源清单与安全标注")


def test_body_truncated():
    long_body = "x" * (MAX_BODY_CHARS + 500)
    out = truncate_body(long_body)
    assert len(out) < len(long_body) and "已截断" in out
    assert truncate_body("短") == "短"
    print("✓ 超长正文截断")


def test_allowed_tools_not_auto_approved(tmp: Path):
    write_skill(tmp / "skills", "risky", extra_fm="allowed-tools: run_bash\n")
    skills, _ = discover_skills([(tmp / "skills", "project")])
    block = build_skill_body_block(load_skill_body(skills[0]), [])
    # 关键安全立场：allowed-tools 只展示，明确写明不等于免确认
    assert "不代表免确认" in block
    print("✓ allowed-tools 只展示、不用于免确认（安全立场落到文案）")


def test_load_skill_tool(tmp: Path):
    write_skill(tmp / "skills", "alpha", body="alpha 的做法")
    skills, _ = discover_skills([(tmp / "skills", "project")])
    tool = LoadSkillTool(SkillBinding(lambda: skills))
    assert tool.dangerous is False
    out = tool.run({"name": "alpha"})
    assert "alpha 的做法" in out
    for bad, why in [({"name": "nope"}, "未知技能"), ({"name": ""}, "空名")]:
        try:
            tool.run(bad)
        except ToolError:
            continue
        raise AssertionError(f"应报错：{why}")
    # 未知技能的报错要带可用技能列表（模型据此自我纠正）
    try:
        tool.run({"name": "nope"})
    except ToolError as e:
        assert "alpha" in str(e)
    print("✓ load_skill 工具：读正文 / 未知技能报错带可用列表 / 非危险")


def test_skill_dirs_order(tmp: Path):
    dirs = skill_dirs(tmp / "ws", tmp / "app", ["/custom/skills"])
    assert [s for _, s in dirs] == ["global", "config", "project"]   # 靠后优先
    assert dirs[0][0] == tmp / "app" / "skills"
    assert dirs[-1][0] == tmp / "ws" / ".hermes" / "skills"
    # 打包模式：内置目录（BUNDLE_DIR）排最前、优先级最低，用户可用同名技能覆盖它
    frozen = skill_dirs(tmp / "ws", tmp / "app", None, tmp / "bundle")
    assert [s for _, s in frozen] == ["builtin", "global", "project"]
    # 源码模式 BUNDLE_DIR == APP_DIR：同一目录不重复扫
    same = skill_dirs(tmp / "ws", tmp / "app", None, tmp / "app")
    assert [s for _, s in same] == ["builtin", "project"]
    print("✓ 技能目录查找顺序：内置 → 全局 → 配置 → 项目（项目级最优先，同路径去重）")


def test_load_skill_reads_latest(tmp: Path):
    """发现阶段已解析过一遍，load_skill 仍重读磁盘——改完技能不必重启。"""
    d = write_skill(tmp / "skills", "alpha", body="旧正文")
    skills, _ = discover_skills([(tmp / "skills", "project")])
    (d / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n\n新正文\n", encoding="utf-8")
    assert "新正文" in load_skill_body(skills[0]).body
    print("✓ load_skill 重读磁盘（改技能即时生效、不必重启）")


def test_conversation_integration(tmp: Path):
    """集成：技能真接进 Conversation——清单注入 system、load_skill 进注册表、关掉即消失。"""
    from agentcore.bridge import Api
    from agentcore.config import (
        AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
    )
    import agentcore.bridge.api as _apimod
    _apimod.persist_model_selection = lambda **k: None

    def make(skills_on: bool) -> Api:
        return Api(AppConfig(
            active_model="m1",
            models={"m1": ModelConfig(provider="anthropic", model="x", api_key_env="K")},
            agent=AgentConfig(
                workspaces_root=str(tmp / "ws"), auto_conventions=False,
                skills=skills_on, skills_dirs=[str(tmp / "skills")],
            ),
            storage=StorageConfig(enabled=True, db_path=str(tmp / f"h{skills_on}.db")),
            memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False),
        ))

    write_skill(tmp / "skills", "report-writing", desc="写调研报告。需要出报告时用。",
                body="## 步骤\n先检索再落笔。")
    api = make(True)
    conv = api.active
    names = [s.name for s in conv._skills]
    assert "report-writing" in names          # 配置的额外目录
    assert "research-report" in names          # 内置技能（<APP_DIR>/skills）开箱可用
    system = conv._effective_system()
    assert "可用技能" in system and "report-writing" in system
    assert "先检索再落笔" not in system      # 正文不进 system（渐进披露的意义所在）
    assert "load_skill" in conv.registry.names()
    assert conv.get_skills()["skills"][0]["name"] == "report-writing"
    # 子 Agent 也能读技能（只读白名单内）
    from agentcore.tools.delegate import ROLES
    assert "load_skill" in conv._subagent_registry(ROLES["researcher"]).names()
    # 安全主张的结构性断言：技能声明 allowed-tools 也**不能**让危险工具变免确认。
    write_skill(tmp / "skills", "privileged", extra_fm="allowed-tools: write_file run_bash\n")
    conv._refresh_skills()
    assert "privileged" in [s.name for s in conv._skills]
    for danger in ("write_file", "edit_file"):
        assert conv.registry.is_dangerous(danger), f"{danger} 不该因技能声明而变成非危险"
    assert conv.registry.is_dangerous("load_skill") is False   # 技能加载本身只读
    assert conv.gate._allow_all is False                        # 技能不碰 gate 的免确认开关
    api.close()

    off = make(False)                        # 关掉：不扫、不注入、不注册工具
    assert off.active._skills == []
    assert "load_skill" not in off.active.registry.names()
    assert "可用技能" not in (off.active._effective_system() or "")
    off.close()
    print("✓ 集成：清单注入 system（正文不进）/ load_skill 注册 / 子 Agent 可用 / 开关生效")


# ---- 内置技能：research-report ------------------------------------------

BUILTIN_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def test_builtin_skills_valid():
    """内置技能包本身必须合规范（否则用户一启动就看到错误）。"""
    skills, errors = discover_skills([(BUILTIN_SKILLS, "global")])
    assert errors == [], f"内置技能包有问题：{errors}"
    assert any(s.name == "research-report" for s in skills)
    # 「产出时严格」：我们自己写的技能必须完全合规范（第三方才走归一化）
    for s in skills:
        parse_skill_md((s.path / "SKILL.md").read_text(encoding="utf-8"), strict=True)
        assert s.name == s.path.name, f"内置技能 name 必须与目录名一致：{s.name} vs {s.path.name}"
    rr = next(s for s in skills if s.name == "research-report")
    loaded = load_skill_body(rr)
    assert len(loaded.body) < MAX_BODY_CHARS      # 未被截断
    assert loaded.body.count("\n") < 500          # 规范建议主文件 ≤500 行
    files = list_skill_files(loaded)
    assert any(f.endswith("check_report.py") for f in files)
    assert any(f.endswith("SOURCING.md") for f in files)
    assert any(f.endswith("report-template.md") for f in files)
    print("✓ 内置技能 research-report 合规范、三类资源齐全")


def _checker():
    import importlib.util
    p = BUILTIN_SKILLS / "research-report" / "scripts" / "check_report.py"
    spec = importlib.util.spec_from_file_location("check_report", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOOD_REPORT = """# 某技术调研

> 信息时点：截至 2026-08 ｜ 调研日期：2026-08-07

## 结论
建议采用方案 A。

## 关键发现
事实一。来源：[官方文档](https://a.example.com/spec)
事实二。来源：[第三方实证](https://b.example.com/study)
事实三。来源：[规范原文](https://c.example.com/rfc)

## 风险与局限
- 未查到 X 的定价。
"""


def test_report_checker():
    mod = _checker()
    errors, warnings = mod.check_report(GOOD_REPORT)
    assert errors == [], f"好报告不该报错：{errors}"

    cases = [
        ("缺结论节", GOOD_REPORT.replace("## 结论", "## 摘要"), "结论"),
        ("缺风险节", GOOD_REPORT.replace("## 风险与局限", "## 其它"), "风险"),
        ("没有来源链接", re.sub(r"\[.*?\]\(.*?\)", "某文章", GOOD_REPORT), "来源链接"),
        ("残留占位符", GOOD_REPORT.replace("建议采用方案 A。", "建议采用 <方案名>。TODO"), "占位符"),
        ("没标信息时点", GOOD_REPORT.replace("截至 2026-08", "近期"), "信息时点"),
    ]
    for label, text, expect in cases:
        errs, _ = mod.check_report(text)
        assert any(expect in e for e in errs), f"{label} 应被检出，实际：{errs}"

    # 单一信源重复凑数 -> 报错
    single = GOOD_REPORT
    for u in ("b.example.com/study", "c.example.com/rfc"):
        single = single.replace(u, "a.example.com/spec")
    errs, _ = mod.check_report(single)
    assert any("1 个信源" in e for e in errs), errs

    # 带编号的标题也要认（真跑实测：模型写「## 一、结论」「## 五、风险与局限汇总」，
    # 原来的 ^#{1,3}\s*结论 匹配不到 → 报假错、逼模型改标题迁就脚本，烧了 8 个工具调用）
    numbered = (GOOD_REPORT.replace("## 结论", "## 一、结论")
                           .replace("## 风险与局限", "## 五、风险与局限汇总"))
    errs, _ = mod.check_report(numbered)
    assert errs == [], f"带编号标题不该报错：{errs}"
    for variant in ("## 1. 结论", "### 第一章 结论", "## 结论与建议"):
        t = GOOD_REPORT.replace("## 结论", variant)
        assert mod.check_report(t)[0] == [], f"{variant} 不该报错"

    # 信息时点合理性（真跑实测：2026-08 做的调研，模型把时点写成「截至 2025-08」= 差 12 个月）
    for stale, gap in [("截至 2025-08", 12), ("截至 2023-01", 43)]:
        _, warns = mod.check_report(GOOD_REPORT.replace("截至 2026-08", stale), today=(2026, 8))
        assert any(f"距今约 {gap} 个月" in w for w in warns), (stale, warns)
    errs, _ = mod.check_report(GOOD_REPORT.replace("截至 2026-08", "截至 2027-05"),
                               today=(2026, 8))
    assert any("在未来" in e for e in errs), errs
    assert mod.check_report(GOOD_REPORT, today=(2026, 8))[0] == []   # 当期不报

    # 空报告 / 结论靠后 -> 分别是错误与警告
    assert mod.check_report("")[0] == ["报告是空的"]
    late = GOOD_REPORT.replace("## 结论\n建议采用方案 A。\n", "") + "\n" + "填充。\n" * 200 + "\n## 结论\n迟到的结论。\n"
    _, warns = mod.check_report(late)
    assert any("位置偏后" in w for w in warns), warns
    print("✓ 报告自检脚本：好报告放行 / 7 类问题全检出 / 结论靠后给警告")


def test_report_checker_cli(tmp: Path):
    import subprocess
    script = str(BUILTIN_SKILLS / "research-report" / "scripts" / "check_report.py")
    good, bad = tmp / "good.md", tmp / "bad.md"
    good.write_text(GOOD_REPORT, encoding="utf-8")
    bad.write_text("# 空壳\n没有任何东西。\n", encoding="utf-8")
    r_ok = subprocess.run([sys.executable, script, str(good)], capture_output=True,
                          text=True, encoding="utf-8")
    r_bad = subprocess.run([sys.executable, script, str(bad)], capture_output=True,
                           text=True, encoding="utf-8")
    r_usage = subprocess.run([sys.executable, script], capture_output=True,
                             text=True, encoding="utf-8")
    assert r_ok.returncode == 0 and "自检通过" in r_ok.stdout
    assert r_bad.returncode == 1 and "✗" in r_bad.stdout
    assert r_usage.returncode == 2
    print("✓ 报告自检脚本 CLI：退出码 0/1/2 分别对应 通过/有问题/用法错")


def main() -> int:
    import tempfile

    tests = [
        test_parse_minimal, test_parse_optional_fields, test_parse_rejects_invalid,
        test_parse_tolerates_bom_and_crlf, test_body_truncated,
        test_builtin_skills_valid, test_report_checker, test_name_normalized_for_third_party,
        test_long_description_truncated_for_third_party,
    ]
    tmp_tests = [
        test_discover_and_override, test_discover_isolates_bad_skill,
        test_progressive_disclosure_blocks, test_allowed_tools_not_auto_approved,
        test_load_skill_tool, test_skill_dirs_order, test_load_skill_reads_latest,
        test_conversation_integration, test_report_checker_cli,
    ]
    n = 0
    for t in tests:
        t()
        n += 1
    for t in tmp_tests:
        with tempfile.TemporaryDirectory() as td:
            t(Path(td))
        n += 1
    print(f"\ntest_skills: {n}/{n} 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
