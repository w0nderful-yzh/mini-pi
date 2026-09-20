"""消息、工具调用与流式事件的统一模型。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# 停止原因：stop=正常结束，length=输出截断，tool_calls=请求工具，error=调用失败
StopReason = Literal["stop", "length", "tool_calls", "error"]


class Usage(BaseModel):
    """token 用量统计。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0


class ToolCall(BaseModel):
    """模型请求执行的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class SystemMessage(BaseModel):
    """系统提示词消息。"""

    role: Literal["system"] = "system"
    content: str


class UserMessage(BaseModel):
    """用户输入消息。"""

    role: Literal["user"] = "user"
    content: str


class AssistantMessage(BaseModel):
    """模型回复，正文与工具调用是两类独立内容。"""

    role: Literal["assistant"] = "assistant"
    content: str = ""
    # DeepSeek reasoner 要求把思考内容回放到后续请求，否则报错
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: StopReason = "stop"
    usage: Usage | None = None
    error_message: str | None = None


class ToolMessage(BaseModel):
    """工具执行结果，作为下一轮对话的 observation。"""

    role: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


# 以 role 为判别字段的消息联合类型，用于 transcript 与持久化
Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]


class ToolSchema(BaseModel):
    """暴露给模型的工具 JSON Schema。"""

    name: str
    description: str
    parameters: dict[str, Any]


class StartEvent(BaseModel):
    """流开始。"""

    type: Literal["start"] = "start"


class TextDeltaEvent(BaseModel):
    """正文增量。"""

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ThinkingDeltaEvent(BaseModel):
    """思考内容增量（DeepSeek reasoning_content）。"""

    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ToolCallStartEvent(BaseModel):
    """新的工具调用开始（按 index 区分一次回复中的多个调用）。"""

    type: Literal["tool_call_start"] = "tool_call_start"
    index: int
    id: str
    name: str


class ToolCallDeltaEvent(BaseModel):
    """工具参数 JSON 的增量分片，需要客户端按 index 拼装。"""

    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    arguments_delta: str


class ToolCallEndEvent(BaseModel):
    """单个工具调用参数拼装完成。"""

    type: Literal["tool_call_end"] = "tool_call_end"
    index: int
    tool_call: ToolCall


class DoneEvent(BaseModel):
    """流结束，携带聚合后的完整 assistant 消息。"""

    type: Literal["done"] = "done"
    message: AssistantMessage


class ErrorEvent(BaseModel):
    """错误编码进事件流，不裸抛给 Agent Loop。"""

    type: Literal["error"] = "error"
    message: str
    retryable: bool = False


# 以 type 为判别字段的流式事件联合类型，保证 UI/Loop 安全分发
StreamEvent = Annotated[
    StartEvent
    | TextDeltaEvent
    | ThinkingDeltaEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | DoneEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
