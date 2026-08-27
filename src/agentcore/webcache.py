"""同回合共享的检索缓存（web_search / web_fetch）。

**它是一道便宜的保险，不是一个已被证实的优化**——这段来历必须写清楚，否则下一个人会照着
错误的理由去扩建它：

2026-08-26 的 A/B 真跑发现并行委派净亏（并发度 2.3~2.6× < 模型工作量膨胀 3.3~4.5×）。
当时**从"子工具调用 41 次 vs 主模型自己搜 19 次"推断**膨胀来自"子 Agent 互不知情、
重复搜同一片领域"，于是有了本模块。**后来把查询词打出来，证据不支持那个推断**：
两道题里跨子 Agent 的**逐字重复是 0 次**，近似重复也只有 1 对（0.45，还是同一个子 Agent
自己跟自己）。三个 researcher 的分工其实很干净（速度基准 / 依赖解析 / 生态替代）。
真正的膨胀是**结构性**的：每个子 Agent 都要跑一遍完整的 agent 循环，外加汇总那一轮。

所以本模块的定位是：**真出现重复时顺手省掉，不出现时是零开销的 no-op**（不命中就是一次
字典查找）。别把它当成"治膨胀"的手段去加码——尤其别为了提高命中率去做模糊匹配（见 `search_key`）。

设计要点：
- **全会话共享一个实例**（主 Agent 与所有子 Agent 传同一个），照 `budget.ToolBudget` 的先例；
  各拿一份等于没有缓存。
- **按回合清空**：跨回合复用会把"再搜一下最新的"变成拿旧结果糊弄——陈旧比多搜一次危险得多。
- **单飞**：同一个 key 只放一个人出网，其余的等它；等不到（超时/leader 失败）就自己跑，
  **绝不把"等"变成新的挂死点**。

纯逻辑 + 一把锁，无 IO，可脱离网络单测。
"""
from __future__ import annotations

import threading
from typing import Callable

# 单条结果的字节上限：web_fetch 的 max_chars 硬上限是 100k 字符，缓存住整页很正常；
# 但真离谱的（拼了大量正文摘录的搜索结果）不值得占着内存，跳过缓存即可（不是错误）。
DEFAULT_MAX_ENTRIES = 64
DEFAULT_MAX_BYTES = 4_000_000
DEFAULT_WAIT = 90.0    # 等 leader 的上限（秒）：略大于一次检索的常见耗时，超了就自己跑


def search_key(query: str, n: int) -> tuple:
    """web_search 的缓存键：查询词归一化（去首尾空白、压缩内部空白、小写）+ 条数。

    **只做归一化、不做模糊匹配**：把"意思差不多"的两个查询判成同一个，省下的那次搜索
    换来的是模型拿到答非所问的结果——它还查不出为什么。宁可少命中。

    真跑实测过命中率：两道题、9 次与 3 次搜索，**逐字重复 0 次**（模块注释里那段）。
    看到"命中率是 0，把键放宽一点吧"这个念头时先想清楚：放宽的代价是**静默给错答案**，
    而省下的是一次搜索。这笔账不划算，0 命中是可接受的结果。
    """
    return ("search", " ".join((query or "").split()).lower(), int(n))


def fetch_key(url: str, focus: str, cap: int) -> tuple:
    """web_fetch 的缓存键。**focus 与 cap 都进键**：同一页给不同 focus 摘出来的是不同片段，
    合并成一个键就会把 A 要的段落当成 B 的答案给出去。"""
    return ("fetch", (url or "").strip(), " ".join((focus or "").split()).lower(), int(cap))


