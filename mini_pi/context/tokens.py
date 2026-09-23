"""与 provider 无关的 token 估算：优先使用精确 usage，否则按字符规则估算。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from mini_pi.llm.types import (
    SYSTEM_PROMPT_SECTION_IDS,
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
)

# 统一字符规则：每 4 个字符约 1 token，向上取整
_CHARS_PER_TOKEN = 4

TokenSource = Literal["usage", "estimated"]


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """估算结果；`source` 明确区分 provider 精确值与字符估算。"""

    tokens: int
    source: TokenSource


def estimate_tokens(messages: Sequence[Message]) -> TokenEstimate:
    """估算投影消息的 token 数。

    优先级：最近一次 assistant 的 usage.total_tokens 全额采用，其后的消息按字符
    规则追加估算；完全没有 usage 时对全部消息估算。只要掺入了估算，source 就不得
    标记为 `usage`。
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AssistantMessage) and message.usage is not None:
            trailing = messages[index + 1 :]
            return TokenEstimate(
                tokens=message.usage.total_tokens
                + sum(_estimate_message(item) for item in trailing),
                source="estimated" if trailing else "usage",
            )
    return TokenEstimate(
        tokens=sum(_estimate_message(message) for message in messages),
        source="estimated",
    )


def _estimate_message(message: Message) -> int:
    """单条消息的字符规则估算。"""
    return _ceil_div(len(_message_text(message)), _CHARS_PER_TOKEN)


def estimate_message_tokens(message: Message) -> int:
    """供当前投影的分类统计复用同一字符估算规则。"""
    return _estimate_message(message)


def estimate_text_tokens(text: str) -> int:
    """对纯文本按同一字符规则估算；成本模型与统计复用，不引入第二套口径。"""
    return _ceil_div(len(text), _CHARS_PER_TOKEN)


def _ceil_div(value: int, divisor: int) -> int:
    """整数向上取整，避免浮点在极长文本上丢精度。"""
    return -(-value // divisor)


def _message_text(message: Message) -> str:
    """提取模型可见的文本；不含仅供 UI 使用的 details。"""
    if isinstance(message, SystemMessage):
        return _system_text(message)
    if isinstance(message, ToolMessage):
        return _join_parts(message.name, message.content)
    if isinstance(message, AssistantMessage):
        parts = [message.content, message.reasoning_content or ""]
        parts.extend(_tool_call_text(call) for call in message.tool_calls)
        return _join_parts(*parts)
    return message.content


def _system_text(message: SystemMessage) -> str:
    """system 消息按当前载荷形态取文本；patch 只计自身文本。"""
    if message.content is not None:
        return message.content
    if message.sections is not None:
        return _join_parts(
            *(
                message.sections[section_id]
                for section_id in SYSTEM_PROMPT_SECTION_IDS
                if section_id in message.sections
            )
        )
    if message.section_patch is not None:
        return _join_parts(*(patch.content or "" for patch in message.section_patch))
    raise ValueError("system message has no replayable payload")


def _tool_call_text(call: ToolCall) -> str:
    """参数按稳定顺序序列化，保证同一请求的估算可复现。"""
    return json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
    )


def _join_parts(*parts: str) -> str:
    """过滤空片段后拼接，避免分隔符本身贡献虚假 token。"""
    return "\n".join(part for part in parts if part)
