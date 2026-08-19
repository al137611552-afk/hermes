"""模型响应的录制 / 回放（ADR 0027 决策 4，块 V3）。

**要解决的问题**：评测跑一次要真模型、要网络、要 key，于是既进不了 CI，也没法在改一个
detector 阈值之后"在**同样的模型输出**下"对比行为差异——模型随机性会把改动效果淹掉。

**做法**：在 `build_provider()` **外面**包一层，不动任何 provider 实现。
- `record`：真跑一遍，把 (请求指纹 → StreamEvent 序列) 落成 cassette；
- `replay`：离线按指纹取回放，**完全不构造真 provider、不取 key、不连网**。

**miss 必须硬报错**（`CassetteMiss`），绝不静默回落真跑——静默回落会同时犯两个错：
偷偷烧 key，以及把"我的改动让轨迹发散了"这个**最有价值的信号**当噪声吞掉。

**已知限制（写进 ADR，实现时别试图绕）**：改 system prompt、改任何 nudge 注入文案 →
对话轨迹发散 → cassette 全 miss，必须重录。所以 **replay 是回归门，不是 A/B 工具**；
提示词类改动的效果验证只能靠真跑 × N 次重复。

开关走**环境变量**而非 config：它是测试/CI 设施、不是用户旋钮，且必须对主 Agent 与所有
子 Agent 一致生效——环境变量是最简单的统一通道。默认不设 = 完全关闭、零行为改动。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .base import BaseProvider, StreamEvent, ToolCall

MODE_ENV = "HERMES_CASSETTE_MODE"   # record | replay；其余值/未设 = 关
DIR_ENV = "HERMES_CASSETTE_DIR"
# 工作区绝对路径：**工具输出会回灌进消息历史**（pytest 的 `rootdir: /tmp/tmpXXXX/ws`、
# 报错里的文件路径…），而临时工作区每跑都不同 → 消息历史不同 → key 不同 → 全 miss。
# 与块 V0 的死路指纹是同一个病、同一个药：把工作区路径折成占位符。
WS_ENV = "HERMES_CASSETTE_WS"


class CassetteMiss(RuntimeError):
    """回放时没有对应录音。**故意是硬错误**——见模块注释。"""


# ---- 纯逻辑：请求指纹 --------------------------------------------------------

def _canon_block(b):
    """图片块用**内容哈希**代替 base64：同一张图不同编码不该分裂成两个 key，
    而且原样存进 cassette 会让文件大到不可读、不可 diff。"""
    if not isinstance(b, dict):
        return b
    if b.get("type") == "image":
        src = b.get("source") or {}
        data = str(src.get("data") or "")
        return {"type": "image", "media_type": src.get("media_type"),
                "sha1": hashlib.sha1(data.encode("utf-8", "replace")).hexdigest()[:16]}
    return b


def _canon_content(content):
    if isinstance(content, list):
        return [_canon_block(b) for b in content]
    return content


def request_key(model: str, system, messages, tools) -> str:
    """一次模型请求的稳定指纹 = 模型 + system + 消息 + 工具 schema。

    **不含 max_tokens/temperature**：按 ADR 决策 4 的口径。它们由配置决定，
    已经进了 Run Record 的配置快照；放进 key 只会让录音更易失效。
    """
    payload = {
        "model": model or "",
        "system": system or "",
        "messages": [{"role": getattr(m, "role", None),
                      "content": _canon_content(getattr(m, "content", None))}
                     for m in (messages or [])],
        "tools": tools or [],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    raw = fold_workspace(raw)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:32]


def fold_workspace(text: str, ws: "str | None" = None) -> str:
    """把工作区绝对路径折成 `<ws>`。在**序列化之后**对整串做替换——一次覆盖所有嵌套字符串，
    比逐个字段递归简单得多，也不会漏掉某个新加的字段。

    同时折原样与 `resolve()` 两种形态（macOS `/private/var`、Windows 8.3 短名同源）。
    """
    ws = ws if ws is not None else (os.getenv(WS_ENV) or "")
    if not ws:
        return text
    cands = {str(Path(ws))}
    try:
        cands.add(str(Path(ws).resolve()))
    except OSError:
        pass
    for pre in sorted((c for c in cands if c), key=len, reverse=True):
        text = text.replace(pre, "<ws>")
    return text


def request_digests(model: str, system, messages, tools) -> list:
    """请求各部分的独立指纹。**只为诊断存在**：miss 时能指出"第几条消息变了"，
    而不是只说"key 对不上"——后者得另写脚本 dump 两侧再逐字 diff（V3 实现时干过两轮）。

    与 `request_key` 走同一套归一化（含工作区折叠），否则诊断结论会与真实判定不符。
    """
    def h(obj) -> str:
        raw = fold_workspace(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")))
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]
    out = [f"model:{h(model or '')}", f"system:{h(system or '')}",
           f"tools:{h(tools or [])}"]
    for i, m in enumerate(messages or []):
        out.append(f"msg{i}:{h({'role': getattr(m, 'role', None),
                                'content': _canon_content(getattr(m, 'content', None))})}")
    return out


def explain_miss(store, digests: list) -> str:
    """在已有录音里找最接近的一条，指出**第一处**不同——把"对不上"变成"哪儿对不上"。"""
    best, best_n = None, -1
    for f in sorted(store.root.glob("*.jsonl")) if store.root.is_dir() else []:
        try:
            head = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        except Exception:  # noqa: BLE001
            continue
        old = head.get("digests") or []
        n = 0
        for a, b in zip(old, digests):
            if a != b:
                break
            n += 1
        if n > best_n:
            best, best_n = old, n
    if best is None:
        return "该目录下还没有任何录音。"
    if best_n >= len(digests):
        return "各部分指纹一致但整体 key 不同（不该发生，请报 bug）。"
    part = digests[best_n].split(":", 1)[0]
    was = best[best_n].split(":", 1)[0] if best_n < len(best) else "（录音更短）"
    return (f"最接近的录音在**第 {best_n + 1} 部分**开始不同：本次是 `{part}`、录音是 `{was}`。"
            f"若差在 `msgN`，多半是那条工具结果里带了每跑都变的东西"
            f"（时间戳/哈希/耗时）；若差在 `system`，是提示词改了。")


# ---- 纯逻辑：事件序列化 ------------------------------------------------------

def event_to_dict(ev) -> dict:
    """StreamEvent → 可 JSON 化的 dict。`meta["call"]` 是 ToolCall，要拆开。"""
    d = {"type": ev.type}
    if ev.text:
        d["text"] = ev.text
    meta = dict(ev.meta or {})
    call = meta.pop("call", None)
    if call is not None:
        meta["call"] = {"id": call.id, "name": call.name, "input": call.input}
    if meta:
        d["meta"] = meta
    return d


def event_from_dict(d: dict) -> StreamEvent:
    meta = dict(d.get("meta") or {})
    c = meta.get("call")
    if isinstance(c, dict):
        meta["call"] = ToolCall(id=c.get("id", ""), name=c.get("name", ""),
                                input=c.get("input") or {})
    return StreamEvent(type=d.get("type", "text"), text=d.get("text", ""), meta=meta)


# ---- 受控 IO：cassette 存取 --------------------------------------------------

class CassetteStore:
    """一个目录 = 一组录音。首行是 meta，其后一行一个事件（jsonl，可 diff、可人读）。"""

    def __init__(self, root) -> None:
        self.root = Path(root)

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.jsonl"

    def read(self, key: str):
        p = self.path_for(key)
        if not p.is_file():
            return None
        events = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if i == 0 and obj.get("_meta"):
                continue
            events.append(event_from_dict(obj))
        return events

    def write(self, key: str, events, meta=None) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path_for(key)
        lines = [json.dumps({"_meta": True, **(meta or {})}, ensure_ascii=False, sort_keys=True)]
        lines += [json.dumps(event_to_dict(e), ensure_ascii=False) for e in events]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def count(self) -> int:
        return len(list(self.root.glob("*.jsonl"))) if self.root.is_dir() else 0


# ---- 包装 provider -----------------------------------------------------------
#
# `store/usage.py:provider_kind()` 从**类名**推厂商（`AnthropicProvider` → `anthropic`），
# 且其文档明说"不给 Provider 基类加字段"。所以这里给包装类**动态取与内层同名的类名**，
# 用量台账的厂商归属才不会变成 "recording"/"replay"——录音模式下的记录仍然如实。
_CLASS_CACHE: dict = {}


def _named_subclass(base: type, name: str) -> type:
    k = (base, name)
    if k not in _CLASS_CACHE:
        _CLASS_CACHE[k] = type(name, (base,), {})
    return _CLASS_CACHE[k]


class _Recording(BaseProvider):
    """真跑 + 落录音。流**中途失败不写**（半截录音比没有更坏）。"""

    def __init__(self, inner, store: CassetteStore) -> None:  # noqa: D107 — 不调 super
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_store", store)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        inner, store = self._inner, self._store
        key = request_key(inner.model, system, messages, tools)
        buf = []
        # **必须用 finally 落盘**：`AgentLoop.run` 收到 `done` 事件就 `break`，
        # 生成器被丢弃、for 之后的语句永远不执行（第一版就是这样录出 0 条的）。
        # GeneratorExit 也会走 finally，故提前中断同样能存下已完成的这一轮。
        try:
            for ev in inner.stream_chat(messages, system, tools, max_tokens):
                buf.append(ev)
                yield ev
        finally:
            # 只在**看到 done** 时写：半截录音比没有录音更坏——回放时它会假装那一轮
            # 正常结束，把"当时其实炸了"这个事实抹掉。
            if any(e.type == "done" for e in buf):
                store.write(key, buf, meta={
                    "model": inner.model,
                    "provider_class": type(inner).__name__,
                    "n_messages": len(messages or []),
                    "n_tools": len(tools or []),
                    "digests": request_digests(inner.model, system, messages, tools)})


class _Replay(BaseProvider):
    """离线回放。**不构造真 provider、不取 key、不连网**——CI 里没有 key 也能跑。"""

    def __init__(self, model: str, store: CassetteStore) -> None:  # noqa: D107
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_step", 0)
        object.__setattr__(self, "max_tokens", 0)
        object.__setattr__(self, "prompt_cache", False)

    def stream_chat(self, messages, system=None, tools=None, max_tokens=None):
        object.__setattr__(self, "_step", self._step + 1)
        key = request_key(self.model, system, messages, tools)
        events = self._store.read(key)
        if events is None:
            why = explain_miss(self._store,
                               request_digests(self.model, system, messages, tools))
            raise CassetteMiss(
                f"第 {self._step} 次模型请求没有对应录音（key={key[:12]}…，"
                f"目录 {self._store.root}）。\n{why}\n"
                f"最常见的原因是**改了 system prompt 或某条注入文案**，对话轨迹就此发散——"
                f"这不是 bug，是 replay 的定义域（ADR 0027 已知限制 1）。重录即可：\n"
                f"  python scripts/eval/run_eval.py --record --task <任务名> --model <档名>")
        yield from events


def cassette_mode() -> str:
    m = (os.getenv(MODE_ENV) or "").strip().lower()
    return m if m in ("record", "replay") else ""


def cassette_store() -> "CassetteStore | None":
    d = (os.getenv(DIR_ENV) or "").strip()
    return CassetteStore(d) if d else None


def wrap_recording(inner, store: CassetteStore):
    return _named_subclass(_Recording, type(inner).__name__)(inner, store)


def make_replay(model: str, store: CassetteStore):
    """录音里记了原始 provider 类名，回放时沿用它——两种模式产出的记录才可比。"""
    cls_name = "ReplayProvider"
    for p in sorted(store.root.glob("*.jsonl")) if store.root.is_dir() else []:
        try:
            head = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        except Exception:  # noqa: BLE001
            continue
        if head.get("_meta") and head.get("provider_class"):
            cls_name = head["provider_class"]
            break
    return _named_subclass(_Replay, cls_name)(model, store)
