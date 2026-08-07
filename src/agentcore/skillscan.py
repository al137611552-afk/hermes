"""技能安全扫描（FR-13.S2）：装第三方技能之前，把风险摊开给用户看。

对标社区注册表（agentskills.codes）的 clean / review / warn 三档分级 + 能力标记，
但**扫描在本地做、不依赖外部服务**——不把"这个技能安不安全"的判断权外包出去。

为什么需要：技能处在"agent 默认信任并执行其脚本"的特权位置，实证研究显示相当比例的
公开技能索要危险权限，且 SKILL.md 正文本身可作为 prompt injection 的载体。hermes 的
既有防线（危险操作照常过 gate）只挡执行，挡不住"用户在不知情下装了个坏技能"。

**这是启发式，不是保证**：只认已知的可疑模式，混淆得足够好就扫不出来。因此结论一律呈现为
"发现了这些信号，你自己判断"，而不是"此技能安全"。分级只决定确认的强度，不代替用户决定。

纯逻辑：输入文本、输出结论，不碰磁盘不联网，便于单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 分级（沿用社区命名，便于和外部注册表的标记对齐）
CLEAN, REVIEW, WARN = "clean", "review", "warn"
GRADE_LABEL = {CLEAN: "未发现可疑信号", REVIEW: "建议过目", WARN: "高风险"}

MAX_SCAN_CHARS = 400_000     # 单个技能包扫描的总字符上限（防超大包拖死）


@dataclass(frozen=True)
class Finding:
    """一条扫描发现。`kind` 是能力标记，`why` 说明为什么值得看一眼。"""
    kind: str          # 能力标记（网络外发 / 读凭据 / 破坏性命令 …）
    severity: str      # REVIEW 或 WARN
    where: str         # 相对路径
    excerpt: str       # 命中的片段（截断）
    why: str


@dataclass(frozen=True)
class ScanResult:
    grade: str
    findings: tuple[Finding, ...] = ()
    truncated: bool = False    # 内容超上限、只扫了前面一部分

    @property
    def flags(self) -> tuple[str, ...]:
        """去重后的能力标记，供 UI 做小标签展示。"""
        seen: list[str] = []
        for f in self.findings:
            if f.kind not in seen:
                seen.append(f.kind)
        return tuple(seen)


# 只在这些后缀的文件里，"提示注入"才按高危算——因为模型真正当指令读的是 SKILL.md 与
# references 里的 Markdown。脚本里出现同样的字符串通常是**数据**（真跑实测：社区的
# `ai-security` 技能，其威胁扫描脚本里有 `SEED_PROMPTS = ["Ignore all previous instructions…"]`，
# 正当的安全工具语料被判成高风险）。脚本里仍然报，但降为 review，不误伤这类技能。
_PROMPT_CONTEXT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
_DOWNGRADE_OUTSIDE_PROMPT = {"提示注入迹象"}

# 规则表：(能力标记, 正则, 默认严重度, 说明)。
# 定规则的取舍：**宁可多报一条 review，也不要漏掉真危险**——review 只是让用户看一眼，成本低；
# 漏报的代价是用户在不知情下装了坏东西。真正判 warn 的只有"几乎没有正当理由"的那几类。
_RULES: list[tuple[str, re.Pattern, str, str]] = [
    ("远程代码执行", re.compile(
        r"(curl|wget|iwr|Invoke-WebRequest)[^\n|]{0,200}\|\s*(ba)?sh\b"
        r"|(curl|wget)[^\n]{0,200}\|\s*(python|node|perl)\b"
        r"|Invoke-Expression|(?<![\w.])iex\s*\(", re.I),
     WARN, "把网上下载的内容直接喂给解释器执行——装了就等于让远端随时改你机器上跑的代码"),

    ("混淆执行", re.compile(
        r"base64\s+(-d|--decode|-D)[^\n]{0,80}\|\s*(ba)?sh"
        r"|eval\s*\(\s*(atob|base64|bytes\.fromhex|codecs\.decode)"
        r"|FromBase64String[^\n]{0,80}(Invoke|iex)", re.I),
     WARN, "先解码再执行——正当脚本几乎没有理由这么写，通常是为了躲过阅读和扫描"),

    ("破坏性命令", re.compile(
        r"rm\s+-[a-z]*[rf][a-z]*\s+[~/$]|rm\s+-rf\s+\*"
        r"|Remove-Item[^\n]{0,80}-Recurse[^\n]{0,40}-Force"
        r"|mkfs\.|dd\s+if=[^\n]{0,40}of=/dev/|format\s+[a-z]:\s*/", re.I),
     WARN, "递归删除或格式化——技能包里出现这类命令要非常确定它在删什么"),

    ("读取凭据", re.compile(
        r"\.env\b|id_rsa|\.ssh/|\.aws/credentials|\.netrc"
        r"|(API|SECRET|ACCESS|PRIVATE)_?(KEY|TOKEN|SECRET)\b"
        r"|keychain|Credential\s?Manager", re.I),
     REVIEW, "涉及密钥/凭据文件——确认它读这些是为了什么，尤其是同时还有联网行为时"),

    ("网络外发", re.compile(
        r"\b(curl|wget)\b|requests\.(post|put|patch)\s*\("
        r"|urllib\.request\.(urlopen|Request)|http\.client|fetch\s*\(\s*['\"]https?://"
        r"|Invoke-RestMethod|\bnc\s+-|socket\.(connect|create_connection)", re.I),
     REVIEW, "会访问网络——确认目标地址是它该去的地方，别把你的数据发去别处"),

    ("提权", re.compile(r"(?<![\w-])sudo\s|runas\s|Start-Process[^\n]{0,60}-Verb\s+RunAs", re.I),
     REVIEW, "要求管理员权限——正当技能很少需要"),

    ("修改 shell 配置", re.compile(
        r"\.bashrc|\.zshrc|\.bash_profile|\.profile\b|crontab|systemctl\s+enable"
        r"|schtasks|New-ScheduledTask|LaunchAgents", re.I),
     WARN, "改登录脚本或建计划任务＝装持久化后门的典型手法，技能包不该做这种事"),

    ("提示注入迹象", re.compile(
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)"
        r"|disregard\s+(your|the)\s+(instructions|system\s+prompt)"
        r"|忽略(之前|上面|以上|前面)的?(所有)?(指令|要求|提示)"
        r"|you\s+are\s+now\s+(in\s+)?(developer|god)\s+mode", re.I),
     WARN, "正文里试图覆盖你的系统指令——这是 prompt injection 的典型写法，技能没有正当理由这么写"),

    # HTML 注释阈值取 600 而非 200：真跑实测官方 plugin-dev 的文档里有 `<!-- COMMAND: … -->`
    # 这类几百字的**正当元数据注释**，200 的阈值把它们全报出来、噪音盖过信号。
    # 零宽字符没有正当用途，不设阈值照报。
    ("隐藏内容", re.compile(r"[​‌‍⁠﻿]|<!--[\s\S]{600,}?-->"),
     REVIEW, "含零宽字符或超长 HTML 注释——可能藏了给模型看、但你看不见的指示"),
]

# 声明索要这些工具时单独提示（对齐规范的 allowed-tools 字段；hermes 不据此免确认，
# 但"它想要什么权限"本身是用户该知道的信息）
_SENSITIVE_TOOLS = re.compile(r"bash|shell|run_|write|edit|execute|python|node", re.I)


def _excerpt(text: str, start: int, end: int, pad: int = 30) -> str:
    """取命中片段的上下文，压成单行、截断。"""
    s = text[max(0, start - pad):min(len(text), end + pad)]
    return re.sub(r"\s+", " ", s).strip()[:160]


def scan_files(files: dict[str, str]) -> ScanResult:
    """扫描技能包的文本文件（{相对路径: 内容}），返回分级与发现。纯函数。

    分级规则：命中任一 WARN 规则 → warn；只命中 REVIEW → review；都没有 → clean。
    """
    findings: list[Finding] = []
    truncated = False
    budget = MAX_SCAN_CHARS

    for path in sorted(files):
        text = files[path] or ""
        if budget <= 0:
            truncated = True
            break
        if len(text) > budget:
            text, truncated = text[:budget], True
        budget -= len(text)

        is_prompt_ctx = path.lower().endswith(_PROMPT_CONTEXT_SUFFIXES)
        for kind, pat, sev, why in _RULES:
            for m in pat.finditer(text):
                eff_sev, eff_why = sev, why
                if kind in _DOWNGRADE_OUTSIDE_PROMPT and not is_prompt_ctx:
                    eff_sev = REVIEW
                    eff_why = (why + "。**这条出现在脚本而非 Markdown 里**，模型不会把它当指令读，"
                               "常见于安全工具的测试语料——已降级，但仍请确认它确实是数据")
                findings.append(
                    Finding(kind, eff_sev, path, _excerpt(text, m.start(), m.end()), eff_why))
                break   # 同一文件同一规则只报一次，避免刷屏

    grade = WARN if any(f.severity == WARN for f in findings) else (
        REVIEW if findings else CLEAN)
    return ScanResult(grade, tuple(findings), truncated)


def scan_declared_tools(allowed_tools) -> tuple[Finding, ...]:
    """技能 frontmatter 里 allowed-tools 声明的敏感工具（只作信息展示）。"""
    hits = [t for t in (allowed_tools or []) if _SENSITIVE_TOOLS.search(str(t))]
    if not hits:
        return ()
    return (Finding(
        "声明敏感工具", REVIEW, "SKILL.md", " ".join(hits),
        "技能声明想用这些工具（hermes 不因此免确认，执行时照常需要你点确认）",
    ),)


def merge(*results) -> ScanResult:
    """合并多次扫描的结果（如文件扫描 + 声明扫描），取最严分级。"""
    findings: list[Finding] = []
    truncated = False
    for r in results:
        if isinstance(r, ScanResult):
            findings.extend(r.findings)
            truncated = truncated or r.truncated
        else:
            findings.extend(r)
    grade = WARN if any(f.severity == WARN for f in findings) else (
        REVIEW if findings else CLEAN)
    return ScanResult(grade, tuple(findings), truncated)


def summarize(result: ScanResult) -> str:
    """给用户看的一段中文小结（GUI 与 CLI 共用）。"""
    if result.grade == CLEAN:
        # 截断时必须说明"没扫完"——否则"未发现可疑模式"会被当成"扫遍了都干净"，是误导
        scope = "（技能包过大，只扫描了前面一部分，未覆盖全部内容）" if result.truncated else ""
        return ("未发现已知的可疑模式" + scope
                + "。注意：这是启发式扫描，不等于保证安全——仍建议扫一眼 SKILL.md 正文。")
    head = {WARN: "⛔ 发现高风险信号，装之前请务必看清楚：",
            REVIEW: "⚠ 发现以下值得过目的信号："}[result.grade]
    lines = [head]
    for f in result.findings:
        mark = "⛔" if f.severity == WARN else "•"
        lines.append(f"  {mark} [{f.kind}] {f.where}：{f.why}\n      命中：{f.excerpt}")
    if result.truncated:
        lines.append("  （技能包过大，只扫描了前面一部分）")
    lines.append("这是启发式扫描，扫不出刻意混淆的内容；最终是否安装由你判断。")
    return "\n".join(lines)
