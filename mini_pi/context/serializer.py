"""M7.5a：把活动投影消息确定性序列化为摘要模型的输入文本。"""

from __future__ import annotations

import json
from collections.abc import Sequence

from mini_pi.llm.types import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage

# 单条 tool result 进入摘要请求的字符上限：摘要只需要失败原因、结论与后续动作，
# 不需要完整日志；超出部分按头部截断并显式标记。
TOOL_RESULT_LIMIT = 2000

# 截断必须显式标注，避免摘要模型把残缺日志当成完整事实
_TRUNCATION_MARKER = "[... {omitted} more characters truncated]"


def serialize_transcript(messages: Sequence[Message]) -> str:
    """按消息顺序输出带 role 标签的确定性文本；不修改入参、不调用模型。

    - system 消息不参与摘要：system 快照由 CompactionEntry 单独保存
    - tool call 与 tool result 都带工具名和 call id，失败结果使用 error 标签
    - 单条 tool result 头部截断到 `TOOL_RESULT_LIMIT` 并标记省略字符数
    - 参数按 key 排序序列化，同一历史每次得到完全相同的文本
    """
    parts: list[str] = []
    for message in messages:
        part = _serialize_message(message)
        if part is not None:
            parts.append(part)
    return "\n\n".join(parts)


def _serialize_message(message: Message) -> str | None:
    """单条消息序列化；没有可摘要事实时返回 None。"""
    if isinstance(message, UserMessage):
        return _text_block("User", message.content)
    if isinstance(message, AssistantMessage):
        return _serialize_assistant(message)
    if isinstance(message, ToolMessage):
        return _serialize_tool(message)
    # SystemMessage：完整快照由 CompactionEntry.systemMessage 承载，不进摘要输入
    return None


def _serialize_assistant(message: AssistantMessage) -> str | None:
    """思考、正文、工具调用与错误状态各占一行，空内容不产出标签。"""
    lines: list[str] = []
    if message.reasoning_content and message.reasoning_content.strip():
        lines.append(f"[Assistant thinking]: {message.reasoning_content}")
    text = _text_block("Assistant", message.content)
    if text is not None:
        lines.append(text)
    if message.tool_calls:
        calls = "; ".join(_format_tool_call(call) for call in message.tool_calls)
        lines.append(f"[Assistant tool calls]: {calls}")
    if message.error_message and message.error_message.strip():
        lines.append(f"[Assistant error]: {message.error_message}")
    return "\n".join(lines) if lines else None


def _serialize_tool(message: ToolMessage) -> str:
    """结果标签必须带工具名与 call id；空结果也保留标签，避免丢掉执行事实。"""
    label = "Tool error" if message.is_error else "Tool result"
    header = f"[{label}]: {message.name} (id={message.tool_call_id})"
    content = _truncate(message.content)
    if not content.strip():
        return header
    return f"{header}\n{content}"


def _text_block(label: str, content: str) -> str | None:
    """空正文不产出标签，避免只有标签没有事实的行。"""
    if not content.strip():
        return None
    return f"[{label}]: {content}"


def _format_tool_call(call: ToolCall) -> str:
    """参数按 key 排序，保证同一调用的文本可复现。"""
    arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
    return f"{call.name}(id={call.id}, arguments={arguments})"


def _truncate(content: str) -> str:
    """头部截断并标记省略量：摘要依赖开头的结论，不依赖日志尾部。"""
    if len(content) <= TOOL_RESULT_LIMIT:
        return content
    omitted = len(content) - TOOL_RESULT_LIMIT
    return f"{content[:TOOL_RESULT_LIMIT]}\n{_TRUNCATION_MARKER.format(omitted=omitted)}"
