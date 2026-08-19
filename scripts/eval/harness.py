"""无头评测 harness（FR-11.0）：不起 GUI，直接驱动 hermes-dev 内核跑真实任务。

- 权限 gate 预置「本会话全部允许」（等价用户点 allow_all）；
- `Api._emit` 替换为事件收集器（无 GUI 时 evaluate_js 不可用）；
- shell 按平台自适应（Windows=powershell / 其它=bash）；存储用临时库，不碰仓库 data/；
- 返回 EvalResult：事件流 / 主对话全文 / 耗时 / 工具与子任务计数，供判分器使用。

真跑需要网络与模型 key（读项目根 .env）；判分逻辑本身可离线自检（见 tests/test_eval.py）。
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


@dataclass
class EvalResult:
    """一次任务运行的产物（判分器的输入）。"""
    ok: bool = False
    answer: str = ""                      # 主对话全部文本输出（含中间回合）
    events: list = field(default_factory=list)   # [(event, data), ...]
    elapsed: float = 0.0
    tool_calls: int = 0
    subagents: int = 0
    error: str = ""
    # 本次**实际生效**的配置对象（harness 会关掉 memory/mcp/截屏等）。
    # 落 Run Record 时必须用它做快照——外面重新 load_config() 拿到的是没被改过的那份，
    # 记下来就是**说谎**（ADR 0027 决策 3 的可比性三件套之一）。
    cfg: object = None

    def count(self, event: str) -> int:
        return sum(1 for e, _ in self.events if e == event)


def run_task(workspace: str, prompt: str, *, model: "str | None" = None,
             verbose: bool = True, db_path: "str | None" = None,
             failure_db: "str | None" = None) -> EvalResult:
    """在指定工作区无头跑一轮任务，返回 EvalResult。

    `failure_db`：死路记忆库路径。**默认每跑一个独立库**（落在临时工作区旁，随之销毁）。

    这一条是块 V3 录制回放时揪出来的：死路提示的文案里嵌着**跨会话累计次数**
    （「这条路已累计 N 次失败」），它会进消息历史。共用一个库时 N 每跑都在涨，于是
    ①cassette 的请求指纹每跑都变、回放永远 miss；②反例任务会随语料增长**逐渐开始误报**
    （新一跑的第一次失败就撞上"已知死路"），基线一路漂。
    要为块 V4 攒语料就显式传共享库（`run_eval.py --accumulate`）。
    """
    from agentcore.bridge.api import Api
    from agentcore.config import load_config

    cwd = os.getcwd()
    os.chdir(ROOT)  # load_config 读项目根 config.yaml / .env
    try:
        cfg = load_config()
    finally:
        os.chdir(cwd)
    cfg.agent.workspace = str(workspace)      # 固定工作区（关闭按会话隔离）
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.auto_conventions = False        # 评测不要后台生成规范（省一次模型调用）
    cfg.agent.screenshot = False
    cfg.memory.enabled = False
    cfg.mcp.enabled = False
    cfg.storage.db_path = db_path or str(Path(workspace).parent / "eval.db")
    # 死路语料入**独立库**（ADR 0027 决策 2）：评测要能清空重跑，
    # 绝不能碰真实使用积累的 data/failures.db（那是跨会话资产）。source 一并标为 eval。
    cfg.agent.failure_memory_db = failure_db or str(
        Path(workspace).parent / "failures.eval.db")
    if model:
        cfg.active_model = model

    res = EvalResult()
    res.cfg = cfg          # 供 record.build_record 取真实生效配置
    chunks: list[str] = []

    def fake_emit(self, event, data, cid=None):  # noqa: ANN001 — 替代 Api._emit
        res.events.append((event, data))
        if event == "chunk":
            chunks.append(data)
            return
        if not verbose:
            return
        if event == "tool_use":
            print(f"  [工具] {data['name']} <- {str(data['input'])[:120]}", flush=True)
        elif event == "subagent_start":
            print(f"  [子#{data['id']}] role={data['role']} {str(data['task'])[:60]}", flush=True)
        elif event == "subagent_done":
            print(f"  [子#{data['id']} 完成] ok={data['ok']}", flush=True)
        elif event in ("error", "stopped"):
            print(f"  [{event}] {data}", flush=True)

    orig_emit = Api._emit
    Api._emit = fake_emit
    api = None
    t0 = time.time()
    try:
        api = Api(cfg)
        conv = api.active
        conv.gate._allow_all = True           # 等价用户点「本会话全部允许」
        ret = conv.send_message(prompt)       # 同步跑完一轮（含多步工具）
        res.ok = bool(ret.get("ok"))
        if not res.ok:
            res.error = str(ret.get("error", ""))
    except Exception as e:  # noqa: BLE001 — 评测失败也要出结果
        res.error = f"{type(e).__name__}: {e}"
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass
        Api._emit = orig_emit
    res.elapsed = time.time() - t0
    res.answer = "".join(chunks)
    res.tool_calls = res.count("tool_use")
    res.subagents = res.count("subagent_start")
    if res.count("error") and not res.error:
        res.error = next(str(d) for e, d in res.events if e == "error")
    return res
