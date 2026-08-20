"""无头评测 harness（FR-11.0）：不起 GUI，直接驱动 hermes-dev 内核跑真实任务。

- 权限 gate 预置「本会话全部允许」（等价用户点 allow_all）；
- `Api._emit` 替换为事件收集器（无 GUI 时 evaluate_js 不可用）；
- shell 按平台自适应（Windows=powershell / 其它=bash）；存储用临时库，不碰仓库 data/；
- **无人值守**：ask_user / request_handoff 这两座阻塞桥在无 GUI 时会永久卡死，故预置放行/有限等待；
- `world`：装一组桩工具替代真实联网世界（V2 批 2），让联网侧 detector 可离线、可回放；
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


# 无人值守下换手最多等这么久再收成"阻塞"（默认 600s 是给真人留的余量，评测里等于挂死）
HANDOFF_WAIT_S = 5.0


def _unblock(conv) -> None:
    """解掉两座**会永久卡死无头评测**的阻塞桥。

    `ask_user` 与 `request_handoff` 都是「emit 给前端 + 阻塞等 resolve」——无 GUI 时永远没人
    resolve。平时没暴露，是因为此前的任务没有一个会走到那里；而 `login_hint` 的注入文案
    **点名要求** ask_user（"提示用户在弹出的浏览器里登录"），批 2 一上来就会踩中。
    处置照搬 crazy 模式的无人值守语义：ask_user 按合理默认放行；换手**不放行**，
    只把"一直等"改成有限等待、超时收成阻塞态（ADR 0023 决策 2）。
    """
    conv._ask.set_auto(True)
    conv._handoff.set_unattended(True)
    conv._handoff._wait = HANDOFF_WAIT_S


def _install_world(api, conv, world: str) -> None:
    """把桩世界的工具装进注册表（V2 批 2）。

    走 `res.mcp_tools` 这条既有通路而不是直接改注册表：MCP 工具本来就是「外部世界接进来的
    工具」，浏览器穿透在真跑里也正是这么进来的（`<server>__<tool>` 命名、默认过 gate），
    连 `Api.get_browser_mcp_status` 都按这个口径认。走同一条路，桩与真跑的形态才一致。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from world import build_world

    api.res.mcp_tools = list(api.res.mcp_tools or []) + build_world(world)
    conv._build_registry()      # 重建注册表，让桩工具进 schema 与 browser_present 判定


def _deny_tools(conv, deny: "tuple | list") -> None:
    """把指定工具从注册表里摘掉（桩世界封边界用）。

    `"shell"` 解析成当前平台的 `run_bash` / `run_powershell`——任务定义不该写死平台名。
    摘不到就**抛错**：静默摘不掉等于以为封住了其实没封，比不封更坏。
    """
    names = set(conv.registry.names())
    drop = set()
    for d in deny:
        if d == "shell":
            hit = {n for n in names if n in ("run_bash", "run_powershell")}
        else:
            hit = {n for n in names if n == d}
        if not hit:
            raise RuntimeError(f"deny_tools：注册表里没有 {d!r}，无法摘除（names={sorted(names)}）")
        drop |= hit
    conv.registry = conv.registry.filtered(lambda n: n not in drop)


def run_task(workspace: str, prompt: str, *, model: "str | None" = None,
             verbose: bool = True, db_path: "str | None" = None,
             failure_db: "str | None" = None,
             max_steps: int = 0, max_tokens: int = 0, world: str = "", firecrawl: str = "",
             deny_tools: "tuple | list" = (), autonomous: bool = False,
             crazy_rounds: int = 0, crazy_seconds: int = 0) -> EvalResult:
    """在指定工作区无头跑一轮任务，返回 EvalResult。

    `failure_db`：死路记忆库路径。**默认每跑一个独立库**（落在临时工作区旁，随之销毁）。

    这一条是块 V3 录制回放时揪出来的：死路提示的文案里嵌着**跨会话累计次数**
    （「这条路已累计 N 次失败」），它会进消息历史。共用一个库时 N 每跑都在涨，于是
    ①cassette 的请求指纹每跑都变、回放永远 miss；②反例任务会随语料增长**逐渐开始误报**
    （新一跑的第一次失败就撞上"已知死路"），基线一路漂。
    要为块 V4 攒语料就显式传共享库（`run_eval.py --accumulate`）。

    `max_tokens`：主模型单次输出上限覆盖（0=跟随模型档）。撞 max_tokens 的转向指令
    （`truncation_hint`）只有把上限压到任务装不下时才谈得上触发。

    `world`：世界夹具名（见 world.py）。非空则**拔掉真的 web_search/web_fetch**、换上桩工具。
    真网结果每跑一变会污染 cassette 的请求指纹，联网侧要进回放门就必须把世界侧也定死。

    `deny_tools`：本任务摘掉的工具（`"shell"` 解析成当前平台的 run_bash/run_powershell）。
    桩世界必须连 shell 一起封——否则模型可以 `curl` 出去，桩就形同虚设。

    `autonomous`：走 **crazy 外层目标循环**（`run_autonomous`）而不是单轮 `send_message`（L3 用）。
    `crazy_rounds` / `crazy_seconds` 把无人值守护栏压到评测尺度（默认 20 轮 / 1 小时对评测太宽）。
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
    # 改完文件自动跑的定向测试走 pytest，输出带**耗时**（`in 0.03s`）——它会回灌进
    # 消息历史，让 cassette 的请求指纹偶发漂移、回放门变成 flaky（块 V3 踩到）。
    # 该功能本身有独立单测（tests/test_affected_tests.py），关掉不损失覆盖面。
    cfg.agent.auto_affected_test = False
    cfg.agent.screenshot = False
    if firecrawl:
        # 托管检索源三档（FR-11.1d）。**只对真网任务有意义**：桩世界会把 web.enabled 关掉，
        # 那条链路上 Firecrawl 根本不参与。落进 Run Record 的 web 快照，两轮才可比。
        cfg.web.firecrawl = firecrawl
    if max_steps > 0:
        cfg.agent.max_steps = max_steps
    if max_tokens > 0:
        cfg.agent.model_max_tokens = max_tokens
    if world:
        # 桩世界接管联网：真工具留着就会真的去 bing/ddg，结果每跑一变（且桩与真同名会撞）
        cfg.web.enabled = False
    if autonomous:
        # **必须关掉自适应过门**：`_crazy_gate_ask` 会显式 `set_auto(False)` 再阻塞等真人回答
        # （撞设计岔路 need_user、或验收连败时），无头评测没人回答 → 整跑挂死。
        # 关掉后走的是它自己文档写明的另一条路："gate_ask=False 时只按预算兜"。
        cfg.agent.crazy_gate_ask = False
        if crazy_rounds > 0:
            cfg.agent.crazy_max_rounds = crazy_rounds
        if crazy_seconds > 0:
            cfg.agent.crazy_max_seconds = crazy_seconds
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
        _unblock(conv)
        if world:
            _install_world(api, conv, world)
        if deny_tools:
            _deny_tools(conv, deny_tools)
        if autonomous:
            # crazy 外层循环：自己写 GOAL → 反复跑 agent + 自评 → 达成或触护栏停。
            # 它**永远返回 ok=True**（"跑完了"不等于"达成了"），达成与否看 reason/终局产物——
            # 判分只认终局可程序化事实，正合 L3 的判据口径。
            ret = conv.run_autonomous(prompt, max_rounds=crazy_rounds or None)
        else:
            ret = conv.send_message(prompt)   # 同步跑完一轮（含多步工具）
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
