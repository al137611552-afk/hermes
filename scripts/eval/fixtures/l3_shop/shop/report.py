"""文本报表：把一个购物车汇总成给人看的清单。"""

from . import catalog, pricing


def summarize(cart):
    """返回多行文本报表（明细 + 件数 + 小计 + 折扣 + 应付）。"""
    lines = ["===== 订单明细 ====="]
    for sku, qty in cart.lines():
        item = catalog.get_item(sku)
        lines.append(f"{sku}  {item['name']}  x{qty}  单价 {item['price']:.2f}")
    pct = pricing.bulk_discount(cart.count())
    lines += [
        "-" * 22,
        f"件数：{cart.count()}",
        f"小计：{cart.subtotal():.2f}",
        f"满量折扣：{pct}%",
        f"应付（含税 {pricing.TAX_RATE:.0%}）：{cart.total():.2f}",
    ]
    return "\n".join(lines)
