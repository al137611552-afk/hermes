"""最小 stdio MCP server（echo / add），用于零依赖验证 P6.4 的 MCP 接入。

不需要 Node/npx——只要装了本项目（已带 mcp SDK）即可。在 config.yaml 里这样配：

    mcp:
      enabled: true
      servers:
        echo:
          command: python              # Windows 上也可写 py
          args: ["scripts/mcp_echo_server.py"]
          trust: false

然后启动应用，让模型调用 echo__echo / echo__add，应能看到工具块并过权限 gate。
"""
from __future__ import annotations

# **1.x 与 2.x 两个都认**（同 `tool_input_schema` 的纪律）：2.0 把 `mcp.server.fastmcp.FastMCP`
# 改名成了 `mcp.server.mcpserver.MCPServer`，装了 2.x 的机器上这个 server 起不来（子进程
# ModuleNotFoundError，客户端只看到一句 "Connection closed"）。装饰器与 run() 两版同名。
try:
    from mcp.server.fastmcp import FastMCP as _Server      # mcp 1.x
except ImportError:  # pragma: no cover - 取决于装的是哪一版 SDK
    from mcp.server.mcpserver import MCPServer as _Server  # mcp 2.x

mcp = _Server("echo")


@mcp.tool()
def echo(text: str) -> str:
    """回显传入的文本（用于连通性测试）。"""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> str:
    """返回两个整数之和。"""
    return str(a + b)


@mcp.tool()
def sleep(seconds: float) -> str:
    """睡 `seconds` 秒再返回（上限 120s）。

    用来验证「停止」能取消**在飞**的调用：真机上 agent 型 server（codex）一次跑几分钟，
    没有慢工具就复现不出"停一个对话别停到另一个"那类归属问题。
    """
    import time
    s = max(0.0, min(float(seconds), 120.0))
    time.sleep(s)
    return f"slept {s}s"


if __name__ == "__main__":
    mcp.run("stdio")
