"""错误分类（Error Taxonomy）——把失败的事实归到稳定的类别（见 docs/adr/0015）。

与 `Need` 正交、且更细：`Need` 答"世界缺什么"（差距），`ErrorClass` 答"这次失败
是哪一类"（根因族）。它是 Failure-Memory（块E）与 Learning（块G）的聚合 key——
"最近 1000 次里 40% 卡在 TRANSIENT_IO" 这种统计要靠它，也是块D 自动重试的判据
（只有 TRANSIENT_IO 才该无脑退避重试）。

输入是块B 的 `Evaluation`（signals/issues）+ 原始输出文本；输出是匹配到的类别
列表（按优先级排序，主类在前）。有失败但没规则命中 → `[UNKNOWN]` 兜底；没失败
→ `[]`。规则先行、UNKNOWN 收口，后续可换/可加模型分类，不改对外形态。
"""
from __future__ import annotations

import re
from enum import Enum


class ErrorClass(str, Enum):
    """失败的根因族。继承 str 便于直接进事件 JSON / 当 dict key。"""

    TRANSIENT_IO = "transient_io"          # 网络抖动/端口占用/超时——重试常能过
    AUTH = "auth"                          # 鉴权/授权失败：401/403/凭证/权限拒绝
    NOT_FOUND = "not_found"                # 路径/资源/模块/命令找不到
    SYNTAX = "syntax"                      # 编译/解析错：SyntaxError/parse error
    LOGIC = "logic"                        # 断言/测试逻辑失败（代码跑了但结果不对）
    RESOURCE = "resource"                  # OOM/磁盘满/配额/限流
    AMBIGUOUS = "ambiguous"                # 指令/匹配不唯一，需澄清
    EXTERNAL_BLOCKED = "external_blocked"  # 第三方硬阻塞：登录墙/封禁/验证码/503
    UNKNOWN = "unknown"                    # 有失败但未归类——兜底，绝不丢


# 规则按优先级排列：越靠前 = 越该作"主类"。TRANSIENT_IO 最前（最可行动：直接重试）；
# 根因类（NOT_FOUND/SYNTAX）排在表象类（LOGIC）前——import 缺失常是断言失败的真因。
_RULES: "list[tuple[ErrorClass, re.Pattern]]" = [
    (ErrorClass.TRANSIENT_IO, re.compile(
        r"超时|timed?\s?out|timeout|端口.{0,4}占用|address already in use|EADDRINUSE|"
        r"connection\s?(refused|reset|aborted)|ECONNREFUSED|ECONNRESET|temporarily unavailable|"
        r"EAGAIN|network is unreachable|读取超时|连接(?:超时|被拒|重置)", re.I)),
    (ErrorClass.AUTH, re.compile(
        r"\b401\b|\b403\b|unauthor|forbidden|authentication failed|鉴权失败|"
        r"permission denied|access denied|invalid (?:token|credentials|api[_ -]?key)|"
        r"凭证(?:无效|错误|过期)|无权限|未授权", re.I)),
    (ErrorClass.EXTERNAL_BLOCKED, re.compile(
        r"登录墙|请先?登[录入]|需要登[录入]|扫码登[录入]|captcha|验证码|人机验证|"
        r"\b503\b|service unavailable|rate[ -]?limit.{0,12}block|被(?:封|限制|拦截)|blocked by", re.I)),
    (ErrorClass.RESOURCE, re.compile(
        r"out of memory|OOM|MemoryError|no space left|disk (?:full|quota)|磁盘(?:满|空间不足)|"
        r"quota exceeded|配额|\brate limit\b|限流|too many requests|\b429\b|资源不足", re.I)),
    (ErrorClass.NOT_FOUND, re.compile(
        r"no such file|not found|\b404\b|未找到|找不到|不存在|无命中|无匹配文件|返回 0 条|"
        r"ModuleNotFoundError|ImportError|cannot find module|command not found|可执行程序|"
        r"缺失|未安装|需(?:安装|装)\b", re.I)),
    (ErrorClass.SYNTAX, re.compile(
        r"SyntaxError|IndentationError|parse error|unexpected token|编译(?:错误|失败|报错)|"
        r"compile error|cannot parse|invalid syntax|语法错误|unterminated", re.I)),
    (ErrorClass.LOGIC, re.compile(
        r"AssertionError|断言|测试(?:失败|未(?:全)?通过)|\bFAILED\b|expected .{0,20}but|"
        r"assertion failed|结果不(?:对|符)|预期.{0,8}实际", re.I)),
    (ErrorClass.AMBIGUOUS, re.compile(
        r"ambiguous|did you mean|多个(?:匹配|候选)|指令不(?:清|明确)|歧义|not unique|"
        r"multiple matches", re.I)),
]


def classify_text(text: str) -> "list[ErrorClass]":
    """对一段文本跑全部规则，返回命中的类别（按优先级去重排序，可能为空）。"""
    hay = text or ""
    out: "list[ErrorClass]" = []
    for cls, rx in _RULES:
        if rx.search(hay) and cls not in out:
            out.append(cls)
    return out


def classify(evaluation, output: str = "") -> "list[ErrorClass]":
    """对一次评估结果分类（主入口）。

    - 失败判定：`evaluation.issues` 非空即视为失败（块B 把 blocker 都放进 issues）。
    - 干草堆：issues + signals + 原始输出文本一起喂规则（信息越全越准）。
    - 有失败但无规则命中 → `[UNKNOWN]`（兜底，绝不把失败吞成"没事"）。
    - 没失败 → `[]`（正常路径，不污染 Failure-Memory）。
    """
    issues = list(getattr(evaluation, "issues", []) or [])
    signals = list(getattr(evaluation, "signals", []) or [])
    is_failure = bool(issues)
    if not is_failure:
        return []   # 没失败（块B 未判 blocker）→ 不分类、不污染 Failure-Memory
    hay = "\n".join([*issues, *signals, output or ""])
    matches = classify_text(hay)
    return matches if matches else [ErrorClass.UNKNOWN]   # 有失败必给类，UNKNOWN 兜底
