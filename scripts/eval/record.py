"""V1 Run Record（ADR 0027 决策 3）：把一次评测跑落成可复查、可对比的记录。

**为什么要有它**：`EvalResult` 原先只活在内存里，`run_eval.py` 打印完就没了——于是两次跑
无法对比，任何改动的"提升"都只能靠"感觉更好了"。而 `loop.py` 其实**早就把该有的遥测全 emit 了**
（八种 nudge 事件 + 每条工具结果附的 `eval.error_classes` + 回合末 `usage`），只是没人接。
本模块就是那个"接"。

**可比性三件套**（缺一份记录就不可比，ADR 0025 决策 3「一个自信的错数比缺失更危险」同源）：
git sha（被测代码是哪一版）+ 真实 model_id（档名会漂）+ 配置快照（影响行为的 agent.* 全量）。

纯逻辑（`summarize_events` / `config_snapshot` / `build_record`）与 IO（`git_sha` / 读写盘）
分离，故判分口径可脱离模型与网络单测（见 `tests/test_eval_record.py`）。
"""
from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from pathlib import Path

# loop.py 里"情境自启"注入的八种事件。**这是 V5 调阈值的唯一输入**——
# 每一次 nudge 触发都在这儿留痕，才谈得上算触发率/误报率。
NUDGE_EVENTS = (
    "login_hint",        # 撞登录墙
    "stuck_hint",        # 同一文件反复改仍失败
    "search_hint",       # 该用 search_code 却在浏览
    "deadend_hint",      # 同一条路反复非瞬时失败（块E）
    "research_hint",     # 搜索不达标 / 不对题 → 催重搜（块H）
    "truncation_hint",   # 撞 max_tokens 截断 → 劝分块写
    "learning_shadow",   # 块G 影子建议（只记不改路）
    "learning_advice",   # 块G 已生效策略注入
)

# **真正会插话的**那几种——即会往 `inject_blocks` 塞文本、被模型看见的。
# `learning_shadow` 不在此列：`loop.py` 只把 `learning_advice` append 进 inject_blocks，
# shadow 纯观测、模型看不见。**只有插话的才谈得上"误报"**——
# 把纯观测事件也当误报，会让任何一次正常失败都被误判（V2 端到端压测时踩到）。
INJECTING_NUDGES = tuple(n for n in NUDGE_EVENTS if n != "learning_shadow")

# 每跑都不同的字段：留在快照里会让两份记录**永远**判为"配置不同"，从而掩盖真正的差异。
_VOLATILE_CONFIG_KEYS = ("workspace", "failure_memory_db", "permissions")


def summarize_events(events) -> dict:
    """从事件流里提炼一次跑的全部可比指标。**纯函数**，输入 `[(event, data), ...]`。

    `usage` 事件**每个 AgentLoop.run 发一次**（主 Agent + 每个子 Agent 各一），故 token 与步数
    是**跨 agent 合计**；`usage_events` 记合计了几份，避免把"子任务多"误读成"主线步数多"。
    """
    tool_names: Counter = Counter()
    error_classes: Counter = Counter()
    nudges = {n: 0 for n in NUDGE_EVENTS}
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    steps = usage_events = subagent_failed = 0
    max_steps = 0
    measured = True
    model = provider = None

    for name, data in events:
        d = data if isinstance(data, dict) else {}
        if name in nudges:
            nudges[name] += 1
        elif name == "tool_use":
            tool_names[str(d.get("name") or "?")] += 1
        elif name == "tool_result":
            for c in (d.get("eval") or {}).get("error_classes") or []:
                error_classes[str(c)] += 1
        elif name == "subagent_done" and not d.get("ok", True):
            subagent_failed += 1
        elif name == "usage":
            usage_events += 1
            for k in tokens:
                tokens[k] += int(d.get(k) or 0)
            steps += int(d.get("steps") or 0)
            max_steps = max(max_steps, int(d.get("max_steps") or 0))
            measured = measured and bool(d.get("measured", True))
            model = d.get("model") or model
            provider = d.get("provider") or provider

    counts = Counter(n for n, _ in events)
    return {
        "steps": steps,
        "max_steps": max_steps,
        "usage_events": usage_events,
        "tool_calls": counts.get("tool_use", 0),
        "tool_retries": counts.get("tool_retry", 0),
        "subagents": counts.get("subagent_start", 0),
        "subagent_failed": subagent_failed,
        "errors": counts.get("error", 0),
        "step_warnings": counts.get("step_warning", 0),
        "tools": dict(tool_names),
        "error_classes": dict(error_classes),
        "nudges": nudges,
        "nudges_total": sum(nudges.values()),
        "tokens": tokens,
        # 端点没回传用量时 loop 会改用估算并标 measured=False——**如实标记**，
        # 别让估算数字冒充实测（ADR 0025 决策 3）。
        "tokens_measured": measured,
        "model": model,
        "provider": provider,
    }


