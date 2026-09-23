"""M7.5a：摘要输入 transcript 的确定性序列化与截断测试。"""

from __future__ import annotations

from mini_pi.context.serializer import TOOL_RESULT_LIMIT, serialize_transcript
from mini_pi.llm.types import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def test_serializes_roles_tool_calls_and_results() -> None:
    """一次完整工具轮要包含 role、工具名、call id、参数与结果。"""
    messages = [
        UserMessage(content="修复登录 bug"),
        AssistantMessage(
            content="先读取文件。",
            reasoning_content="先确认参数校验位置",
            tool_calls=[
                ToolCall(id="call_1", name="read", arguments={"path": "login.py", "offset": 1})
            ],
        ),
        ToolMessage(tool_call_id="call_1", name="read", content="print('hi')"),
    ]

    assert serialize_transcript(messages) == (
        "[User]: 修复登录 bug\n"
        "\n"
        "[Assistant thinking]: 先确认参数校验位置\n"
        "[Assistant]: 先读取文件。\n"
        '[Assistant tool calls]: read(id=call_1, arguments={"offset": 1, "path": "login.py"})\n'
        "\n"
        "[Tool result]: read (id=call_1)\n"
        "print('hi')"
    )


def test_argument_key_order_does_not_change_output() -> None:
    """参数 key 的插入顺序不同也必须产出同一文本（排序后序列化）。"""
    first = AssistantMessage(
        tool_calls=[
            ToolCall(id="call_1", name="edit", arguments={"path": "a.py", "old": "x", "new": "y"})
        ]
    )
    second = AssistantMessage(
        tool_calls=[
            ToolCall(id="call_1", name="edit", arguments={"new": "y", "path": "a.py", "old": "x"})
        ]
    )

    assert serialize_transcript([first]) == serialize_transcript([second])


def test_multiple_tool_calls_keep_order_and_ids() -> None:
    """同一 assistant 的多个调用按原顺序输出，并各自带 call id。"""
    message = AssistantMessage(
        tool_calls=[
            ToolCall(id="call_1", name="read", arguments={"path": "a.py"}),
            ToolCall(id="call_2", name="bash", arguments={"command": "ls"}),
        ]
    )

    assert serialize_transcript([message]) == (
        "[Assistant tool calls]: "
        'read(id=call_1, arguments={"path": "a.py"}); '
        'bash(id=call_2, arguments={"command": "ls"})'
    )


def test_system_messages_are_not_part_of_transcript() -> None:
    """system 快照由 CompactionEntry 承载，不进入摘要输入。"""
    messages = [SystemMessage(content="项目规则"), UserMessage(content="继续")]

    assert serialize_transcript(messages) == "[User]: 继续"


def test_tool_error_keeps_error_status() -> None:
    """失败结果必须标记为 error，不能伪装成普通 observation。"""
    message = ToolMessage(tool_call_id="call_9", name="bash", content="exit code 1", is_error=True)

    assert serialize_transcript([message]) == "[Tool error]: bash (id=call_9)\nexit code 1"


def test_assistant_error_status_is_preserved() -> None:
    """失败的 assistant 没有正文，也必须留下错误状态。"""
    message = AssistantMessage(stop_reason="error", error_message="429 rate limited")

    assert serialize_transcript([message]) == "[Assistant error]: 429 rate limited"


def test_long_tool_result_is_truncated_with_marker() -> None:
    """超长结果头部截断并标记省略量，原始消息不被修改。"""
    content = "x" * (TOOL_RESULT_LIMIT + 50)
    message = ToolMessage(tool_call_id="call_1", name="bash", content=content)

    text = serialize_transcript([message])

    assert text == (
        "[Tool result]: bash (id=call_1)\n"
        f"{'x' * TOOL_RESULT_LIMIT}\n"
        "[... 50 more characters truncated]"
    )
    # 序列化只读入参：原始 message 必须保持完整
    assert message.content == content


def test_result_at_limit_is_not_marked_truncated() -> None:
    """恰好等于上限时不加截断标记。"""
    message = ToolMessage(tool_call_id="call_1", name="read", content="x" * TOOL_RESULT_LIMIT)

    assert "[... " not in serialize_transcript([message])


def test_empty_content_is_skipped_but_tool_header_survives() -> None:
    """空正文不产出标签；空结果的头部仍保留，避免丢掉执行与错误事实。"""
    messages = [
        UserMessage(content="   "),
        AssistantMessage(content=""),
        ToolMessage(tool_call_id="call_1", name="bash", content="", is_error=True),
    ]

    assert serialize_transcript(messages) == "[Tool error]: bash (id=call_1)"


def test_metadata_is_not_serialized_as_fact() -> None:
    """modified_files 等元数据不进摘要输入，事实只来自消息可见字段。"""
    message = ToolMessage(
        tool_call_id="call_1",
        name="write",
        content="wrote 4 lines",
        modified_files=["only-in-metadata.txt"],
    )

    text = serialize_transcript([message])

    assert text == "[Tool result]: write (id=call_1)\nwrote 4 lines"
    assert "only-in-metadata" not in text


def test_no_summarizable_messages_returns_empty_text() -> None:
    """没有可摘要消息时返回空串，是否视为失败由调用方决定。"""
    assert serialize_transcript([]) == ""
    assert serialize_transcript([SystemMessage(content="rules")]) == ""
