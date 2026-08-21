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
    # **口径改过一次**（2026-08-21）：原先是"模型显式给了就不覆盖"，
    # 真机上 Codex 因此整场在别的目录里干活、还起了服务，工作区一个文件都没有。
    # 现在一律夹回工作区（工作区内的子目录仍照用，见 test_cwd_is_forced_into_the_workspace）。
    bound.run({"prompt": "做事", "cwd": "D:/other"})
    assert seen[-1]["cwd"] == "D:/proj"
    # 原实例不受影响（同一批工具被所有会话共用，改自己会串台）
    assert t.workspace is None
    t.run({"prompt": "做事"})
    assert "cwd" not in seen[-1]


def test_agentic_flag_is_decided_by_code_not_guessed_by_name():
    """"这次是不是委派"由代码判定后随 `tool_use` 事件下发——**别让前端按工具名猜**，
    名字是 server 起的，猜必然漏。"""
    import inspect

    from agentcore.agent.loop import AgentLoop
    src = inspect.getsource(AgentLoop)
    assert src.count('"agentic": self._is_agentic') == 2, "并行与串行两条分支都要带上"

    class _Reg:
        def __init__(self, tools):
            self._t = tools

        def get(self, name):
            return self._t[name]

    class _Agentic:
        _takes_cwd = True

    class _Plain:
        pass

    loop = AgentLoop.__new__(AgentLoop)
    loop.registry = _Reg({"codex__codex": _Agentic(), "read_file": _Plain()})
    assert loop._is_agentic("codex__codex") is True
    assert loop._is_agentic("read_file") is False
    assert loop._is_agentic("不存在的工具") is False       # 取不到不能抛


def test_cancel_all_stops_inflight_calls():
    """agent 型 server 一次调用几分钟，没有出口就只能干等到 call_timeout（真机 900s）——
    「停止」必须真的能停。"""
    import concurrent.futures

    from agentcore.config import MCPConfig
    from agentcore.mcp_client.manager import McpManager

    m = McpManager(MCPConfig(enabled=True))
    running = concurrent.futures.Future()
    done = concurrent.futures.Future()
    done.set_result("已经跑完了")
    m._inflight = {running, done}
    assert m.cancel_all() == 1          # 只取消得动没跑完的那个
    assert running.cancelled() and done.result() == "已经跑完了"
    assert m.cancel_all() == 0          # 再来一次不会重复计数


def test_cancelled_call_reads_as_user_stop_not_as_failure():
    """原样抛 CancelledError 会被上层包成"MCP 调用失败：CancelledError"——
    看着像故障，而不是"你自己停的"。"""
    import concurrent.futures

    from agentcore.config import MCPConfig
    from agentcore.mcp_client.manager import McpManager
    from agentcore.tools.base import ToolError as _TE

    m = McpManager(MCPConfig(enabled=True, servers={"s": {"command": "x"}}))
    class _Sess:                       # call_tool 只是被 _submit 吞掉，不会真跑
        def call_tool(self, *a, **k):
            return None

    m._loop = object()
    m._sessions["s"] = _Sess()
    cancelled = concurrent.futures.Future()
    cancelled.cancel()
    m._submit = lambda coro: cancelled
    try:
        m.call("s", "t", {})
    except _TE as e:
        assert "用户停止" in str(e), e
    else:
        raise AssertionError("取消后应抛可读的 ToolError")
    assert not m._inflight          # 登记表要清干净，别泄漏


def test_stop_forwards_to_mcp():
    """Conversation.stop() 必须把停止传到 MCP，否则按了停止仍要等 call_timeout。"""
    import inspect

    from agentcore.bridge import conversation as conv_mod
    src = inspect.getsource(conv_mod.Conversation.stop)
    assert "cancel_all" in src


def test_cwd_is_forced_into_the_workspace():
    """**"它只可能在这个工作区里干活"这条保证，比让模型自由选目录值钱得多。**

    2026-08-21 真机：Codex 在别的目录里建了整个项目、还从那儿起了服务，
    工作区里一个文件都没有，用户翻进程列表才发现。原先是"模型没给才补"——
    给错一个目录就整场跑偏，而且全程无声。
    """
    from agentcore.mcp_client.tool import clamp_cwd

    # 工作区内的子目录是正当需求，照用
    assert clamp_cwd("/ws/proj/sub", "/ws/proj") == ("/ws/proj/sub", "")
    assert clamp_cwd("/ws/proj", "/ws/proj")[0] == "/ws/proj"
    assert clamp_cwd("", "/ws/proj") == ("/ws/proj", "")
    # 工作区外 → 改回根，并**说明**（不说明就又变成一次静默纠偏）
    got, why = clamp_cwd("/somewhere/else", "/ws/proj")
    assert got == "/ws/proj" and "工作区之外" in why
    # 没绑工作区时不干预（存量调用方零变化）
    assert clamp_cwd("/anywhere", "") == ("/anywhere", "")

    seen = []
    t = McpTool("codex", "codex", "d", {"properties": {"prompt": {}, "cwd": {}}},
                caller=lambda s_, n, p, st=None: seen.append(p) or _Result([_Text("ok")]),
                ).bind_workspace("/ws/proj")
    out = t.run({"prompt": "x", "cwd": "/etc"})
    assert seen[-1]["cwd"] == "/ws/proj", seen[-1]
    assert out.startswith("[已改回工作区]"), out[:40]
    # 内部标记不能混进真正发给 server 的参数
    assert not [k for k in seen[-1] if k.startswith("_hermes")]


