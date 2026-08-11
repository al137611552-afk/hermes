#!/usr/bin/env python3
"""技能包成稿自检：检查「能不能用」，不检查内容对错。

用法：
    python3 check_skill.py <技能目录>
    python3 check_skill.py --self-test        # 规则自检（改规则后跑一次）
退出码：0=通过（可能有警告），1=有必须修的问题，2=用法错误/目录读不了。

检查项都是"机器能判、判错代价低"的硬性要求：frontmatter 合规、name 与目录名一致、
description 带触发词、命令表没留占位符、引用的文件真实存在、没把疑似密钥写进去。
技能内容对不对靠真跑，不靠脚本。

纯逻辑（check_skill）与 IO 分离，便于被 hermes 的测试直接调用。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_DESC = 1024
MIN_DESC = 30
# 占位符：成稿里不该残留。分两档——
# ① 明写的未完成标记，出现在哪都算（连代码块里也算，`<TODO 填命令>` 就是典型）；
# ② 尖括号占位（`<入口>`）**只在代码块外算**——真跑发现命令用法里写 `<入口> --help`
#    是正当写法，一刀切会把合格技能全判挂（研发时误伤了本技能与 research-report）。
PLACEHOLDER_RE = re.compile(r"(TODO|FIXME|待填|待补|XXX|根据实际情况|请替换)")
ANGLE_PLACEHOLDER_RE = re.compile(r"<[^>\n]{1,30}>")
CODE_SPAN_RE = re.compile(r"`[^`]*`")
# 疑似密钥前缀（凭据真长这样）。
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,})")
# 长串 base64 只在文档类文件里算——脚本里长串常是正则/测试语料/编码表，
# 判死会误伤（同 skillscan 的教训：脚本里的可疑串多半是数据不是凭据）。
LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
DOC_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".env", ".ini", ".cfg")
# 触发词：description 里要能看出"用户说什么时候用它"
TRIGGER_HINTS = ("当用户", "当需要", "用于", "适用于", "想要", "需要")
MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")


def _split_frontmatter(text: str) -> "tuple[str, str] | None":
    s = text.lstrip("﻿")
    if not s.startswith("---"):
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", s, re.S)
    return (m.group(1), s[m.end():]) if m else None


def check_skill(skill_dir: Path, read=None) -> "tuple[list[str], list[str]]":
    """检查一个技能目录。read 可注入（便于单测），默认读磁盘。返回 (errors, warnings)。"""
    read = read or (lambda p: p.read_text(encoding="utf-8", errors="replace"))
    errors: list[str] = []
    warnings: list[str] = []

    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return [f"{skill_dir} 里没有 SKILL.md（技能包必须有它）"], []

    text = read(md)
    parts = _split_frontmatter(text)
    if parts is None:
        return ["SKILL.md 缺少 YAML frontmatter（文件须以 --- 起始的元数据块开头）"], []
    fm, body = parts

    def field(name: str) -> str:
        m = re.search(rf"^{name}\s*:\s*(.+)$", fm, re.M)
        return m.group(1).strip().strip('"').strip("'") if m else ""

    name = field("name")
    if not name:
        errors.append("frontmatter 缺 name")
    elif name != skill_dir.name:
        errors.append(f"name（{name}）必须与目录名（{skill_dir.name}）一致")
    elif not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", name):
        errors.append(f"name（{name}）要全小写连字符，如 futures-monitor")

    desc = field("description")
    if not desc:
        errors.append("frontmatter 缺 description——技能能不能被用上全看它")
    else:
        if len(desc) > MAX_DESC:
            errors.append(f"description 超过 {MAX_DESC} 字符")
        if len(desc) < MIN_DESC:
            warnings.append("description 太短，写清「做什么 + 用户说什么时候用它」")
        if not any(h in desc for h in TRIGGER_HINTS):
            warnings.append("description 里看不出触发场景，建议写明「当用户问…时使用」")

    if not body.strip():
        errors.append("SKILL.md 正文是空的")

    # 占位符：命令表里留 <TODO> 等于把活推回给用户。
    # 围栏代码块与行内代码里的尖括号是用法示意（`<入口> --help`），只查明写的未完成标记。
    in_fence = False
    for i, line in enumerate(body.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        stripped = line.lstrip()
        is_table = stripped.startswith("|")
        # 未完成标记只在"真会坑人的位置"算：命令表行 / 代码块内 / 行首。
        # 散文里**提到** TODO（"不留 `<TODO>`""检查是否残留 TODO"）是正当写法——
        # 同 skillscan 的教训：把"提到"当成"存在"会误伤正经文档。
        risky = in_fence or is_table or PLACEHOLDER_RE.match(stripped)
        m = PLACEHOLDER_RE.search(line) if risky else None
        if not m and not in_fence:
            # 命令表要求每行可直接复制执行 → 表格行里的 <占位> 就是问题；
            # 散文里的 `<入口>` 是用法示意，先剥掉行内代码再看。
            m = ANGLE_PLACEHOLDER_RE.search(line if is_table else CODE_SPAN_RE.sub("", line))
        if m:
            errors.append(f"正文第 {i} 行残留占位符 {m.group(0)!r}：{line.strip()[:60]}")
            break

    # 引用的相对路径文件要真实存在。只查"看起来像路径"的（含 / 或后缀）——
    # 模板里写 [示例](URL) 这种占位说明不是坏链接（research-report 里就有）。
    for rel in MD_LINK_RE.findall(body):
        if rel.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if "/" not in rel and "." not in rel:
            continue
        if not (skill_dir / rel).exists():
            errors.append(f"正文引用了不存在的文件：{rel}")

    # 疑似密钥（整个技能包扫一遍）
    try:
        files = [p for p in skill_dir.rglob("*") if p.is_file()]
    except OSError:
        files = [md]
    for p in files:
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".zip", ".pdf"):
            continue
        try:
            content = read(p)
        except Exception:  # noqa: BLE001
            continue
        m = SECRET_RE.search(content)
        if m:
            hit = m.group(0)[:12]
            if p.suffix.lower() in DOC_SUFFIXES:
                errors.append(f"{p.name} 里有疑似密钥（{hit}…）——凭据只放 .env，绝不进技能包")
            else:
                # 脚本里可能是测试语料/正则（本脚本自己就有），报警告让人自己看一眼
                warnings.append(f"{p.name} 里有像密钥的串（{hit}…），确认不是真凭据")
            break
        if p.suffix.lower() in DOC_SUFFIXES and LONG_B64_RE.search(content):
            warnings.append(f"{p.name} 里有超长随机串，确认不是编码后的凭据")
            break

    if len(body.splitlines()) > 500:
        warnings.append("正文超过 500 行，规范建议主文件精简、细节挪进 references/")
    return errors, warnings


def _self_test() -> int:
    """规则自检：好样本过、各类坏样本必须被抓到。用真临时目录跑真路径，不搭假对象。"""
    import tempfile

    good_md = (
        "---\n"
        "name: demo-tool\n"
        'description: 查询演示数据。当用户问"看看演示数据 / 跑一下 demo"时使用。\n'
        "---\n"
        "# Demo\n\n## 命令表\n| 问什么 | 跑什么 |\n|---|---|\n| 看数据 | `demo list --json` |\n"
    )
    cases = [
        ("好样本应通过", "demo-tool", {"SKILL.md": good_md}, 0),
        ("缺 SKILL.md 应报错", "demo-tool", {"README.md": "空的"}, 1),
        ("缺 frontmatter 应报错", "demo-tool", {"SKILL.md": "# 只有正文"}, 1),
        ("name 与目录名不符应报错", "other-name", {"SKILL.md": good_md}, 1),
        ("name 不合规范应报错", "Demo_Tool",
         {"SKILL.md": good_md.replace("name: demo-tool", "name: Demo_Tool")}, 1),
        ("残留占位符应报错", "demo-tool",
         {"SKILL.md": good_md.replace("`demo list --json`", "`<TODO 填命令>`")}, 1),
        ("疑似密钥应报错", "demo-tool",
         {"SKILL.md": good_md, "references/OUTPUT.md": "key: sk-abcdefghijklmnopqrstuvwx"}, 1),
        ("引用不存在的文件应报错", "demo-tool",
         {"SKILL.md": good_md + "\n见 [字段表](references/OUTPUT.md)\n"}, 1),
        ("代码块里的 <入口> 是用法示意，不该报错", "demo-tool",
         {"SKILL.md": good_md + "\n```\n<入口> --help\n```\n用 `<入口> list` 查。\n"}, 0),
        ("代码块里的 TODO 仍要报错", "demo-tool",
         {"SKILL.md": good_md + "\n```\nTODO: 填命令\n```\n"}, 1),
        ("命令表行里的 TODO 要报错", "demo-tool",
         {"SKILL.md": good_md + "\n| 看别的 | TODO 待补 |\n"}, 1),
        ("散文里提到 TODO 不算残留", "demo-tool",
         {"SKILL.md": good_md + "\n命令表每行都要能直接跑，不留 `<TODO>` 或 TODO 标记。\n"}, 0),
        ("模板里的 [示例](URL) 不算坏链接", "demo-tool",
         {"SKILL.md": good_md + "\n参考 [示例](URL)\n"}, 0),
        ("引用存在的文件应通过", "demo-tool",
         {"SKILL.md": good_md + "\n见 [字段表](references/OUTPUT.md)\n",
          "references/OUTPUT.md": "字段说明"}, 0),
    ]
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        for i, (label, dirname, files, want) in enumerate(cases):
            d = Path(td) / str(i) / dirname
            for rel, content in files.items():
                f = d / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content, encoding="utf-8")
            errors, _ = check_skill(d)
            got = 1 if errors else 0
            if got != want:
                bad += 1
                print(f"✗ 自检用例不符预期：{label}（errors={errors}）")
            else:
                print(f"✓ {label}")
    # description 太短只该是警告，不该拦人
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "demo-tool"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: demo-tool\ndescription: 查数据\n---\n正文\n", encoding="utf-8")
        errors, warnings = check_skill(d)
        if errors or not warnings:
            bad += 1
            print(f"✗ 自检用例不符预期：description 太短应只给警告（errors={errors}）")
        else:
            print("✓ description 太短只给警告，不拦人")
    print("\n自检" + ("通过" if not bad else f"未通过：{bad} 条规则失效"))
    return 0 if not bad else 1


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--self-test":
        return _self_test()
    if len(argv) != 2:
        print("用法：python3 check_skill.py <技能目录> | --self-test", file=sys.stderr)
        return 2
    d = Path(argv[1])
    if not d.is_dir():
        print(f"不是目录：{d}", file=sys.stderr)
        return 2

    errors, warnings = check_skill(d)
    for w in warnings:
        print(f"⚠ {w}")
    for e in errors:
        print(f"✗ {e}")
    if errors:
        print(f"\n技能自检未通过：{len(errors)} 个问题必须修（另有 {len(warnings)} 条警告）。改完重跑。")
        return 1
    print(f"✓ 技能自检通过（{len(warnings)} 条警告，建议但不强制）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
