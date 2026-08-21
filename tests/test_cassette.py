"""块 V3 录制/回放的离线自检（ADR 0027 决策 4）。

不调模型、不联网、不需要 key——用假 provider 压出录制与回放的全部契约。

运行：python tests/test_cassette.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.providers.base import Message, StreamEvent, ToolCall  # noqa: E402
from agentcore.providers.cassette import (  # noqa: E402
    CassetteMiss, CassetteStore, event_from_dict, event_to_dict, fold_workspace,
    make_replay, normalize_noise, request_key, wrap_recording,
)
from agentcore.store.usage import provider_kind  # noqa: E402


class AnthropicProvider:
    """假 provider，类名刻意与真实实现同名——用来验厂商归属不被包装改掉。"""

    model = "fake-model-1"

    def __init__(self, script=None):
        self.script = script or [StreamEvent("text", "好"),
                                 StreamEvent("done", meta={"stop_reason": "end_turn"})]
        self.calls = 0

    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        self.calls += 1
        yield from self.script


def _msgs(text="hi"):
    return [Message("user", text)]


# ---- 请求指纹 ---------------------------------------------------------------

def test_key_is_stable_and_field_sensitive():
    k = request_key("m", "sys", _msgs(), [{"name": "t"}])
    assert k == request_key("m", "sys", _msgs(), [{"name": "t"}])
    assert k != request_key("m2", "sys", _msgs(), [{"name": "t"}])
    assert k != request_key("m", "sys2", _msgs(), [{"name": "t"}])
    assert k != request_key("m", "sys", _msgs("bye"), [{"name": "t"}])
    assert k != request_key("m", "sys", _msgs(), [{"name": "t2"}])


def test_key_ignores_max_tokens_by_design():
    """key 不含 max_tokens/temperature（ADR 决策 4 口径）——它们已进 Run Record 的配置快照，
    放进 key 只会让录音更易失效。这条写成测试，免得将来"顺手"加进去。"""
    import inspect
    src = inspect.getsource(request_key)
    assert "max_tokens" not in src.split('"""')[2], "max_tokens 不该进指纹"


def test_image_blocks_hashed_not_inlined():
    """图片用内容哈希代替 base64：否则同一张图不同编码会分裂成两个 key，
    且原样存进 cassette 会让文件大到不可读。"""
    def img(data):
        return [{"type": "image", "source": {"media_type": "image/png", "data": data}}]
    a = request_key("m", None, [Message("user", img("AAAA"))], None)
    b = request_key("m", None, [Message("user", img("AAAA"))], None)
    c = request_key("m", None, [Message("user", img("BBBB"))], None)
    assert a == b and a != c


def test_workspace_path_is_folded_out_of_the_key():
    """**工具输出会回灌进消息历史**（pytest 的 `rootdir: /tmp/tmpXXXX/ws`），
    临时工作区每跑都不同 → 不折就全 miss。与块 V0 的死路指纹同病同药。"""
    m1 = [Message("user", "看 /tmp/run1/ws/calc.py 报错")]
    m2 = [Message("user", "看 /tmp/run2/ws/calc.py 报错")]
    assert request_key("m", None, m1, None) != request_key("m", None, m2, None)
    os.environ["HERMES_CASSETTE_WS"] = "/tmp/run1/ws"
    try:
        k1 = request_key("m", None, m1, None)
    finally:
        os.environ["HERMES_CASSETTE_WS"] = "/tmp/run2/ws"
    try:
        k2 = request_key("m", None, m2, None)
    finally:
        os.environ.pop("HERMES_CASSETTE_WS", None)
    assert k1 == k2, "折了工作区路径后，两跑应得同一个 key"


def test_fold_workspace_folds_both_separator_forms():
    """**同一个工作区在消息历史里会以两种分隔符形态出现**：Windows 上 `str(Path(...))`
    一律给反斜杠，而 bash/git/一些工具的输出、以及模型自己复述路径时常写正斜杠。
    只折一种＝换个形态就漏折，指纹又变回"跟这台机器有关"（2026-08-21 Windows CI 暴露）。
    """
    assert fold_workspace("看 D:/x/ws/calc.py", "D:\\x\\ws") == "看 <ws>/calc.py"
    assert fold_workspace("看 D:\\x\\ws\\calc.py", "D:\\x\\ws") == "看 <ws>\\calc.py"


