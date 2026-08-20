"""购物车：加购、小计、应付合计。"""

from . import catalog, pricing


class Cart:
    def __init__(self):
        self._lines = []      # [(sku, qty)]

    def add(self, sku, qty=1):
        """加购。SKU 不存在会由 catalog 抛 KeyError；数量必须为正。"""
        if qty <= 0:
            raise ValueError("数量必须为正")
        catalog.get_item(sku)      # 校验 SKU 存在
        self._lines.append((sku, int(qty)))
        return self

    def lines(self):
        return list(self._lines)

    def count(self):
        """整车件数。"""
        return sum(qty for _sku, qty in self._lines)

    def subtotal(self):
        """未折未税小计。"""
        total = 0.0
        for sku, qty in self._lines:
            total += catalog.get_item(sku)["price"] * qty
        return round(total, 2)

    def total(self):
        """应付合计：小计 → 满量折扣 → 加税。"""
        pct = pricing.bulk_discount(self.count())
        return pricing.with_tax(pricing.apply_discount(self.subtotal(), pct))
