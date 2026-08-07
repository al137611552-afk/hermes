#!/usr/bin/env python3
"""调研报告成稿自检：检查结构完整性与信源支撑，不检查内容对错。

用法：python3 check_report.py <报告文件路径>
退出码：0=通过（可能有警告），1=有必须修的问题，2=用法错误/文件读不了。

检查项都是"机器能判、且判错代价低"的硬性要求——事实准确性靠交叉验证，不靠脚本。
纯逻辑（check_report）与 IO 分离，便于被 hermes 的测试直接调用。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 占位符/未完成标记：成稿里不该残留
PLACEHOLDER_RE = re.compile(r"(TODO|FIXME|待补|待填|XXX|<[^>\n]{1,40}>)")
LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
# 信息时点：形如「截至 2026-08」「截至 2026 年 8 月」
ASOF_RE = re.compile(r"截至\s*(\d{4})\s*[-年]\s*(\d{1,2})")
# 章节匹配容忍编号前缀（「## 一、结论」「## 1. 结论」「### 第五章 风险与局限」）和后缀
# （「## 风险与局限汇总」）——只要标题行里出现关键词即可。过死的匹配会逼作者迁就脚本改标题。
REQUIRED_SECTIONS = [
    ("结论", re.compile(r"^#{1,3}[^\n]*结论", re.M),
     "开篇要有结论节，直接给答案和建议"),
    ("风险与局限", re.compile(r"^#{1,3}[^\n]*(风险|局限|不足)", re.M),
     "要有风险/局限节，包括没查到的部分；没风险也要写明'未发现'"),
]
# 信息时点早于这个月数就提醒复核。取 6 个月是因为真跑实测：2026-08 做的调研，模型把时点写成
# 「截至 2025-08」（训练语料年份），差 12 个月——阈值放宽就抓不住这类最常见的时点写错。
# 只是警告：确实只有一年前数据的选题属正常，作者判断后可忽略。
STALE_MONTHS = 6


def check_report(text: str, today: "tuple[int, int] | None" = None) -> tuple[list[str], list[str]]:
    """检查报告文本，返回 (必须修的问题, 警告)。纯函数（today=(年,月)，省略则取系统当月）。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not text.strip():
        return ["报告是空的"], []

    for label, pat, hint in REQUIRED_SECTIONS:
        if not pat.search(text):
            errors.append(f"缺少「{label}」一节——{hint}（标题带编号如「## 一、{label}」也认）")

    links = LINK_RE.findall(text)
    if not links:
        errors.append("全文没有任何来源链接——每个结论段末尾都要附 [标题](URL)")
    else:
        unique = set(links)
        if len(unique) == 1 and len(links) > 2:
            errors.append(f"全文只引用了 1 个信源却重复 {len(links)} 次——需要独立信源交叉验证")
        elif len(unique) < 3:
            warnings.append(f"只有 {len(unique)} 个独立信源，交叉验证偏薄（建议 ≥3，且含 1 个非官方信源）")

    placeholders = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(text)})
    if placeholders:
        shown = "、".join(placeholders[:5])
        more = f" 等 {len(placeholders)} 处" if len(placeholders) > 5 else ""
        errors.append(f"残留占位符/未完成标记：{shown}{more}——模板尖括号占位必须替换掉")

    asof = ASOF_RE.search(text)
    if not asof:
        errors.append("没有标注信息时点（如「截至 2026-08」）——技术类结论必须带时效")
    else:
        # 时点合理性：模型很容易把"截至"写成训练语料里的年份（真跑实测：2026 年写成 2025-08）
        import datetime
        now = today or (datetime.date.today().year, datetime.date.today().month)
        y, mo = int(asof.group(1)), int(asof.group(2))
        months_ago = (now[0] - y) * 12 + (now[1] - mo)
        if months_ago < 0:
            errors.append(f"信息时点 {y}-{mo:02d} 在未来——写成了实际调研日期之后，请核对")
        elif months_ago > STALE_MONTHS:
            warnings.append(
                f"信息时点 {y}-{mo:02d} 距今约 {months_ago} 个月——确认这不是把当前年份写错了"
                "（技术类结论过期快，写错时点会误导读者）"
            )

    # 结论先行：正文里「结论」应出现在前 1/3
    m = REQUIRED_SECTIONS[0][1].search(text)
    if m and m.start() > len(text) // 3:
        warnings.append("「结论」一节位置偏后——结论应先行，读者不该翻到后半段才看到答案")

    return errors, warnings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python3 check_report.py <报告文件路径>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"读不了文件 {path}：{e}", file=sys.stderr)
        return 2

    errors, warnings = check_report(text)
    for w in warnings:
        print(f"⚠ {w}")
    for e in errors:
        print(f"✗ {e}")
    if errors:
        print(f"\n自检未通过：{len(errors)} 个问题必须修（另有 {len(warnings)} 条警告）。改完重跑。")
        return 1
    print(f"✓ 自检通过（{len(warnings)} 条警告，建议但不强制）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