def test_fold_workspace_is_a_pure_function():
    assert fold_workspace("a /w/p/x.py b", "/w/p") == "a <ws>/x.py b"
    assert fold_workspace("无路径", "/w/p") == "无路径"
    assert fold_workspace("原样", "") == "原样"


# ---- 噪声归一化：边界必须写死 -----------------------------------------------

def test_heap_addresses_are_normalized():
    """pytest 的断言自省会打出 `<function f at 0x7c258c2e0360>`，工具输出又会回灌进
    消息历史 → cassette 指纹每跑都变。这是块 V3 那个"每三四轮红一次"的确切根因。"""
    a = normalize_noise("<function moving_average at 0x7c258c2e0360>([1,2],2)")
    b = normalize_noise("<function moving_average at 0x75c9686d8360>([1,2],2)")
    assert a == b == "<function moving_average at 0xADDR>([1,2],2)"


def test_test_durations_are_normalized():
    """pytest 摘要行的耗时（`1 error in 0.09s`）度量的是本机调度快慢、不是被测代码行为。
    录制时机器忙（正在等模型），回放时空闲——于是**稳定地**差那么几毫秒。"""
    assert normalize_noise("1 error in 0.09s") == normalize_noise("1 error in 0.08s")
    assert normalize_noise("3 passed in 0.42s") == "3 passed in Ns"


def test_duration_pattern_stays_narrow():
    """**模式必须窄**：光写 `\\d+\\.\\d+s` 会误伤正文里有意义的数字。
    只认 `in <小数>s` 这一个搭配。"""
    assert normalize_noise("超时设成 1.5s") == "超时设成 1.5s"
    assert normalize_noise("耗时 0.42s") == "耗时 0.42s"
    assert normalize_noise("in 3s") == "in 3s", "整数秒不认（那多半是配置值）"


def test_timestamps_and_shas_are_NOT_normalized():
    """**边界写死，别扩。** 只抹「机器生成的、标识临时运行态的、零语义」记号。

    时间戳不抹——"某时刻的日志"可能是有意义的内容；git SHA 不抹——它标识内容本身。
    这两类任务改走 `replayable=False`，而不是把它们也归一化掉：
    那就滑向 ADR 决策 4 禁止的"更聪明的模糊匹配"了。
    """
    ts = "2026-08-19T07_14_25_080Z-debug-0.log"
    sha = "commit d24226f849a779665aa17e110439a26aca7646bc"
    assert normalize_noise(ts) == ts
    assert normalize_noise(sha) == sha


def test_addresses_make_two_requests_equal_in_the_key():
    """归一化要真的作用在指纹上，不能只是个没人调的纯函数。"""
    m1 = [Message("user", "<function f at 0xaaaaaaaaaaaa>")]
    m2 = [Message("user", "<function f at 0xbbbbbbbbbbbb>")]
    assert request_key("m", None, m1, None) == request_key("m", None, m2, None)


# ---- 事件序列化 -------------------------------------------------------------

def test_tool_call_round_trips():
    ev = StreamEvent("tool_use", meta={"call": ToolCall("c1", "run_bash", {"command": "ls"})})
    back = event_from_dict(event_to_dict(ev))
    assert back.type == "tool_use"
    assert isinstance(back.meta["call"], ToolCall)
    assert back.meta["call"].name == "run_bash" and back.meta["call"].input == {"command": "ls"}


def test_done_meta_round_trips():
    ev = StreamEvent("done", meta={"stop_reason": "tool_use",
                                   "usage": {"input": 10, "output": 3}})
    back = event_from_dict(event_to_dict(ev))
    assert back.meta["stop_reason"] == "tool_use" and back.meta["usage"]["input"] == 10


# ---- 录制 -------------------------------------------------------------------

def test_recording_writes_even_when_consumer_breaks_early():
    """**`AgentLoop.run` 收到 done 就 break**，生成器被丢弃——第一版把写盘放在 for 之后，
    结果一条都没录上。必须用 finally。"""
    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        inner = AnthropicProvider([StreamEvent("text", "a"),
                                   StreamEvent("done", meta={"stop_reason": "end_turn"}),
                                   StreamEvent("text", "尾巴")])
        for ev in wrap_recording(inner, store).stream_chat(_msgs()):
            if ev.type == "done":
                break
        assert store.count() == 1, "提前 break 后没录上"


