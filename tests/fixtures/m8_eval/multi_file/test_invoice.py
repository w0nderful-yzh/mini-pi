"""费用规则与订单合计的外部判定。"""

from fees import discount
from invoice import total


def test_discount_and_total() -> None:
    """两处实现必须同时修复，且保持接口不变。"""
    assert discount(200, 10) == 20
    assert total(200, 10, 5) == 185
