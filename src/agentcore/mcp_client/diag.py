"""MCP server 配置体检：把「为什么连不上」摊开成可读的几条（纯逻辑 + 受控 IO 分离）。

面板原来只能显示一句 `Connection closed` 加 server 自己的 stderr——信息不够定位，
用户对着它猜了四轮（2026-08-20）。真正踩到的两种故障都**不在**那句话里：

  ① 参数写进了「启动命令」框 → codex 被当交互式 TUI 启动 → `stdin is not a terminal`；
  ② PATH 里有两份同名命令，终端解析到新的、子进程解析到旧的。

`analyze_spec()` 是**纯函数**（不碰盘、不起进程），可注入解析结果单测；
`resolve_command` / `all_in_path` / `probe_connect` 是受控 IO。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

OK, WARN, BAD = "ok", "warn", "bad"


def all_in_path(cmd: str) -> list:
    """PATH 里**全部**同名候选（受控 IO）。只看第一个会漏掉"两份同名"这种坑。"""
    if not cmd or os.sep in cmd or (os.altsep and os.altsep in cmd):
        return []
    exts = [e for e in os.environ.get("PATHEXT", "").split(os.pathsep) if e] \
        if sys.platform == "win32" else []
    out, seen = [], set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for e in [""] + exts:
            p = Path(d) / (cmd + e)
            if not p.is_file():
                continue
            # Windows 路径**大小写不敏感**，且 PATH 里同一目录常出现多次——
            # 不归一就会把同一个文件报成"有多份同名命令"（真机误报过）
            key = str(p).lower() if sys.platform == "win32" else str(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(str(p))
    return out


def resolve_command(cmd: str) -> str:
    """命令实际解析到哪个文件（受控 IO）。Windows 上还要试 .cmd/.bat（npm 装的是垫片）。"""
    hit = shutil.which(cmd)
    if hit:
        return hit
    if sys.platform == "win32":
        for ext in (".cmd", ".bat", ".exe"):
            hit = shutil.which(cmd + ext)
            if hit:
                return hit
    return ""


def sdk_capabilities() -> dict:
    """装的 mcp SDK 支不支持两条关键通道（受控 IO：只读版本与函数签名）。

    真机反馈"没有边跑边出字"时，第一件要分清的就是**配置问题还是环境问题**——
    过程流依赖 `notification_bindings`（或退而求其次的 `message_handler`），
    老 SDK 两条都没有的话，接得再对也不会有输出。
    """
    out = {"version": "?", "events": False, "message_handler": False}
    try:
        import importlib.metadata as md
        out["version"] = md.version("mcp")
    except Exception:  # noqa: BLE001
        pass
    try:
        import inspect

        from mcp import ClientSession
        sig = inspect.signature(ClientSession.__init__).parameters
        out["events"] = "notification_bindings" in sig
        out["message_handler"] = "message_handler" in sig
    except Exception:  # noqa: BLE001
        pass
    return out


def inside_hermes_dir(cwd: str) -> bool:
    """cwd 是不是落在 hermes 自己的安装目录里（纯逻辑，只比路径）。

    真机踩到：面板模板把"当前工作区"填成了 `data/workspaces/_scratch`——那是 hermes 的
    临时工作区，不是用户的项目，agent 会在那儿建文件而**不报错**。
    """
    try:
        from ..config import APP_DIR
        here = str(APP_DIR).replace("\\", "/").rstrip("/").lower()
        target = str(cwd).replace("\\", "/").rstrip("/").lower()
        return bool(here) and (target == here or target.startswith(here + "/"))
    except Exception:  # noqa: BLE001
        return False


def analyze_spec(spec: dict, global_timeout: float, *, resolved: str = "",
                 candidates=None, cwd_exists=None) -> list:
    """只看配置本身能发现的问题（**纯函数**）。返回 [{level, text}]。

    解析结果由调用方注入（`resolved` / `candidates` / `cwd_exists`），便于脱离环境单测。
    """
    spec = spec or {}
    command = str(spec.get("command") or "")
    args = list(spec.get("args") or [])
    cwd = str(spec.get("cwd") or "")
    out = []

    if not command:
        out.append({"level": BAD, "text": "没填启动命令"})
    elif " " in command:
        out.append({"level": BAD,
                    "text": f"启动命令里带空格（{command}）——参数要放「参数」框、一行一个，"
                            "不能写进命令框"})
    if not args:
        out.append({"level": WARN,
                    "text": "没有参数：有些 server 需要子命令（Codex 要 mcp-server）。"
                            "缺了它会以**交互模式**启动，报 stdin is not a terminal"})
    if command and not resolved:
        out.append({"level": BAD, "text": f"PATH 里找不到 {command}——填绝对路径最稳"})
    elif resolved:
        out.append({"level": OK, "text": f"解析到 {resolved}"})
    cands = list(candidates or [])
    if len(cands) > 1:
        out.append({"level": WARN,
                    "text": "PATH 里有多份同名命令，终端和子进程可能用的不是同一个："
                            + "；".join(cands[:4])})
    if not cwd:
        out.append({"level": WARN, "text": "没设工作目录：agent 型 server 会在 hermes 自己的目录里干活"})
    elif cwd_exists is False:
        out.append({"level": BAD, "text": f"工作目录不存在：{cwd}"})
    elif inside_hermes_dir(cwd):
        out.append({"level": BAD,
                    "text": f"工作目录指向 hermes 自己的目录（{cwd}）——agent 会在**那儿**干活，"
                            "不是你的项目。改成你的项目路径"})
    if not spec.get("call_timeout"):
        out.append({"level": WARN,
                    "text": f"单次调用超时跟随全局（{global_timeout:g}s）：agent 型 server "
                            "一次调用是跑完一整个会话，分钟级，几乎必超时"})
    if spec.get("trust") and spec.get("always_confirm"):
        out.append({"level": BAD,
                    "text": "「免确认」与「每次都问」**同时开着**（矛盾）。现在以更严的为准"
                            "（每次都问），但请把「免确认」关掉——否则看配置根本判断不出会不会弹确认"})
    elif spec.get("trust"):
        out.append({"level": WARN, "text": "已开「免确认」：该 server 的工具不过权限确认"})
    elif not spec.get("always_confirm"):
        out.append({"level": WARN,
                    "text": "**没开「每次都问」**：这个 server 的调用会吃「本会话全部允许」，"
                            "点过一次之后就不再弹确认（面板里勾上「每次都问」即可）"})
    else:
        out.append({"level": OK, "text": "已开「每次都问」：不吃「本会话全部允许」，每次调用都确认"})
    return out


def probe_connect(spec: dict, timeout: float = 30.0) -> dict:
    """真起一次子进程做握手（受控 IO）。返回 {ok, tools, error, stderr}。

    **照 hermes 自己的方式起**——否则测的不是同一件事。
    """
    command = str(spec.get("command") or "")
    args = [str(a) for a in (spec.get("args") or [])]
    cwd = spec.get("cwd") or None
    res = {"ok": False, "tools": [], "error": "", "stderr": ""}
    # **先解析成真实路径再起**：Windows 上 CreateProcess 只补 .exe，
    # npm 装的 `codex` 其实是 `codex.CMD`，直接 spawn 必然 WinError 2；
    # 而 hermes 真正连接时走的是 SDK（它自己会认 .cmd），于是体检报"起不来"、面板却连得上——
    # **体检说错话比不说更糟**（2026-08-20 真机误报）。
    exe = resolve_command(command) or command
    argv = [exe, *args]
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", exe, *args]      # 垫片得经 cmd 起
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1, cwd=cwd,
                             encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    try:
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "hermes-diag", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}):
            p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 2 and "result" in m:
                res["ok"] = True
                res["tools"] = [t.get("name") for t in m["result"].get("tools", [])]
                break
        if not res["ok"] and not res["error"]:
            res["error"] = "握手超时或没拿到工具清单"
    except Exception as e:  # noqa: BLE001 — 体检本身绝不能把面板带崩
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        p.kill()
        try:
            res["stderr"] = (p.stderr.read() or "").strip()[:600]
        except Exception:  # noqa: BLE001
            pass
    return res


def diagnose(name: str, spec: dict, global_timeout: float, probe: bool = True) -> dict:
    """一个 server 的完整体检（受控 IO）。返回 {name, findings:[{level,text}], tools}。"""
    command = str(spec.get("command") or "")
    cwd = spec.get("cwd") or ""
    findings = analyze_spec(
        spec, global_timeout,
        resolved=resolve_command(command) if command else "",
        candidates=all_in_path(command) if command else [],
        cwd_exists=(Path(cwd).is_dir() if cwd else None))
    caps = sdk_capabilities()
    if not caps["events"] and not caps["message_handler"]:
        findings.append({"level": WARN,
                         "text": f"mcp SDK {caps['version']} 不支持通知通道："
                                 "过程不会实时显示（升级 mcp 可解）"})
    else:
        findings.append({"level": OK,
                         "text": f"mcp SDK {caps['version']}（过程流通道"
                                 f"{'：notification_bindings' if caps['events'] else '：message_handler 兜底'}）"})
    tools = []
    if probe and command:
        r = probe_connect(spec)
        err = r["stderr"]
        if r["ok"]:
            tools = r["tools"]
            findings.append({"level": OK, "text": f"连上了，工具：{', '.join(tools) or '（无）'}"})
        else:
            findings.append({"level": BAD, "text": f"起不来/握不上手：{r['error']}"})
        if err:
            findings.append({"level": BAD if not r["ok"] else WARN, "text": f"server stderr：{err}"})
            if "stdin is not a terminal" in err:
                findings.append({"level": BAD,
                                 "text": "这句＝它被当**交互式**启动了：参数没传到，"
                                         "或这个可执行文件的版本没有该子命令"})
    return {"name": name, "findings": findings, "tools": tools}
