"""流式事件合并（FR-12.3 性能）：把「一 token 一次跨进程调用」压成「一小段一次」。

病根在 `Api._emit`：每个事件都是一次 `evaluate_js`——**同步跨进程调用**（Windows 上要 marshal
到 WebView2 的 UI 线程），还被一把全局锁串行化。模型每吐一个 token 就走一趟，并发几个会话时
所有 worker 都堵在这把锁上，前台界面也被 UI 线程的排队拖住。

主流 agent（Claude Code 的 Ink fork、Codex 的 ratatui）都是**按帧**输出、差分写终端，不按 token。
它们写的是同进程的 stdout 都要这么做；我们这条路每次贵一到两个数量级，更没有理由不合并。

**顺序是硬约束**：chunk 可以攒，但攒着的 chunk 必须在同一对话的**任何**其它事件（tool_use /
done / error…）之前吐出去，否则用户会看到"工具块出现在它上面那段话之前"。所以本模块不是
简单的定时器，而是「按对话攒 + 遇到别的事件先冲」。

纯逻辑（本模块）与 IO（`Api._emit` 里的 evaluate_js）分离：合并/冲刷的判定可脱离 GUI 单测。
"""
from __future__ import annotations

# 可合并的事件：只有纯文本增量。**其余一律直通**——工具、终态、错误都是低频且要立刻可见的。
COALESCABLE = ("chunk", "thinking")


class ChunkCoalescer:
    """按 (cid, event) 攒文本增量，到点或被别的事件打断时冲刷。

    用法（调用方持锁）：`for ev, data, cid in co.feed(event, data, cid, now): 真正发出去`。
    `feed` 返回**已经排好序**的待发列表，调用方照单发出即可。
    """

    def __init__(self, window_s: float = 0.08, max_chars: int = 8192) -> None:
        self.window_s = window_s      # 攒多久（秒）。与前端 STREAM_FLUSH_MS 同量级
        self.max_chars = max_chars    # 攒到这么多就先发，别让一次 payload 大到卡住 JSON 序列化
        self._buf: dict = {}          # (cid, event) -> [文本, 起始时刻]

    def _pop(self, key) -> list:
        item = self._buf.pop(key, None)
        return [] if item is None else [(key[1], item[0], key[0])]

    def flush_cid(self, cid) -> list:
        """冲掉某对话攒着的全部增量（顺序按事件名固定，保证可预测）。"""
        out = []
        for key in sorted([k for k in self._buf if k[0] == cid], key=lambda k: k[1]):
            out += self._pop(key)
        return out

    def flush_all(self) -> list:
        out = []
        for key in sorted(list(self._buf), key=lambda k: (str(k[0]), k[1])):
            out += self._pop(key)
        return out

    def feed(self, event: str, data, cid, now: float) -> list:
        """喂一个事件，返回**现在就该发出去**的事件列表（含被它挤出来的积压）。"""
        if event not in COALESCABLE or not isinstance(data, str):
            # 不可合并的事件：先把同一对话攒着的文本吐干净，再发它自己——顺序不能乱
            return self.flush_cid(cid) + [(event, data, cid)]
        key = (cid, event)
        buf = self._buf.get(key)
        if buf is None:
            self._buf[key] = [data, now]
            buf = self._buf[key]
        else:
            buf[0] += data
        if now - buf[1] >= self.window_s or len(buf[0]) >= self.max_chars:
            return self._pop(key)
        return []

    def due(self, now: float) -> list:
        """定时器调用：把已经攒够时间的吐出去（否则出字一停，最后一截会一直压着）。"""
        out = []
        for key in sorted([k for k, v in self._buf.items() if now - v[1] >= self.window_s],
                          key=lambda k: (str(k[0]), k[1])):
            out += self._pop(key)
        return out

    def pending(self) -> bool:
        return bool(self._buf)
