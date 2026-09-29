"""逗号分隔整数的解析。"""


def parse_numbers(raw: str) -> list[int]:
    """忽略首尾空白和空字段。"""
    return [int(part) for part in raw.split(",")]
