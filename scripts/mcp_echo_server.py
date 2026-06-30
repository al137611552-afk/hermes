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

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """回显传入的文本（用于连通性测试）。"""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> str:
    """返回两个整数之和。"""
    return str(a + b)


if __name__ == "__main__":
    mcp.run("stdio")
