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
