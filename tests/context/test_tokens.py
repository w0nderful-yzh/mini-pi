"""M7.4d：token 估算的来源与字符规则测试。"""

from __future__ import annotations

from mini_pi.context.request import estimate_request, estimate_tools_tokens
from mini_pi.context.tokens import TokenEstimate, estimate_text_tokens, estimate_tokens
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    ToolSchema,
    Usage,
    UserMessage,
)


def test_usage_with_no_trailing_messages_is_provider_exact() -> None:
    """usage 覆盖全部投影时，来源必须是 provider，而不是估算。"""
    messages: list[Message] = [
        UserMessage(content="hi"),
        AssistantMessage(content="ok", usage=Usage(total_tokens=100)),
    ]

    assert estimate_tokens(messages) == TokenEstimate(tokens=100, source="usage")


def test_trailing_messages_are_estimated_on_top_of_usage() -> None:
    """usage 之后还有消息时，只能标记为 estimated。"""
    messages: list[Message] = [
        AssistantMessage(content="ok", usage=Usage(total_tokens=100)),
        UserMessage(content="abcdefgh"),
    ]

    assert estimate_tokens(messages) == TokenEstimate(tokens=102, source="estimated")


def test_without_usage_estimates_every_message() -> None:
    """没有 usage 时每条消息按加权文本估算并分别向上取整。"""
    messages: list[Message] = [UserMessage(content="abcd"), AssistantMessage(content="efgh")]

    assert estimate_tokens(messages) == TokenEstimate(tokens=2, source="estimated")


def test_last_assistant_usage_wins() -> None:
    """多段 usage 以最近一次 assistant 为准。"""
    messages: list[Message] = [
        AssistantMessage(content="a", usage=Usage(total_tokens=100)),
        UserMessage(content="b"),
        AssistantMessage(content="c", usage=Usage(total_tokens=7)),
    ]

    assert estimate_tokens(messages) == TokenEstimate(tokens=7, source="usage")


def test_empty_payloads_count_zero() -> None:
    """空消息不引入虚假 token（分隔符也不能计数）。"""
    messages: list[Message] = [
        UserMessage(content=""),
        AssistantMessage(content=""),
        ToolMessage(tool_call_id="c1", name="", content=""),
    ]

    assert estimate_tokens(messages) == TokenEstimate(tokens=0, source="estimated")


def test_character_classes_have_distinct_weights() -> None:
    """ASCII、东亚文字与标点、其他宽字符各自使用固定权重。"""
    assert estimate_text_tokens("a" * 8) == 2  # ASCII 每字 1/5 token，向上取整。
    assert estimate_text_tokens("你好世界") == 3  # CJK 每字 5/8 token。
    assert estimate_text_tokens("你好，世界") == 4  # 东亚标点与文字同类。
    assert estimate_text_tokens("😀😀") == 2  # 其他宽字符每字 1 token。
    assert estimate_text_tokens("") == 0


def test_tool_call_arguments_and_result_are_counted() -> None:
    """工具名、参数与结果都属于模型可见输入。"""
    bare = estimate_tokens(
        [AssistantMessage(tool_calls=[ToolCall(id="c1", name="read", arguments={})])]
    )
    with_args = estimate_tokens(
        [
            AssistantMessage(
                tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "a/b.txt"})]
            )
        ]
    )
    result = estimate_tokens([ToolMessage(tool_call_id="c1", name="read", content="data")])

    assert with_args.tokens > bare.tokens
    # 改用 ASCII 1/5 权重后，名称、换行和正文合计 9 字符，向上取整为 2。
    assert result.tokens == 2


def test_reasoning_content_is_counted() -> None:
    """需要回放的 reasoning_content 不能漏算。"""
    without = estimate_tokens([AssistantMessage(content="x")])
    with_reasoning = estimate_tokens(
        [AssistantMessage(content="x", reasoning_content="thinking hard")]
    )

    assert with_reasoning.tokens > without.tokens


def test_system_sections_are_counted() -> None:
    """结构化 system 快照按 section 文本合计。"""
    estimate = estimate_tokens([SystemMessage(sections={"preamble": "abcd", "rules": "ef"})])

    assert estimate.tokens == 2


def test_very_long_message_uses_ceiling_division() -> None:
    """极长消息必须向上取整，不能截断成整数除法。"""
    assert estimate_tokens([UserMessage(content="x" * 100_001)]).tokens == 20001


def test_tool_schema_uses_wire_payload_and_one_mode_overhead() -> None:
    """工具模式开销只加一次，schema 文本越长仍需增加预测。"""
    small = ToolSchema(name="ping", description="Pong", parameters={"type": "object"})
    large = ToolSchema(
        name="read", description="读取工作区文件。" * 10, parameters={"type": "object"}
    )
    assert estimate_tools_tokens([]) == 0
    assert estimate_tools_tokens([small, large]) > estimate_tools_tokens([small])
    # 第二个工具只增加自己的 wire 载荷，不重复计入工具模式固定开销。
    assert estimate_tools_tokens([small, large]) - estimate_tools_tokens([small]) < 100


def test_historical_usage_remains_separate_from_current_request() -> None:
    """旧 total_tokens 可用于区域锚点，但不能冒充下一请求的输入实测。"""
    old = AssistantMessage(content="done", usage=Usage(input_tokens=10, total_tokens=100_000))
    messages: list[Message] = [old, UserMessage(content="继续")]
    region = estimate_tokens(messages)
    request = estimate_request(messages, [], provider="deepseek")

    assert region.tokens > 100_000
    assert region.source == "estimated"
    assert request.input_tokens < 100
    assert request.source == "estimated"
