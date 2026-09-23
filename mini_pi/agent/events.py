"""Agent Loop 对外发出的事件模型。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mini_pi.llm.types import AssistantMessage, ToolCall
from mini_pi.tools.base import ToolResult


class AgentStartEvent(BaseModel):
    """一次 run 开始。"""

    type: Literal["agent_start"] = "agent_start"


class TurnStartEvent(BaseModel):
    """一轮 LLM 调用开始。"""

    type: Literal["turn_start"] = "turn_start"
    step: int


class MessageStartEvent(BaseModel):
    """流式 assistant 消息开始。"""

    type: Literal["message_start"] = "message_start"


class MessageDeltaEvent(BaseModel):
    """流式增量；kind 区分正文与思考内容。"""

    type: Literal["message_delta"] = "message_delta"
    kind: Literal["text", "thinking"]
    delta: str


class MessageEndEvent(BaseModel):
    """assistant 消息聚合完成。"""

    type: Literal["message_end"] = "message_end"
    message: AssistantMessage


class ToolExecutionStartEvent(BaseModel):
    """单个工具调用开始执行。"""

    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call: ToolCall


class ToolExecutionEndEvent(BaseModel):
    """单个工具调用结束；is_error 表示可预期失败已转成 observation。"""

    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call: ToolCall
    result: ToolResult
    is_error: bool


class TurnEndEvent(BaseModel):
    """一轮 LLM 调用与工具执行结束。"""

    type: Literal["turn_end"] = "turn_end"
    step: int


class BudgetWarningEvent(BaseModel):
    """下一请求接近用户预算；提示只作用于本次 run。"""

    type: Literal["budget_warning"] = "budget_warning"
    limit: int
    used: int
    remaining: int
    predicted_next_input: int
    source: Literal["provider", "estimated", "mixed"]


class AgentEndEvent(BaseModel):
    """一次 run 结束，reason 说明终止原因。"""

    type: Literal["agent_end"] = "agent_end"
    reason: Literal["completed", "step_limit", "budget_limit", "error"]
    message: AssistantMessage | None = None
    error: str | None = None
    budget_limit: int | None = None
    budget_used: int | None = None
    predicted_next_input: int | None = None
    budget_source: Literal["provider", "estimated", "mixed"] | None = None


# 以 type 为判别字段的事件联合，CLI 只做分发渲染，不参与决策
AgentEvent = Annotated[
    AgentStartEvent
    | TurnStartEvent
    | MessageStartEvent
    | MessageDeltaEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionEndEvent
    | TurnEndEvent
    | BudgetWarningEvent
    | AgentEndEvent,
    Field(discriminator="type"),
]
