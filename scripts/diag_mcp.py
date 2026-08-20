"""MCP server 连不上时的第一手排查：**照 hermes 完全相同的方式**起一次，把过程摊开。

    python scripts/diag_mcp.py            # 诊断所有已启用的 server
    python scripts/diag_mcp.py codex      # 只诊断某一个

面板只能显示一句 `Connection closed`（外加 server 自己吐的 stderr），信息不够定位。
本脚本把**中间每一层**都打出来：配置存的是什么 → 命令解析到哪个可执行文件 → 子进程起没起来 →
握手拿到几个工具。踩过的两种真实故障都能一眼看出：
  ① 参数没进 `args`（写进了命令框）→ codex 被当交互式 TUI 启动 → `stdin is not a terminal`；
  ② PATH 里有两份同名命令，终端解析到新的、子进程解析到旧的。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.config import APP_DIR, USER_MCP_FILE, load_config  # noqa: E402


def _which(cmd: str) -> str:
    """命令实际解析到哪个文件。Windows 上还要试 .cmd/.bat（npm 装的是 shim）。"""
    hit = shutil.which(cmd)
    if hit:
        return hit
    if sys.platform == "win32":
        for ext in (".cmd", ".bat", ".exe"):
            hit = shutil.which(cmd + ext)
            if hit:
                return f"{hit}  （靠补 {ext} 才找到）"
    return "❌ 没找到（PATH 里没有这个命令）"


def _all_matches(cmd: str) -> list:
    """PATH 里的**全部**同名候选——两份同名命令是真踩过的坑，只看第一个会漏。"""
    out, seen = [], set()
    import os
    exts = os.environ.get("PATHEXT", "").split(os.pathsep) if sys.platform == "win32" else [""]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for e in [""] + [x for x in exts if x]:
            p = Path(d) / (cmd + e)
            if p.is_file() and str(p) not in seen:
                seen.add(str(p))
                out.append(str(p))
    return out


_CFG = None


def probe(name: str, spec: dict, call_timeout: float) -> None:
    print(f"\n{'=' * 62}\n■ server: {name}")
    command = spec.get("command") or ""
    args = list(spec.get("args") or [])
    cwd = spec.get("cwd") or None
    print(f"  启动命令 : {command!r}")
    print(f"  参数     : {args!r}" + ("   ⚠ 空的！" if not args else ""))
    print(f"  工作目录 : {cwd!r}" + ("   ⚠ 未设，会用 hermes 自己的目录" if not cwd else ""))
    print(f"  调用超时 : {spec.get('call_timeout') or call_timeout} 秒"
          + ("   ⚠ 跟随全局；agent 型 server（codex）几乎必超时" if not spec.get("call_timeout") else ""))
    trust = bool(spec.get("trust", False))
    print(f"  免确认   : {trust}"
          + ("   ⚠ 该 server 的工具**不过权限确认**（它会自主写文件/跑命令）" if trust else
             "（每次调用都会弹确认）"))
    if " " in command:
        print("  ⚠ **启动命令里带空格**——参数要放「参数」框、一行一个，不能写进命令框")
    print(f"  解析到   : {_which(command)}")
    others = _all_matches(command)
    if len(others) > 1:
        print("  ⚠ PATH 里有多份同名命令（终端与子进程可能用的不是同一个）：")
        for o in others:
            print(f"      {o}")
    if cwd and not Path(cwd).is_dir():
        print(f"  ❌ 工作目录不存在：{cwd}")
        return

    print("  → 起子进程并握手…")
    try:
        p = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1, cwd=cwd)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 起不来：{type(e).__name__}: {e}")
        return
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "hermes-diag", "version": "1"}}}
        p.stdin.write(json.dumps(req) + "\n")
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized",
                                  "params": {}}) + "\n")
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                  "params": {}}) + "\n")
        p.stdin.flush()
        deadline, tools = time.time() + 30, None
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2 and "result" in msg:
                tools = [t["name"] for t in msg["result"].get("tools", [])]
                break
        if tools:
            print(f"  ✅ 连上了，工具：{tools}")
            try:
                perm_note(_CFG, name, tools)
            except Exception as e:  # noqa: BLE001 — 诊断信息拿不到不影响主结论
                print(f"  （权限判定跳过：{type(e).__name__}）")
        else:
            print("  ❌ 握手没拿到工具清单")
    finally:
        p.kill()
        err = (p.stderr.read() or "").strip()
        if err:
            print("  ── server 的 stderr（前 10 行）──")
            for l in err.splitlines()[:10]:
                print("   ", l[:160])
            if "stdin is not a terminal" in err:
                print("  💡 这句＝它被当**交互式**启动了：多半参数没传到，"
                      "或这个可执行文件的版本没有该子命令（对 codex 应是 `mcp-server` 一行）")


def perm_note(cfg, server: str, tools: list) -> None:
    """除了 trust，还有两条路会让它不弹确认——一起报出来，省得对着"怎么没弹"猜。"""
    from agentcore.permissions import evaluate
    allow = list(getattr(cfg.agent, "permissions", None).allow or []) if getattr(
        cfg.agent, "permissions", None) else []
    deny = list(getattr(cfg.agent, "permissions", None).deny or []) if getattr(
        cfg.agent, "permissions", None) else []
    hit = [t for t in tools if evaluate(allow, deny, f"{server}__{t}", {}) == "allow"]
    if hit:
        print(f"  ⚠ permissions.allow 里有规则命中 {hit} —— 这些也不会弹确认")
    print("  提示：本会话点过「全部允许」、或在 /crazy 免确认模式下，同样不弹确认"
          "（那是会话状态，不在配置里，本脚本看不到）")


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_config()
    global _CFG
    _CFG = cfg
    print(f"hermes 目录 : {APP_DIR}")
    f = APP_DIR / USER_MCP_FILE
    print(f"面板存盘    : {f}  {'（存在）' if f.is_file() else '（不存在——面板还没存过）'}")
    print(f"mcp.enabled : {cfg.mcp.enabled}" + ("" if cfg.mcp.enabled else "   ⚠ 关着，工具不会挂载"))
    servers = {n: s.model_dump() for n, s in cfg.mcp.servers.items()}
    if not servers:
        print("没有配置任何 MCP server。")
        return 1
    for name, spec in servers.items():
        if only and name != only:
            continue
        if not spec.get("enabled", True):
            print(f"\n■ server: {name} —— 已停用，跳过")
            continue
        probe(name, spec, cfg.mcp.call_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
