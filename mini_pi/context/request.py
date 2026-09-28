"""按实际请求载荷估算下一次模型输入，保留消息与工具的同批快照。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from mini_pi.context.tokens import TokenSource, estimate_text_tokens
from mini_pi.llm.openai_client import to_openai_messages, to_openai_tools
from mini_pi.llm.types import Message, ToolSchema

# DeepSeek 固定样本中，请求框架和工具模式有额外输入开销；仅作为估算，不标记为实测。
_REQUEST_OVERHEAD_TOKENS = 18
# 工具模式的一次性开销，与每个工具自身的 wire 结构开销分开：M7.9.3 用 1/3/7 个工具
# 实测对拍发现，只按 schema 文本计量会随工具数线性低估（7 个工具时 −16.6%）。
_TOOLS_OVERHEAD_TOKENS = 220
_TOOL_FRAMING_TOKENS = 32


@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    """一次待发送请求的消息、工具和可复现输入估算。"""

    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    provider: str | None
    model: str | None
    input_tokens: int
    source: TokenSource


def _estimate_wire(value: list[dict[str, object]]) -> int:
    """按稳定 JSON 估算非空 wire 数组，空数组不产生请求内容。"""
    if not value:
        return 0
    return estimate_text_tokens(json.dumps(value, ensure_ascii=False, sort_keys=True))


def estimate_tools_tokens(tools: Sequence[ToolSchema]) -> int:
    """按 Provider function 载荷估算当前工具集。

    组成：每个工具的 schema 文本 + 每工具一次的 wire 结构开销 + 进入工具模式的一次性开销。
    结构开销必须按工具数累加，否则工具越多越低估。
    """
    if not tools:
        return 0
    return (
        _estimate_wire(to_openai_tools(list(tools)))
        + _TOOLS_OVERHEAD_TOKENS
        + _TOOL_FRAMING_TOKENS * len(tools)
    )


def estimate_request(
    messages: Sequence[Message],
    tools: Sequence[ToolSchema],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> RequestSnapshot:
    """冻结同批消息与工具，以实际 wire 形态预测输入；历史 usage 不充当本次实测。"""
    frozen_messages = tuple(message.model_copy(deep=True) for message in messages)
    frozen_tools = tuple(tool.model_copy(deep=True) for tool in tools)
    wire_messages = to_openai_messages(
        list(frozen_messages), include_reasoning=provider == "deepseek"
    )
    # schema 与消息分别估算，方便分类展示；两项相加是唯一的请求预测。
    input_tokens = (
        _estimate_wire(wire_messages)
        + (_REQUEST_OVERHEAD_TOKENS if wire_messages else 0)
        + estimate_tools_tokens(frozen_tools)
    )
    return RequestSnapshot(
        messages=frozen_messages,
        tools=frozen_tools,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        source="estimated",
    )
