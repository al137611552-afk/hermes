"""价目与成本换算（ADR 0025 P2）：**纯逻辑**，与存储/UI 分离，可脱离环境单测。

设计要点（每条都对应 ADR 里的一个决策）：

- **币种是一等字段**，不做汇率换算。多币种**分币种汇总**，绝不合并成一个数——
  汇率天天变，合起来又是一个会漂的错数（决策 4）。
- **缓存三态分别计价**（未命中输入 / 写缓存 / 读缓存）。以前那个"缓存读=输入价×10%"的
  常数对所有厂商一视同仁，必错；实测这条路更要命——真跑里一轮的输入有 **99% 是缓存命中**
  （179 未命中 vs 17152 命中），几乎整个成本都由这个系数决定（决策 1）。
- **没有可信价格就不出金额**，只出 token（决策 3）。
- **匹配 `model_id` 而不是档名**，且用**前缀**匹配而非任意子串——旧的子串写法会让
  `opus` 命中任何名字含该词的档（决策 4）。

**这里没有内置价目表，是刻意的**：曾平移过一份公开牌价当兜底，但它全是美元、又未经核实，
而用户按人民币结算——两条加起来就是**安静地给出一个币种和数值都不对的金额**。
价格一律由用户在面板里填（字段与厂商定价页一致：输入·未命中 / 输入·命中 / 输出）。
**不凭记忆往代码里写价格**：写错一个数字，它会安静地把每一笔账都算错。
"""
from __future__ import annotations

from dataclasses import dataclass

# 每百万 token 的单价
PER = 1_000_000


@dataclass(frozen=True)
class Price:
    currency: str                       # "USD" / "CNY" / ...
    input: float                        # 未命中缓存的输入
    output: float
    cache_read: float | None = None     # None = 厂商未单列/用户没填 → 回落输入价并标记 inferred
    cache_write: float | None = None    # 同上
    as_of: str = ""                     # 这份价格是哪天的（空=不知道）
    source: str = "bundled"             # bundled | user
    verified: bool = False              # 是否经人确认过
    note: str = ""


def match_price(model_id: str | None, table: dict[str, Price]) -> Price | None:
    """按 **最长前缀** 匹配价格；匹配不到返回 None（调用方据此只显 token）。

    最长优先：`gpt-4o-mini` 必须命中它自己那条，而不是更短的 `gpt-4o`。
    """
    if not model_id:
        return None
    m = model_id.strip().lower()
    best: tuple[int, Price] | None = None
    for prefix, price in table.items():
        p = prefix.lower()
        if m.startswith(p) and (best is None or len(p) > best[0]):
            best = (len(p), price)
    return best[1] if best else None


def resolve_price(model_id: str | None, user_prices: dict[str, Price] | None = None) -> Price | None:
    """价格**只来自用户手填**：先精确 `model_id`，再前缀匹配；都没有 → None（只显 token）。

    **不再随包带内置牌价**（2026-08-14 改）：原来平移了一份 pure.js 的公开牌价当兜底，
    但它①全是美元，而用户按人民币结算；②未经核实。两条加起来＝**安静地给出一个币种和
    数值都不对的金额**，正是本 ADR 要消灭的东西。宁可只显 token。
    """
    users = user_prices or {}
    if model_id and model_id.strip().lower() in {k.strip().lower() for k in users}:
        for k, v in users.items():
            if k.strip().lower() == model_id.strip().lower():
                return v
    return match_price(model_id, users)


def cost_of(tokens: dict, price: Price | None) -> dict | None:
    """按价目算一行/一批的金额。没有价格 → None（**只显 token，不给错数**）。

    返回 `{currency, amount, inferred, parts}`：
    - `inferred=True` 表示**缓存单价是推断的**（价目里没单列，按输入价回落）——
      UI 必须把这种数标出来，别让它冒充精确值。
    - `parts` 给出四类各花了多少，便于在面板上看"钱花在缓存写还是输出上"。
    """
    if price is None:
        return None
    uncached = tokens.get("input_uncached", 0) or 0
    cw = tokens.get("input_cache_write", 0) or 0
    cr = tokens.get("input_cache_read", 0) or 0
    out = tokens.get("output", 0) or 0

    inferred = False
    p_cr = price.cache_read
    if p_cr is None:
        p_cr = price.input          # 回落：把缓存读按普通输入价算（偏高，但不假装便宜）
        inferred = inferred or cr > 0
    p_cw = price.cache_write
    if p_cw is None:
        p_cw = price.input
        inferred = inferred or cw > 0

    parts = {
        "input_uncached": uncached * price.input / PER,
        "input_cache_write": cw * p_cw / PER,
        "input_cache_read": cr * p_cr / PER,
        "output": out * price.output / PER,
    }
    return {
        "currency": price.currency,
        "amount": sum(parts.values()),
        "inferred": inferred,
        "parts": parts,
    }


def summarize_costs(rows: list[dict], resolve=None) -> dict:
    """把若干条（已带 `model_id` 与 token 列的）汇总行折算成**分币种**的金额。

    `resolve(model_id) -> Price | None` 可注入，便于单测与接用户价目表。

    返回：
    ```
    {"by_currency": {"USD": {...}, "CNY": {...}},
     "unpriced_rows": 2,      # 没有价格、只能算 token 的有几条
     "inferred": True}        # 是否有任何一笔用了推断的缓存单价
    ```
    **绝不把不同币种加在一起**——那是决策 4 明确拒绝的事。
    """
    resolve = resolve or (lambda mid: resolve_price(mid))
    by_currency: dict[str, dict] = {}
    unpriced = 0
    inferred_any = False
    for r in rows:
        c = cost_of(r, resolve(r.get("model_id")))
        if c is None:
            unpriced += 1
            continue
        inferred_any = inferred_any or c["inferred"]
        slot = by_currency.setdefault(c["currency"], {"amount": 0.0, "rows": 0, "inferred": False})
        slot["amount"] += c["amount"]
        slot["rows"] += 1
        slot["inferred"] = slot["inferred"] or c["inferred"]
    return {"by_currency": by_currency, "unpriced_rows": unpriced, "inferred": inferred_any}


def is_stale(price: Price, today: str, max_days: int = 180) -> bool:
    """价格是否该标黄（`as_of` 太旧或压根没有）。日期用 `YYYY-MM-DD`。

    **没有 as_of 一律算过期**——不知道是哪天的价格，就不能当它是新的。
    """
    if not price.as_of:
        return True
    try:
        from datetime import date
        y1, m1, d1 = (int(x) for x in price.as_of.split("-"))
        y2, m2, d2 = (int(x) for x in today.split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days > max_days
    except Exception:  # noqa: BLE001 — 日期格式不对当作过期，别当作"新鲜"
        return True
