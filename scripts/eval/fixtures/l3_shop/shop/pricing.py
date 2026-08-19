"""计价规则：满量折扣、百分比折扣、含税金额。"""

TAX_RATE = 0.06

# 满量折扣档位：(件数门槛, 折扣百分比)，从低到高
_BULK_TIERS = ((3, 5), (5, 10), (10, 15))


def bulk_discount(qty):
    """按整车件数给折扣百分比（取满足的最高一档）。

    业务口径见 README：满 3 件 5%、满 5 件 10%、满 10 件 15%。
    """
    pct = 0
    for threshold, tier_pct in _BULK_TIERS:
        if qty > threshold:
            pct = tier_pct
    return pct


def apply_discount(price, pct):
    """按百分比打折：pct=10 表示减 10%。四舍五入到分。"""
    if pct < 0 or pct > 100:
        raise ValueError("折扣百分比必须在 0..100 之间")
    return round(price * (100 - pct) / 100, 2)


def with_tax(amount, rate=TAX_RATE):
    """加税后金额，四舍五入到分。"""
    return round(amount * (1 + rate), 2)