class RetrievalCache:
    """同回合内按 key 复用检索结果，并对同 key 的并发调用做单飞。

    线程安全：并行委派时多个子 Agent 线程同时进出。
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 wait_timeout: float = DEFAULT_WAIT) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_bytes = max(1, int(max_bytes))
        self._wait = max(0.0, float(wait_timeout))
        self._lock = threading.Lock()
        self._data: "dict[tuple, str]" = {}      # 插入序即淘汰序（Python dict 保序）
        self._bytes = 0
        self._inflight: "dict[tuple, threading.Event]" = {}
        self.hits = 0        # 观测用（诊断脚本/测试断言）
        self.misses = 0

    # ---- 回合边界 -------------------------------------------------------
    def new_turn(self) -> None:
        """新回合开始：清空。**不动在飞的 key**——上一回合还没跑完的调用（停止后仍在收尾的
        网络往返）set 事件时会去 pop，留着它自己收拾，这里清了只会让它 pop 到空。"""
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "bytes": self._bytes,
                    "hits": self.hits, "misses": self.misses}

    # ---- 主入口 ---------------------------------------------------------
    def get_or_call(self, key: tuple, produce: Callable[[], str]) -> "tuple[str, bool]":
        """返回 (结果, 是否命中缓存)。未命中时调用 produce() 真跑一次并缓存。

        produce 抛异常＝**不缓存**：网络抽风是瞬时的，把失败钉在整个回合里，
        会让模型第二次试同一条路时拿到一条它无法理解的旧错误。
        """
        while True:
            with self._lock:
                if key in self._data:
                    self.hits += 1
                    return self._data[key], True
                ev = self._inflight.get(key)
                if ev is None:
                    ev = threading.Event()
                    self._inflight[key] = ev
                    self.misses += 1
                    leader = True
                else:
                    leader = False
            if leader:
                return self._produce_as_leader(key, ev, produce), False
            # 跟随者：等 leader 出结果。
            if not ev.wait(self._wait):
                break          # 等超时：别再等下去，自己跑（宁可多跑一次，不留挂死点）
            with self._lock:
                if key in self._data:
                    self.hits += 1
                    return self._data[key], True
                if self._inflight.get(key) is ev:
                    break      # 事件已 set 却还挂在飞（不该发生）：自己跑，别转圈
            # leader 失败并已清场 → 回到循环顶：要么自己当新 leader，要么等接棒的那个。
            # 每轮要么终止、要么等一个**全新**的事件，跟随者数量有限，不会死循环。
        return produce(), False     # 自己跑，且**不占 leader 位**（避免连锁等待）

    def _produce_as_leader(self, key: tuple, ev: threading.Event,
                           produce: Callable[[], str]) -> str:
        try:
            value = produce()
        except BaseException:
            with self._lock:
                if self._inflight.get(key) is ev:
                    del self._inflight[key]
            ev.set()          # **先清场再唤醒**：跟随者醒来看到没缓存也没在飞，就自己跑
            raise
        with self._lock:
            self._put(key, value)
            if self._inflight.get(key) is ev:
                del self._inflight[key]
        ev.set()              # 唤醒必须在写入之后，否则跟随者醒来时缓存还是空的
        return value

    def _put(self, key: tuple, value: str) -> None:
        """写缓存（调用方持锁）。超大单条直接不存——它一条就能顶满预算。"""
        if not isinstance(value, str):
            return
        size = len(value)
        if size > self._max_bytes:
            return
        self._data[key] = value
        self._bytes += size
        while self._data and (len(self._data) > self._max_entries or self._bytes > self._max_bytes):
            old_key = next(iter(self._data))
            self._bytes -= len(self._data.pop(old_key))


# 命中时贴在结果最前面的说明。**必须说出来**：模型不知道这是复用的，就会以为"搜了两次都这样"，
# 继续拿同样的关键词打转；说清楚了它才会换角度——这条提示本身也是在治"重复搜同一片领域"。
SEARCH_HIT_NOTE = (
    "[缓存命中] 本回合内已经用同样的关键词搜过（可能是并行的另一个子任务搜的），"
    "下面是那次的结果，本次未再出网。**别再用同样或高度近似的关键词重搜**——"
    "换个角度、换更具体的问法，或直接 web_fetch 读上面某条的正文。"
)
FETCH_HIT_NOTE = (
    "[缓存命中] 本回合内已经抓过同一个 URL（focus 与长度也相同），下面是那次的正文，本次未再出网。"
)
