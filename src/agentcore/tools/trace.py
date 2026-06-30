"""运行时插桩 / 值追踪工具（FR-13.D，P5 调试能力工程化第二波核心）。

让 Agent **真看到中间值**，把「盲调猜哪步错」变成「看证据定位」。给一段驱动 `code`
（import 被测目标 + 用具体输入调用它），在**子进程**里用 `sys.settrace` 记录工作区内
函数**每一步的局部变量**，跑完回传：逐步中间值 + 程序 stdout +（若抛异常）崩溃前的轨迹。

相比「往源码插 print 再还原」：**零源码改动、无需还原、不会改坏文件、还能拿到全部局部变量**——
故采用 settrace 方案（用户原议的"注入打印"目标一致、此法更稳更全）。

纯逻辑（format_trace / _short）与受控 IO（子进程跑 harness）分离，便于单测。
"""
from __future__ import annotations

import json
import subprocess
import sys

from ..verify import _test_env
from .base import Tool, ToolError

TRACE_TIMEOUT = 30
_SENTINEL = "__HERMES_TRACE_JSON__"

# 在子进程里跑的追踪 harness：从 stdin 读 {code,target,max_events,workspace}，
# settrace 记录工作区内帧的逐行局部变量，把结果 JSON 打在 _SENTINEL 之后。
_HARNESS = r'''
import sys, json, os, io, traceback, linecache
from contextlib import redirect_stdout, redirect_stderr

cfg = json.loads(sys.stdin.read())
code = cfg["code"]; target = (cfg.get("target") or "").strip()
max_events = int(cfg.get("max_events") or 200)
ws = os.path.abspath(cfg["workspace"])
SENT = cfg["sentinel"]

events = []
prev_locals = {}

def _short(v):
    try:
        r = repr(v)
    except Exception:
        try:
            r = "<%s 实例，repr 失败>" % type(v).__name__
        except Exception:
            r = "<?>"
    return r if len(r) <= 200 else r[:200] + "…"

def in_scope(frame):
    fn = frame.f_code.co_filename
    try:
        absfn = os.path.abspath(fn)
    except Exception:
        return False
    if not absfn.startswith(ws):
        return False
    if "site-packages" in absfn or os.sep + "lib" + os.sep in absfn:
        return False
    if target:
        return target in frame.f_code.co_name or target in absfn
    return True

def _capture_locals(frame):
    out = {}
    for k, v in list(frame.f_locals.items()):
        if k.startswith("__"):
            continue
        out[k] = _short(v)
    return out

def tracer(frame, event, arg):
    if event == "call":
        return tracer if in_scope(frame) else None
    if len(events) >= max_events:
        return tracer
    if event == "line" and in_scope(frame):
        fid = id(frame)
        cur = _capture_locals(frame)
        prev = prev_locals.get(fid, {})
        changed = {k: val for k, val in cur.items() if prev.get(k) != val}
        prev_locals[fid] = cur
        ln = frame.f_lineno
        src = linecache.getline(frame.f_code.co_filename, ln).strip()
        events.append({"func": frame.f_code.co_name, "line": ln,
                       "src": src, "changed": changed})
    elif event == "return" and in_scope(frame):
        events.append({"func": frame.f_code.co_name, "ret": _short(arg)})
    return tracer

buf = io.StringIO()
err = None
sys.settrace(tracer)
try:
    with redirect_stdout(buf), redirect_stderr(buf):
        exec(compile(code, "<trace-driver>", "exec"), {"__name__": "__main__"})
except Exception:
    err = traceback.format_exc()
finally:
    sys.settrace(None)

result = {"events": events, "stdout": buf.getvalue()[:4000],
          "error": err, "capped": len(events) >= max_events}
sys.stdout.write(SENT + "\n" + json.dumps(result))
'''


def _short(v: str, limit: int = 200) -> str:
    return v if len(v) <= limit else v[:limit] + "…"