def test_partial_stream_is_not_recorded():
    """流中途炸了不写——半截录音比没有更坏：回放时它会假装那轮正常结束，
    把"当时其实炸了"这个事实抹掉。"""
    class Boom(AnthropicProvider):
        def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
            yield StreamEvent("text", "开头")
            raise RuntimeError("断了")

    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        try:
            list(wrap_recording(Boom(), store).stream_chat(_msgs()))
        except RuntimeError:
            pass
        assert store.count() == 0, "半截流不该留下录音"


def test_recording_preserves_provider_kind():
    """`store/usage.py:provider_kind()` 从**类名**推厂商。包一层若改了类名，
    用量台账的归属会变成 'recording'——录音模式下的记录就不如实了。"""
    with tempfile.TemporaryDirectory() as d:
        w = wrap_recording(AnthropicProvider(), CassetteStore(d))
        assert provider_kind(w) == "anthropic"
        assert w.model == "fake-model-1", "属性要透传到内层"


def test_cassette_file_is_readable_jsonl():
    """人要能直接打开看、能 diff——首行 meta，其后一行一个事件。"""
    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        list(wrap_recording(AnthropicProvider(), store).stream_chat(_msgs()))
        f = next(Path(d).glob("*.jsonl"))
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        head = json.loads(lines[0])
        assert head["_meta"] is True and head["model"] == "fake-model-1"
        assert head["provider_class"] == "AnthropicProvider"
        assert json.loads(lines[1])["type"] == "text"


# ---- 回放 -------------------------------------------------------------------

def test_replay_reproduces_the_recorded_stream():
    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        script = [StreamEvent("text", "答"),
                  StreamEvent("tool_use", meta={"call": ToolCall("c1", "read_file", {"path": "a"})}),
                  StreamEvent("done", meta={"stop_reason": "tool_use"})]
        list(wrap_recording(AnthropicProvider(script), store).stream_chat(_msgs()))
        out = list(make_replay("fake-model-1", store).stream_chat(_msgs()))
        assert [e.type for e in out] == ["text", "tool_use", "done"]
        assert out[1].meta["call"].name == "read_file"


def test_replay_needs_no_provider_and_no_key():
    """回放**不构造真 provider**——CI 里没有 key 也能跑。这是"评测进 CI"的全部前提。"""
    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        list(wrap_recording(AnthropicProvider(), store).stream_chat(_msgs()))
        r = make_replay("fake-model-1", store)
        assert not hasattr(r, "api_key") or getattr(r, "api_key", None) is None
        assert list(r.stream_chat(_msgs()))[-1].type == "done"


def test_miss_raises_and_names_the_step():
    """**miss 必须硬报错**、且指出第几步——绝不静默回落真跑（会偷烧 key，
    还会把"我的改动让轨迹发散了"这个最有价值的信号当噪声吞掉）。"""
    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        list(wrap_recording(AnthropicProvider(), store).stream_chat(_msgs()))
        r = make_replay("fake-model-1", store)
        list(r.stream_chat(_msgs()))            # 第 1 步命中
        try:
            list(r.stream_chat(_msgs("换个问题")))  # 第 2 步 miss
            raise AssertionError("miss 没有报错")
        except CassetteMiss as e:
            assert "第 2 次" in str(e), str(e)
            assert "重录" in str(e), "报错要给出路，不能只说失败"


def test_replay_keeps_recorded_provider_kind():
    """回放沿用录音里记的 provider 类名——两种模式产出的 Run Record 才可比。"""
    with tempfile.TemporaryDirectory() as d:
        store = CassetteStore(d)
        list(wrap_recording(AnthropicProvider(), store).stream_chat(_msgs()))
        assert provider_kind(make_replay("fake-model-1", store)) == "anthropic"


# ---- 工厂接线 ---------------------------------------------------------------

def test_factory_is_off_by_default():
    """不设环境变量 = 完全关闭、零行为改动。"""
    from agentcore.providers.cassette import cassette_mode, cassette_store
    for k in ("HERMES_CASSETTE_MODE", "HERMES_CASSETTE_DIR"):
        os.environ.pop(k, None)
    assert cassette_mode() == "" and cassette_store() is None


def test_factory_replay_skips_credentials_entirely():
    """`build_provider` 在 replay 分支要**在取 key 之前**返回——否则 CI 上没 key 直接抛。"""
    import inspect

    from agentcore.providers import build_provider
    src = inspect.getsource(build_provider)
    i_replay = src.index('mode == "replay"')
    i_key = src.index("resolve_api_key")
    assert i_replay < i_key, "replay 分支必须早于 resolve_api_key"


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
