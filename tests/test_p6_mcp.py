"""P6.4 MCP 接入自测（纯逻辑，无真 server、无 mcp SDK 依赖）。

覆盖：结果转换 convert_result、命名 qualified_name、McpTool.run（文本/图片/错误/异常）、
MCPConfig 解析、build_registry 接入 mcp_tools + 危险标记。真连 server 属 Windows 验证范围。

运行：python tests/test_p6_mcp.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import MCPConfig  # noqa: E402
from agentcore.mcp_client.manager import _decode_best, _flatten_exc, tool_input_schema  # noqa: E402


def test_tool_input_schema_accepts_both_sdk_field_names():
    """mcp SDK **2.0 起把 `inputSchema` 改名 `input_schema`**——两个都要认。

    2026-08-07 真跑撞到：装了 mcp 2.0.0 后连 server 直接
    `AttributeError: 'Tool' object has no attribute 'inputSchema'`，**所有 MCP server 全连不上**
    （浏览器穿透 / 文件系统 / Codex 模板一起废）。pyproject 只写 `mcp>=1.2`，新装机器拿到的就是新版。
    """
    schema = {"type": "object", "properties": {"url": {"type": "string"}}}

    class New:      # mcp >= 2.0
        input_schema = schema

    class Old:      # mcp 1.x
        inputSchema = schema

    class Neither:  # 无参工具 / 更古怪的实现
        pass

    assert tool_input_schema(New()) == schema
    assert tool_input_schema(Old()) == schema
    assert tool_input_schema(Neither()) == {"type": "object", "properties": {}}
    # 空 schema 也要给出合法对象（不能返回 None 让 provider 报错）
    class Empty:
        input_schema = None
        inputSchema = {}
    assert tool_input_schema(Empty()) == {"type": "object", "properties": {}}


def test_decode_best_handles_gbk_and_utf8():
    # Windows 中文系统错误是 GBK，别当 UTF-8 读成乱码
    assert _decode_best("系统找不到指定的路径。".encode("gbk")) == "系统找不到指定的路径。"
    assert _decode_best("ENOENT no such file".encode("utf-8")) == "ENOENT no such file"
    assert _decode_best(b"") == ""
from agentcore.mcp_client.tool import McpTool, convert_result, qualified_name  # noqa: E402
from agentcore.tools import ToolError, ToolOutput, build_registry  # noqa: E402


def test_flatten_exc_unwraps_exceptiongroup():
    # 单个异常：原样
    assert _flatten_exc(FileNotFoundError("npx 不存在")) == "FileNotFoundError: npx 不存在"
    # ExceptionGroup（anyio TaskGroup 那种）：拆出叶子真异常
    eg = ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("server 退出码 1")])
    assert _flatten_exc(eg) == "RuntimeError: server 退出码 1"
    # 嵌套 + 去重
    nested = ExceptionGroup("g", [ExceptionGroup("h", [ValueError("bad dir"), ValueError("bad dir")])])
    assert _flatten_exc(nested) == "ValueError: bad dir"


# ---- 鸭子类型的假 MCP 内容 / 结果 ----------------------------------------
class _Text:
    type = "text"
    def __init__(self, text): self.text = text

class _Image:
    type = "image"
    def __init__(self, data, mime="image/png"): self.data, self.mimeType = data, mime

class _Result:
    def __init__(self, content, is_error=False): self.content, self.isError = content, is_error


def test_qualified_name():
    assert qualified_name("fs", "read_file") == "fs__read_file"


def test_convert_text_only():
    text, blocks, ok = convert_result(_Result([_Text("第一段"), _Text("第二段")]))
    assert ok and blocks == [] and text == "第一段\n第二段"


def test_convert_image_becomes_block():
    text, blocks, ok = convert_result(_Result([_Text("看图"), _Image("BASE64DATA", "image/jpeg")]))
    assert ok and len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "BASE64DATA"}
    assert "[图片]" in text and "看图" in text


def test_convert_error_flag():
    text, blocks, ok = convert_result(_Result([_Text("出错了")], is_error=True))
    assert ok is False and text == "出错了"


def test_convert_empty():
    text, blocks, ok = convert_result(_Result([]))
    assert ok and text == "(无输出)" and blocks == []


def test_mcptool_metadata():
    t = McpTool("fs", "read_file", "读取文件", {"type": "object"}, caller=lambda *a, **k: None)
    assert t.name == "fs__read_file"
    assert t.dangerous is True               # 默认危险
    assert "MCP:fs" in t.description
    t2 = McpTool("fs", "ls", "", {}, caller=lambda *a, **k: None, trusted=True)
    assert t2.dangerous is False             # trust 的 server 免 gate
    assert t2.input_schema == {"type": "object", "properties": {}}  # 空 schema 兜底


def test_mcptool_run_text():
    calls = []
    def caller(server, name, params, stream=None):
        calls.append((server, name, params))
        return _Result([_Text("ok 内容")])
    t = McpTool("fs", "read_file", "d", {}, caller=caller)
    out = t.run({"path": "a.txt"})
    assert out == "ok 内容"
    assert calls == [("fs", "read_file", {"path": "a.txt"})]  # 用原始名调用


# ---- 续话 id 自动接续（2026-08-20 真机痛点）--------------------------------
# 续话 id 只能靠模型自己从上一次返回里掏出来再带上，漏了就是**静默新开会话**：
# 上下文全丢、还不报错，表现成"它怎么又从头问一遍"。

class _ResultSC(_Result):
    def __init__(self, content, structured=None, is_error=False):
        super().__init__(content, is_error)
        self.structuredContent = structured


def test_sdk_camel_and_snake_field_names_are_both_accepted():
    """mcp SDK **1.x 驼峰 / 2.x 蛇形**并存。CLAUDE.md 记过同类坑（inputSchema），
    但当时只改了那一个——2026-08-20 端到端真跑才发现还漏着两处，后果都是**静默的**：

      `isError` 没读到 → MCP 工具报错**从来没被识别成错误**，错误文本被当正常结果回灌；
      `structuredContent` 没读到 → 续话 id 一直取不到，自动接续形同虚设。
    """
    from agentcore.mcp_client.tool import convert_result, extract_thread_id

    class _Snake:      # mcp 2.x
        content = [_Text("出错了")]
        is_error = True
        structured_content = {"threadId": "T-9"}

    class _Camel:      # mcp 1.x
        content = [_Text("出错了")]
        isError = True
        structuredContent = {"threadId": "T-9"}

    for r in (_Snake(), _Camel()):
        text, _blocks, ok = convert_result(r)
        assert ok is False, r          # 错就是错，别当成功
        assert extract_thread_id(r) == "T-9", r

    # 图片的 mimeType / mime_type 同理
    class _ImgSnake:
        type = "image"
        data = "IMG"
        mime_type = "image/jpeg"

    class _R:
        content = [_ImgSnake()]
    _t, blocks, _ok = convert_result(_R())
    assert blocks[0]["source"]["media_type"] == "image/jpeg"


def test_extract_thread_id_only_trusts_structured_content():
    """只认 structuredContent。**不去正文里正则捞**——正文是自然语言、形状随模型变，
    靠它接续迟早接到别的会话上，比接不上更糟。"""
    from agentcore.mcp_client.tool import extract_thread_id
    assert extract_thread_id(_ResultSC([_Text("x")], {"threadId": "T-1"})) == "T-1"
    assert extract_thread_id(_ResultSC([_Text("x")], {"thread_id": " T-2 "})) == "T-2"
    assert extract_thread_id(_ResultSC([_Text('thread id: T-9')], None)) == ""
    assert extract_thread_id(_Result([_Text("x")])) == ""       # 没这个属性也不能抛


def test_thread_param_is_decided_by_schema_not_tool_name():
    """按 schema 认，不按 `*-reply` 这种命名约定认——约定不是契约。"""
    from agentcore.mcp_client.tool import thread_param
    assert thread_param({"properties": {"threadId": {}, "prompt": {}}}) == "threadId"
    assert thread_param({"properties": {"prompt": {}}}) == ""
    assert thread_param({}) == ""


def test_reply_tool_auto_resumes_last_thread():
    from agentcore.mcp_client.tool import ThreadMemory

    mem = ThreadMemory()
    seen = []
    def caller(server, name, params, stream=None):
        seen.append(params)
        return _ResultSC([_Text("ok")], {"threadId": "T-1"})

    start = McpTool("codex", "codex", "d", {"properties": {"prompt": {}}},
                    caller=caller, threads=mem)
    out = start.run({"prompt": "做事"})
    assert "[thread] T-1" in out, out          # 回显：模型自己带上才是常态，自动接续只是兜底

    reply = McpTool("codex", "codex-reply", "d",
                    {"properties": {"prompt": {}, "threadId": {}}}, caller=caller, threads=mem)
    out2 = reply.run({"prompt": "继续"})
    assert seen[-1]["threadId"] == "T-1", seen[-1]
    assert out2.startswith("[已自动接续 threadId=T-1]"), out2[:60]


def test_cwd_follows_the_session_workspace():
    """agent 型 server 的 cwd 是**按调用**给的参数：不补的话它就在 hermes 自己的
    安装目录里干活（真机踩过），而且**不报错**。"""
    seen = []
    t = McpTool("codex", "codex", "d", {"properties": {"prompt": {}, "cwd": {}}},
                caller=lambda s, n, p, st=None: seen.append(p) or _Result([_Text("ok")]))
    bound = t.bind_workspace("D:/proj")
    bound.run({"prompt": "做事"})
    assert seen[-1]["cwd"] == "D:/proj"
    # 模型显式给了就不覆盖
    bound.run({"prompt": "做事", "cwd": "D:/other"})
    assert seen[-1]["cwd"] == "D:/other"
    # 原实例不受影响（同一批工具被所有会话共用，改自己会串台）
    assert t.workspace is None
    t.run({"prompt": "做事"})
    assert "cwd" not in seen[-1]


def test_agentic_is_decided_by_cwd_not_by_thread_key():
    """**「schema 收 cwd」＝agent 型**。别用 thread key 当判据——`codex__codex` 的 schema 里
    根本没有 threadId（只有 codex-reply 有），用它当门会让起始那次既不补 cwd、也不记改动。
    2026-08-20 端到端真跑就是这么发现的。"""
    start = McpTool("codex", "codex", "d", {"properties": {"prompt": {}, "cwd": {}}},
                    caller=lambda *a, **k: _Result([_Text("ok")]))
    reply = McpTool("codex", "codex-reply", "d", {"properties": {"prompt": {}, "threadId": {}}},
                    caller=lambda *a, **k: _Result([_Text("ok")]))
    plain = McpTool("fs", "read_file", "d", {"properties": {"path": {}}},
                    caller=lambda *a, **k: _Result([_Text("ok")]))
    assert start._takes_cwd is True and start._thread_key == ""
    assert reply._takes_cwd is False and reply._thread_key == "threadId"
    assert plain._takes_cwd is False


def test_plain_tools_never_touch_git():
    """普通 MCP 工具（文件系统/浏览器）不该为了一次读文件去跑 git。"""
    calls = []
    import agentcore.mcp_client.tool as tool_mod
    orig = tool_mod.status_lines
    tool_mod.status_lines = lambda cwd, **k: calls.append(cwd) or []
    try:
        t = McpTool("fs", "read_file", "d", {"properties": {"path": {}}},
                    caller=lambda *a, **k: _Result([_Text("ok")]))
        out = t.run({"path": "a.txt"})
    finally:
        tool_mod.status_lines = orig
    assert calls == [] and "git status" not in out


def test_prepare_runs_before_the_gate_so_the_bar_shows_real_params():
    """确认条上要显示**真正会执行的参数**——cwd 决定它在哪儿干活，看不到就等于没确认。
    所以 loop 必须在 gate 之前调 prepare。"""
    import inspect

    from agentcore.agent import loop as loop_mod
    src = inspect.getsource(loop_mod.AgentLoop._run_tool) if hasattr(loop_mod.AgentLoop, "_run_tool") \
        else inspect.getsource(loop_mod)
    i_prep, i_gate = src.find("tool.prepare("), src.find("self.gate.confirm(")
    assert 0 < i_prep < i_gate, "prepare 必须排在 gate.confirm 之前"


def test_prepare_is_idempotent_and_cheap():
    """run() 内部还会再调一次 prepare（存量调用方可能直接 run）——重复调用不能出岔。"""
    from agentcore.mcp_client.tool import RESUMED_KEY, ThreadMemory

    mem = ThreadMemory(); mem.set("codex", "T-1")
    t = McpTool("codex", "codex-reply", "d", {"properties": {"threadId": {}, "cwd": {}}},
                caller=lambda *a, **k: _Result([_Text("ok")]), threads=mem).bind_workspace("/w")
    once = t.prepare({"prompt": "x"})
    twice = t.prepare(dict(once))
    assert twice["threadId"] == "T-1" and twice["cwd"] == "/w"
    assert RESUMED_KEY not in twice          # 第二次不该再标"自动接续"


def test_explicit_thread_id_is_never_overwritten():
    """模型显式给了就以它为准——它可能有意开新会话或切到别的线程。"""
    from agentcore.mcp_client.tool import ThreadMemory

    mem = ThreadMemory()
    mem.set("codex", "T-OLD")
    seen = []
    reply = McpTool("codex", "codex-reply", "d", {"properties": {"threadId": {}}},
                    caller=lambda s, n, p, st=None: seen.append(p) or _ResultSC([_Text("ok")]),
                    threads=mem)
    reply.run({"threadId": "T-NEW"})
    assert seen[-1]["threadId"] == "T-NEW"


def test_tools_without_thread_key_are_untouched():
    """普通 MCP 工具（文件系统/浏览器）不该被塞进莫名其妙的参数。"""
    from agentcore.mcp_client.tool import ThreadMemory

    mem = ThreadMemory()
    mem.set("fs", "T-1")
    seen = []
    t = McpTool("fs", "read_file", "d", {"properties": {"path": {}}},
                caller=lambda s, n, p, st=None: seen.append(p) or _Result([_Text("ok")]),
                threads=mem)
    out = t.run({"path": "a.txt"})
    assert seen[-1] == {"path": "a.txt"} and "[thread]" not in out


def test_always_confirm_flag_and_trust_are_mutually_exclusive():
    """agent 型 server 每次都问；trust=True 时本来就不过 gate，两者同时开只会自相矛盾——
    以 trust 为准（不过 gate），always_confirm 归 False，别让配置矛盾变成运行期悬念。"""
    t = McpTool("codex", "codex", "d", {}, caller=lambda *a, **k: None, always_confirm=True)
    assert t.dangerous is True and t.always_confirm is True
    t2 = McpTool("codex", "codex", "d", {}, caller=lambda *a, **k: None,
                 trusted=True, always_confirm=True)
    assert t2.dangerous is False and t2.always_confirm is False
    t3 = McpTool("fs", "ls", "d", {}, caller=lambda *a, **k: None)
    assert t3.always_confirm is False        # 默认不打扰


def test_mcptool_forwards_stream_callback():
    """agent 型 server（codex）一次调用要跑几分钟——没有过程推送就是**黑箱**。

    hermes 早有 `wants_stream` 这条管子（前台 shell 用它边跑边看），MCP 工具接上即可，
    前端一行不用改。不发通知的 server 自然没增量，行为不变。
    """
    seen = {}
    def caller(server, name, params, stream=None):
        seen["stream"] = stream
        if stream:
            stream("progress", "正在读文件…\n")
        return _Result([_Text("done")])
    t = McpTool("fs", "read_file", "d", {}, caller=caller)
    assert t.wants_stream is True          # 不声明的话 loop 根本不会给回调
    got = []
    assert t.run({}, stream=lambda kind, delta: got.append((kind, delta))) == "done"
    assert got == [("progress", "正在读文件…\n")]
    assert seen["stream"] is not None


def test_mcptool_run_image_returns_tooloutput():
    t = McpTool("cam", "snap", "d", {}, caller=lambda *a, **k: _Result([_Image("IMG")]))
    out = t.run({})
    assert isinstance(out, ToolOutput)
    assert out.blocks[0]["type"] == "image" and out.blocks[0]["source"]["data"] == "IMG"


def test_mcptool_run_error_raises_toolerror():
    t = McpTool("fs", "boom", "d", {}, caller=lambda *a, **k: _Result([_Text("权限不足")], is_error=True))
    try:
        t.run({})
        assert False, "应抛 ToolError"
    except ToolError as e:
        assert "权限不足" in str(e)


def test_mcptool_run_exception_raises_toolerror():
    def caller(*a, **k):
        raise ConnectionError("管道已断")
    t = McpTool("fs", "x", "d", {}, caller=caller)
    try:
        t.run({})
        assert False, "应抛 ToolError"
    except ToolError as e:
        assert "MCP 调用失败" in str(e) and "管道已断" in str(e)


def test_config_parsing():
    cfg = MCPConfig(**{
        "enabled": True,
        "servers": {
            "fs": {"command": "npx", "args": ["-y", "pkg", "/dir"], "trust": True},
            "off": {"command": "x", "enabled": False},
        },
    })
    assert cfg.enabled and cfg.connect_timeout == 60   # 默认 60s 容纳首次 npx 下载
    assert cfg.servers["fs"].command == "npx" and cfg.servers["fs"].trust is True
    assert cfg.servers["off"].enabled is False


def test_per_server_call_timeout_overrides_global():
    """agent 型 server（`codex mcp-server`）一次调用＝跑完一整个会话，分钟级。

    **超时该按 server 定**：为了一个慢 server 调高全局，会把 Playwright 之类的一起放松，
    于是"卡死"要等十几分钟才暴露。没配的 server 必须一字不差地跟随全局。
    """
    from agentcore.mcp_client.manager import McpManager

    cfg = MCPConfig(**{
        "enabled": True,
        "call_timeout": 60,
        "servers": {
            "codex": {"command": "codex", "args": ["mcp-server"], "call_timeout": 900},
            "fs": {"command": "npx", "args": ["-y", "pkg"]},
        },
    })
    assert cfg.servers["codex"].call_timeout == 900
    assert cfg.servers["fs"].call_timeout is None      # 不写＝跟随全局，不是 0

    m = McpManager(cfg)
    assert m.call_timeout_for("codex") == 900.0
    assert m.call_timeout_for("fs") == 60.0
    assert m.call_timeout_for("不存在的 server") == 60.0   # 取不到配置也得给个能用的值


def test_zero_call_timeout_falls_back_to_global():
    """0 / None 都当"没配"——**别把它当成"立刻超时"**：那会让每次调用当场失败，
    而用户的本意几乎必然是"没填"。"""
    from agentcore.mcp_client.manager import McpManager

    cfg = MCPConfig(**{"enabled": True, "call_timeout": 45,
                       "servers": {"x": {"command": "c", "call_timeout": 0}}})
    assert McpManager(cfg).call_timeout_for("x") == 45.0


def test_registry_includes_mcp_tools_and_marks_dangerous(tmp: Path):
    mcp_tools = [
        McpTool("fs", "read_file", "d", {}, caller=lambda *a, **k: None),
        McpTool("fs", "ls", "d", {}, caller=lambda *a, **k: None, trusted=True),
    ]
    reg = build_registry(tmp, screenshot=False, memory_store=None, mcp_tools=mcp_tools)
    names = reg.names()
    assert "fs__read_file" in names and "fs__ls" in names
    assert "read_file" in names           # 内置工具仍在
    assert reg.is_dangerous("fs__read_file") is True
    assert reg.is_dangerous("fs__ls") is False
    # schema 产出包含 MCP 工具
    schemas = {s["name"] for s in reg.to_schemas()}
    assert "fs__read_file" in schemas


def _run_all():
    import inspect
    import tempfile
    fns = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            try:
                if "tmp" in inspect.signature(fn).parameters:
                    fn(Path(d))
                else:
                    fn()
                print(f"  ok  {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                raise
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