def format_trace(result: dict, target: str = "") -> str:
    """把 harness 回传的结果组织成可读轨迹（纯逻辑）。"""
    events = result.get("events") or []
    lines: list[str] = []
    head = "🔬 运行时值追踪（FR-13.D）"
    if target:
        head += f"｜聚焦：{target}"
    lines.append(head)
    lines.append("（每步 = 到达该行时的局部变量变化；ret = 函数返回值。源码未改、子进程跑完即还原。）")
    if not events:
        lines.append("— 未记录到工作区内的执行步骤（检查 target 是否匹配、code 是否真的调到了目标函数）。")
    for e in events:
        if "ret" in e:
            lines.append(f"  ↵ {e['func']} 返回 = {e['ret']}")
            continue
        changed = e.get("changed") or {}
        kv = "  ".join(f"{k}={v}" for k, v in changed.items())
        loc = f"{e['func']}:{e['line']}"
        src = e.get("src", "")
        lines.append(f"  {loc}  {src}" + (f"    ⟶ {kv}" if kv else ""))
    if result.get("capped"):
        lines.append("  …（步骤数达上限被截断，可调小范围或用 target 聚焦）")
    out = result.get("stdout")
    if out:
        lines.append("— 程序输出 stdout —\n" + _short(out, 2000))
    if result.get("error"):
        lines.append("— 抛出异常（上方为崩溃前的轨迹，含定位线索）—\n" + result["error"])
    return "\n".join(lines)


class TraceRunTool(Tool):
    dangerous = True  # 执行任意驱动代码，过权限 gate（同 shell 风险级）
    name = "trace_run"
    description = (
        "运行时值追踪：给一段驱动代码（import 目标函数 + 用具体输入调用它），"
        "在子进程里记录**工作区内函数每一步的局部变量与返回值**并回传——用于 debug 时"
        "**直接看到中间数值**（哪一步算出了什么），而不是盲改瞎猜。源码不被改动。"
        "可选 target 聚焦某个函数名或文件。适合：定位算错的值、看分支为何没进、确认入参。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "驱动代码：import 被测模块/函数并用具体输入调用它（相对工作区根/src 可直接 import）。"
                               "例：`from calc import yield_rate\\nprint(yield_rate(100, 3))`",
            },
            "target": {
                "type": "string",
                "description": "可选聚焦：只追踪函数名或文件路径含此子串的帧（缩小噪声）。省略=工作区内所有帧。",
            },
            "max_events": {
                "type": "integer",
                "description": "最多记录多少步（默认 200，防爆量）。",
            },
        },
        "required": ["code"],
    }

    def run(self, params: dict) -> str:
        code = (params.get("code") or "").strip()
        if not code:
            raise ToolError("code 不能为空：给一段 import 目标并用具体输入调用它的驱动代码。")
        target = (params.get("target") or "").strip()
        payload = {
            "code": code,
            "target": target,
            "max_events": params.get("max_events") or 200,
            "workspace": str(self.workspace),
            "sentinel": _SENTINEL,
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _HARNESS],
                input=json.dumps(payload),
                cwd=str(self.workspace),
                env=_test_env(self.workspace),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=TRACE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"追踪超时（>{TRACE_TIMEOUT}s）。可能驱动代码里有长循环/等待，缩小输入或用 target 聚焦。")
        except OSError as e:
            raise ToolError(f"无法启动追踪子进程：{e}")
        out = proc.stdout or ""
        if _SENTINEL not in out:
            # harness 自身没跑起来（极少见）：把原始 stderr 回灌帮助定位
            detail = (proc.stderr or out or "").strip()[-800:]
            raise ToolError("追踪 harness 未产出结果（可能驱动代码有导入/语法问题）：\n" + detail)
        raw = out.split(_SENTINEL, 1)[1].strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise ToolError("追踪结果解析失败。原始输出尾部：\n" + raw[-800:])
        return format_trace(result, target)
