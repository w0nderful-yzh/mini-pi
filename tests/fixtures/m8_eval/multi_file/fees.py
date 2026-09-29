"""订单金额计算的费用规则。"""


def discount(amount: int, percent: int) -> int:
    """按整数百分比返回折扣金额。"""
    return amount * percent // 10
