"""运行：python run_tests.py —— 全通过时打印 ALL TESTS PASSED。"""
from shop import catalog, pricing
from shop.cart import Cart
from shop.report import summarize

# ---- catalog ----
assert catalog.get_item("kb-01")["price"] == 399.0
assert len(catalog.all_items()) == 5
try:
    catalog.get_item("nope")
except KeyError:
    pass
else:
    raise AssertionError("未知 SKU 应当抛 KeyError")

# ---- pricing ----
assert pricing.apply_discount(100.0, 10) == 90.0
assert pricing.apply_discount(99.99, 0) == 99.99
assert pricing.with_tax(100.0) == 106.0
assert pricing.bulk_discount(1) == 0
assert pricing.bulk_discount(4) == 5
assert pricing.bulk_discount(12) == 15

# ---- cart ----
c = Cart().add("kb-01").add("ms-02", 2)
assert c.count() == 3
assert c.subtotal() == 657.0

# ---- report ----
text = summarize(Cart().add("mn-03"))
assert "订单明细" in text and "显示器" in text and "应付" in text

print("ALL TESTS PASSED")
