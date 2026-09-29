"""解析失败后的修复判定。"""

from parser import parse_numbers


def test_parse_numbers() -> None:
    """空字段和空白输入不应让解析命令报错。"""
    assert parse_numbers("1, , 2,") == [1, 2]
    assert parse_numbers("  ") == []