def config_snapshot(cfg) -> dict:
    """影响行为的配置快照。**纯函数**（只读 cfg）。

    整份 `agent.*` 全量落下、不手工列白名单——手工列表必然与新增字段漂移，
    而漂移的后果是"两份记录看着可比、实际不可比"。剔掉的只有每跑必变的临时路径。

    **`web.*` 同样全量落下**（FR-11.1d 起）：检索链路的旋钮（引擎、宽召回、读正文条数、
    托管源三档）直接改变解题过程，而此前它们一个都不在快照里——拿 `firecrawl=fallback`
    与 `always` 两轮记录做对比，配置栏会显示"完全相同"，正是本模块开头那条纪律要防的。
    """
    def _dump(section):
        return section.model_dump() if hasattr(section, "model_dump") else dict(vars(section))

    agent = _dump(cfg.agent)
    for k in _VOLATILE_CONFIG_KEYS:
        agent.pop(k, None)
    return {"active_model": getattr(cfg, "active_model", None), "agent": agent,
            "web": _dump(cfg.web)}


def model_identity(cfg, model_name=None) -> dict:
    """档名 + **真实 model_id**。档名可以随便起、也会被改，按档名对比等于没对比。"""
    name = model_name or getattr(cfg, "active_model", None)
    out = {"profile": name, "model_id": None, "provider": None}
    try:
        mc = cfg.get_model(name)
        out["model_id"] = getattr(mc, "model", None)
        out["provider"] = getattr(mc, "provider", None)
    except Exception:  # noqa: BLE001 — 取不到不致命，但要留 None 而不是编一个
        pass
    return out


def build_record(*, task, title, prompt, passed, why, result, cfg,
                 model_name=None, git=None, tag="", started_at=None,
                 tier="L1", nudge_check=None) -> dict:
    """组装一条 Run Record。**纯函数**（不碰盘、不调 git）。"""
    return {
        "schema": 1,
        "task": task,
        "tier": tier,
        "title": title,
        "prompt": prompt,
        "passed": bool(passed),
        "why": why,
        "elapsed": round(float(getattr(result, "elapsed", 0.0)), 3),
        "error": getattr(result, "error", "") or "",
        "answer_chars": len(getattr(result, "answer", "") or ""),
        "metrics": summarize_events(getattr(result, "events", []) or []),
        # nudge 期望核验（V2）：{"ok": 硬断言是否全过, "why": 说明}。
        # 硬断言不过 = 误报，任务直接判 FAIL；软观测只留痕不影响判定。
        "nudge_check": nudge_check or {},
        # ---- 可比性三件套 ----
        "git": git or {},
        "model": model_identity(cfg, model_name),
        "config": config_snapshot(cfg),
        "tag": tag,
        "started_at": started_at if started_at is not None else time.time(),
    }


# ---- 受控 IO ---------------------------------------------------------------

def git_sha(root) -> dict:
    """被测代码是哪一版。取不到就如实留空，**不编造**。"""
    out = {"sha": None, "dirty": None, "branch": None}
    try:
        run = lambda *a: subprocess.run(a, cwd=str(root), capture_output=True,  # noqa: E731
                                        text=True, timeout=10)
        r = run("git", "rev-parse", "HEAD")
        if r.returncode == 0:
            out["sha"] = r.stdout.strip()[:12]
        r = run("git", "status", "--porcelain")
        if r.returncode == 0:
            out["dirty"] = bool(r.stdout.strip())
        r = run("git", "rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode == 0:
            out["branch"] = r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return out


def new_run_id(tag: str = "") -> str:
    """`20260819-140355` 或带标签 `20260819-140355_baseline`。可排序、肉眼可读。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in (tag or "") if c.isalnum() or c in "-_")
    return f"{stamp}_{safe}" if safe else stamp


def write_record(run_dir, record: dict) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    # 一次 --repeat 里同名任务跑多遍：按序号分文件，别互相覆盖
    base = record["task"]
    i, path = 1, run_dir / f"{base}.json"
    while path.exists():
        i += 1
        path = run_dir / f"{base}.{i}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_run(run_dir) -> list:
    """读回一次跑的全部记录（按文件名排序，稳定）。"""
    run_dir = Path(run_dir)
    out = []
    for p in sorted(run_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — 单条坏了不该毁掉整份报告
            continue
    return out


def runs_root(root) -> Path:
    return Path(root) / "data" / "eval_runs"