def test_no_change_is_stated_not_silent():
    """agent 自述"已创建 xxx"而工作区毫无改动，是最值得当场看见的一种矛盾——
    真机就是这么被漏过去的。但**测不了要保持安静**，别把"没测"说成"没改"。"""
    from agentcore.mcp_client.gitwatch import render_changes

    assert "工作区无改动" in render_changes([], measurable=True)
    assert render_changes([], measurable=False) == ""
    assert "?? a.py" in render_changes(["?? a.py"])


def test_effective_cwd_is_echoed_in_the_result():
    """agent 在别的目录里建文件时**完全无声**：它自述"已创建"，工作区里却什么都没有，
    人要靠翻进程、搜磁盘才查得出来（2026-08-21 真机：Codex 在别处建了项目并起了服务）。
    回显工作目录之后，"它说建了 → 工作区没有 → 目录写着别处"三句话就能对上。
    """
    t = McpTool("codex", "codex", "d", {"properties": {"prompt": {}, "cwd": {}}},
                caller=lambda *a, **k: _Result([_Text("已创建 server.js")]))
    out = t.run({"prompt": "x", "cwd": "/tmp/proj"})
    assert "[工作目录] /tmp/proj" in out, out
    # 不收 cwd 的普通工具不该多这一行
    plain = McpTool("fs", "read_file", "d", {"properties": {"path": {}}},
                    caller=lambda *a, **k: _Result([_Text("ok")]))
    assert "[工作目录]" not in plain.run({"path": "a.txt"})


def test_working_inside_hermes_own_dir_is_called_out_loudly():
    """真机踩到：面板模板把"当前工作区"填成了 hermes 的临时工作区
    `data/workspaces/_scratch`，于是 Codex 全干在了 hermes 自己的目录里。
    **它不报错、结果也看着正常**——所以必须在调用时就喊出来。"""
    from agentcore.config import APP_DIR

    warned = []
    t = McpTool("codex", "codex", "d", {"properties": {"prompt": {}, "cwd": {}}},
                caller=lambda *a, **k: _Result([_Text("ok")]))
    out = t.run({"prompt": "x", "cwd": str(APP_DIR / "data" / "workspaces" / "_scratch")},
                stream=lambda kind, text: warned.append(text))
    assert out.startswith("⚠ 工作目录是 hermes 自己的目录"), out[:60]
    assert warned and "不是你的项目" in warned[0]      # 流里也要出现，别只藏在结果里

    # 正常项目目录不该被打扰
    quiet = t.run({"prompt": "x", "cwd": "/tmp"}, stream=lambda k, x: warned.append(x))
    assert not quiet.startswith("⚠")


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


def test_conflicting_permission_flags_resolve_to_the_stricter_one():
    """两个开关都开时**以更严的为准**（每次都问压过免确认）。

    **口径改过一次**（2026-08-20）：原先写的是"trust 优先"，真机上用户配里
    `trust=true` + `always_confirm=true`，于是"每次都问"被**静默作废**、一次确认都没弹。
    权限上宁可多问一次，也不能让两个开关打架的结果是"谁都没拦住"。
    面板已改成两者互斥，这里是兜底（手编 config.yaml 仍可能写出矛盾）。
    """
    t = McpTool("codex", "codex", "d", {}, caller=lambda *a, **k: None, always_confirm=True)
    assert t.dangerous is True and t.always_confirm is True
    both = McpTool("codex", "codex", "d", {}, caller=lambda *a, **k: None,
                   trusted=True, always_confirm=True)
    assert both.dangerous is True and both.always_confirm is True, "矛盾时该按更严的走"
    only_trust = McpTool("fs", "ls", "d", {}, caller=lambda *a, **k: None, trusted=True)
    assert only_trust.dangerous is False and only_trust.always_confirm is False
    plain = McpTool("fs", "ls", "d", {}, caller=lambda *a, **k: None)
    assert plain.dangerous is True and plain.always_confirm is False   # 默认危险但不强制每次


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


def test_readable_toolerror_is_not_wrapped_twice():
    """"调用已被用户停止"再套一层"MCP 调用失败：ToolError: …"就看不出是自己停的了。"""
    def caller(*a, **k):
        raise ToolError("调用已被用户停止")

    t = McpTool("codex", "codex", "d", {}, caller=caller)
    try:
        t.run({})
    except ToolError as e:
        assert str(e) == "调用已被用户停止", e


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
