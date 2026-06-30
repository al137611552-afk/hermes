"""块H3a：模型裁判（judge.py）+ detect_offtarget_research 自检。

用"假裁判"（注入的 judge_fn）替真模型，纯逻辑、无网络、无模型。
`python tests/test_research_judge.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.agent.judge import (  # noqa: E402
    Verdict, build_judge_prompt, parse_verdict, judge_research,
)
from agentcore.agent.loop import (  # noqa: E402
    detect_offtarget_research, detect_ungrounded_answer, _latest_user_text,
)
from agentcore.providers.base import Message  # noqa: E402


class _Call:
    def __init__(self, i, name, params):
        self.id, self.name, self.input = str(i), name, params


# ---- parse_verdict ----
def test_parse_offtarget_json():
    v = parse_verdict('{"on_target": false, "off": ["真丝厚睡衣：秋冬款不符夏季"], "suggestion": "加\'冰丝 短袖\'重搜"}')
    assert v.on_target is False
    assert "秋冬款" in v.off[0]
    assert "重搜" in v.suggestion


def test_parse_ontarget_json():
    v = parse_verdict('结果如下 {"on_target": true, "off": []} 完毕')  # 容忍前后多余文字
    assert v.on_target is True and v.off == []


def test_parse_garbage_defaults_ontarget():
    # 裁判输出无法解析 → 放行不拦（裁判出错不误触发重搜）
    assert parse_verdict("模型抽风了没给JSON").on_target is True
    assert parse_verdict("").on_target is True


# ---- judge_research ----
def test_judge_calls_fn_and_parses():
    captured = {}
    def fake(prompt, images):
        captured["prompt"] = prompt
        captured["images"] = images
        return '{"on_target": false, "off": ["A：冬季款"], "suggestion": "换夏季关键词"}'
    v = judge_research("618夏季女士睡衣", "1. 真丝厚睡衣 ¥299", fake)
    assert v.on_target is False and "冬季款" in v.off[0]
    assert "夏季女士睡衣" in captured["prompt"]      # goal 进了 prompt


def test_judge_no_goal_passes():
    v = judge_research("", "一些结果", lambda p, i: '{"on_target": false}')
    assert v.on_target is True                      # 无目标无可判 → 放行


def test_judge_fn_failure_passes():
    def boom(prompt, images):
        raise RuntimeError("模型超时")
    v = judge_research("目标", "结果", boom)
    assert v.on_target is True                       # 裁判故障 → 不拦


def test_judge_multimodal_passes_images():
    seen = {}
    def fake(prompt, images):
        seen["images"] = images
        return '{"on_target": true}'
    judge_research("夏季睡衣", "", fake, images=["<img1>"])
    assert seen["images"] == ["<img1>"]
    assert "配图" in build_judge_prompt("g", "r", has_images=True)


# ---- H3c：三态裁判（salvage / use 萃取）----
def test_parse_use_salvage():
    v = parse_verdict('{"on_target": false, "use": ["冰丝短袖睡衣 ¥199"], "off": ["真丝厚款：秋冬"]}')
    assert v.on_target is False and v.use == ["冰丝短袖睡衣 ¥199"]
    assert v.salvageable is True              # 整体不对题但有可用少数 → 该萃取


def test_parse_no_use_not_salvageable():
    v = parse_verdict('{"on_target": false, "use": [], "off": ["全是冬季款"]}')
    assert v.salvageable is False             # 一条都不相关 → 不可萃取（才重搜）


def test_ontarget_not_salvageable():
    assert parse_verdict('{"on_target": true}').salvageable is False  # 对题=直接用，不走萃取路


# ---- detect_offtarget_research（loop 钩子）----
_OFF = '{"on_target": false, "off": ["厚款真丝睡衣：秋冬，不符夏季"], "suggestion": "加\'冰丝/短袖\'换平台重搜"}'
_SALVAGE = '{"on_target": false, "use": ["冰丝短袖睡衣 ¥199"], "off": ["真丝厚款：秋冬不符夏季"]}'
_ON = '{"on_target": true, "off": []}'


def test_hook_salvage_extracts_not_discard():
    # 部分污染 → 提示"挑出有效的用"，**不**说"请不要采用这些结果"、**不**强推重搜
    calls = [_Call(1, "web_search", {"query": "618夏季女士睡衣"})]
    out = {"1": "1. 真丝厚款\n2. 冰丝短袖睡衣 ¥199"}
    msg = detect_offtarget_research(calls, out, "618夏季女士睡衣", lambda p, i: _SALVAGE, {}, 1)
    assert msg is not None and "部分有效" in msg and "冰丝短袖" in msg
    assert "请不要采用这些结果" not in msg     # 杀掉旧的整批丢弃措辞
    assert "整批丢" in msg and "凭训练记忆" in msg


def test_hook_basically_junk_research_not_from_memory():
    # 一条都不相关（use 空）→ 才重搜，且明确禁止凭记忆顶替
    calls = [_Call(1, "web_search", {"query": "618夏季女士睡衣"})]
    msg = detect_offtarget_research(calls, {"1": "1. 厚款"}, "618夏季女士睡衣",
                                    lambda p, i: _OFF, {}, 1)
    assert msg is not None and "基本不对题" in msg and "别凭训练记忆" in msg


# ---- H3c：接地/时效闸（detect_ungrounded_answer，纯正则）----
def test_grounded_with_citation_passes():
    # 有引用来源 → 接地，不打扰
    assert detect_ungrounded_answer("2026最新手机价格", "据搜索，X 售价 ¥3999 http://jd.com/x", True) is None


def test_grounded_with_disclaimer_passes():
    # 已声明可能过时 → 诚实，不打扰
    assert detect_ungrounded_answer(
        "今年618优惠", "以下基于训练知识、可能已过时，建议以实时为准：……", True) is None


def test_ungrounded_freshness_nudges():
    # 时效敏感 + 做过搜索 + 既无引用又无声明 → 催据搜到内容作答/声明过时
    msg = detect_ungrounded_answer("帮我查2026最新显卡价格", "大概在三千到五千元。", True)
    assert msg is not None and "实时数据" in msg and "标注来源" in msg


def test_not_freshness_sensitive_passes():
    # 稳定知识（无时效信号）→ 凭常识答没问题，不误杀
    assert detect_ungrounded_answer("光合作用的原理是什么", "植物把光能转化为化学能。", True) is None


def test_no_research_no_guard():
    # 本轮没搜索（无可用搜索内容）→ 不触发
    assert detect_ungrounded_answer("2026最新价格", "大概三千。", False) is None


def test_hook_offtarget_nudges():
    calls = [_Call(1, "web_search", {"query": "618夏季女士睡衣"})]
    out = {"1": "1. 真丝厚睡衣套装\n   http://a\n   ¥299"}
    msg = detect_offtarget_research(calls, out, "搜618夏季女士睡衣并附图",
                                    lambda p, i: _OFF, {}, 1)
    assert msg is not None and "不对题" in msg and "秋冬" in msg


def test_hook_ontarget_silent():
    calls = [_Call(1, "web_search", {"query": "夏季睡衣"})]
    msg = detect_offtarget_research(calls, {"1": "1. 冰丝短袖睡衣"}, "夏季睡衣",
                                    lambda p, i: _ON, {}, 1)
    assert msg is None


def test_hook_capped_per_query():
    calls = [_Call(1, "web_search", {"query": "夏季睡衣"})]
    out = {"1": "1. 厚睡衣"}
    state = {}
    a = detect_offtarget_research(calls, out, "夏季睡衣", lambda p, i: _OFF, state, 1)
    b = detect_offtarget_research(calls, out, "夏季睡衣", lambda p, i: _OFF, state, 1)
    assert a is not None and b is None


def test_hook_respects_h2_already_nudged():
    # H2 已就该 query 提示过（state 计数已满）→ H3 跳过，不重复催
    calls = [_Call(1, "web_search", {"query": "睡衣 500元以内"})]
    state = {"睡衣 500元以内": 1}
    msg = detect_offtarget_research(calls, {"1": "1. 厚睡衣"}, "睡衣 500元以内",
                                    lambda p, i: _OFF, state, 1)
    assert msg is None


# ---- _latest_user_text ----
def test_latest_user_text_from_str_and_blocks():
    msgs = [Message("user", "搜618夏季女士睡衣"),
            Message("assistant", "好的"),
            Message("user", [{"type": "text", "text": "要附图"},
                             {"type": "text", "text": "[用户追加] 急", }])]
    # 取最后一条 user；跳过 [用户追加] 前缀
    assert _latest_user_text(msgs) == "要附图"


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    fns.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
