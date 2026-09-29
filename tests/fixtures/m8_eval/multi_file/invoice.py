"""订单最终应付金额。"""

from fees import discount


def total(amount: int, percent: int, shipping: int) -> int:
    """从商品金额中扣除折扣并加上运费。"""
    return amount + discount(amount, percent) + shipping
