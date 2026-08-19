"""商品目录：只读数据源（不含价格计算逻辑）。"""


_ITEMS = {
    "kb-01": {"name": "机械键盘", "price": 399.0, "category": "外设"},
    "ms-02": {"name": "无线鼠标", "price": 129.0, "category": "外设"},
    "mn-03": {"name": "显示器", "price": 1299.0, "category": "显示"},
    "hp-04": {"name": "头戴耳机", "price": 259.0, "category": "音频"},
    "cb-05": {"name": "数据线", "price": 39.0, "category": "配件"},
}


def get_item(sku):
    """按 SKU 取商品；不存在抛 KeyError。返回的是副本，外部改不坏目录。"""
    if sku not in _ITEMS:
        raise KeyError(f"未知 SKU：{sku}")
    return dict(_ITEMS[sku])


def all_items():
    """全部商品：{sku: 商品副本}。"""
    return {sku: dict(item) for sku, item in _ITEMS.items()}


def skus():
    return sorted(_ITEMS)
