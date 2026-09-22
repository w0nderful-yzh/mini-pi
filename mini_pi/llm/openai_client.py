"""OpenAI / OpenAI-Compatible 流式客户端。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import openai
from openai import APIStatusError, OpenAI

from mini_pi.context.sections import render_system_prompt, replay_system_messages
from mini_pi.errors import LLMError, MiniPiError
from mini_pi.llm.base import BaseLLMClient
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    Message,
    StartEvent,
    StopReason,
    StreamEvent,
    SystemMessage,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolMessage,
    ToolSchema,
    Usage,
    UserMessage,
)

# finish_reason 到内部停止原因的映射；content_filter 在 _map_finish_reason 单独处理
_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
}


def to_openai_messages(
    messages: list[Message], *, include_reasoning: bool = False
) -> list[dict[str, Any]]:
    """transcript 转 OpenAI wire 格式；DeepSeek 需要 include_reasoning=True。"""
    wire: list[dict[str, Any]] = []
    system_messages = [message for message in messages if isinstance(message, SystemMessage)]
    system_state = replay_system_messages(system_messages)
    if system_state is not None:
        wire.append({"role": "system", "content": render_system_prompt(system_state)})
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        elif isinstance(message, UserMessage):
            wire.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            # 只要存在 tool_calls，OpenAI 要求 content 为 null 才能正确回放
            item: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if include_reasoning and message.reasoning_content is not None:
                item["reasoning_content"] = message.reasoning_content
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            wire.append(item)
        elif isinstance(message, ToolMessage):
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
    return wire


def to_openai_tools(tools: list[ToolSchema]) -> list[dict[str, Any]]:
    """工具 schema 转 OpenAI function 定义。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _parse_usage(usage: Any) -> Usage:
    """映射 usage；reasoning_tokens 可能不存在，缺失按 0 处理。"""
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", 0) or 0
    return Usage(
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        total_tokens=usage.total_tokens or 0,
        reasoning_tokens=reasoning,
    )


def _status_to_llm_error(exc: APIStatusError) -> LLMError:
    """HTTP 状态分类：408/409/429/5xx 可重试，其余（400/401/403）不重试。"""
    status = exc.status_code
    retryable = status in {408, 409, 429} or status >= 500
    return LLMError(f"LLM API error {status}: {exc}", retryable=retryable, status_code=status)


def _map_finish_reason(reason: str | None) -> StopReason:
    """映射 finish_reason；未知值显式报错，不猜测。"""
    if reason is None:
        return "stop"
    if reason == "content_filter":
        raise LLMError("model stopped because of content filter", retryable=False)
    mapped = _FINISH_REASONS.get(reason)
    if mapped is None:
        raise LLMError(f"unknown finish_reason: {reason}", retryable=False)
    return mapped


class _ToolCallBuffer:
    """单个工具调用的参数 JSON 拼装缓冲。"""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_json = ""


class _AssistantAccumulator:
    """把 SSE chunk 序列聚合成 AssistantMessage，并产出对应事件。"""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: dict[int, _ToolCallBuffer] = {}
        self._finish_reason: str | None = None
        self._usage: Usage | None = None

    def consume(self, chunk: Any) -> Iterator[StreamEvent]:
        # usage 可能出现在 choices 为空的最后一个 chunk 上
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._usage = _parse_usage(usage)
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        if choice.finish_reason is not None:
            self._finish_reason = choice.finish_reason
        delta = choice.delta
        if delta is None:
            return
        if delta.content:
            self._text.append(delta.content)
            yield TextDeltaEvent(delta=delta.content)
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            self._reasoning.append(reasoning)
            yield ThinkingDeltaEvent(delta=reasoning)
        for tool_delta in delta.tool_calls or []:
            buffer = self._tool_calls.get(tool_delta.index)
            if buffer is None:
                # 新调用：首个分片携带 id 与函数名
                buffer = _ToolCallBuffer()
                buffer.id = tool_delta.id or f"call_{tool_delta.index}"
                buffer.name = tool_delta.function.name or ""
                self._tool_calls[tool_delta.index] = buffer
                yield ToolCallStartEvent(
                    index=tool_delta.index, id=buffer.id, name=buffer.name
                )
            else:
                if tool_delta.id:
                    buffer.id = tool_delta.id
                if tool_delta.function is not None and tool_delta.function.name:
                    buffer.name = tool_delta.function.name
            if tool_delta.function is not None and tool_delta.function.arguments:
                buffer.arguments_json += tool_delta.function.arguments
                yield ToolCallDeltaEvent(
                    index=tool_delta.index, arguments_delta=tool_delta.function.arguments
                )

    def _build_tool_calls(self) -> list[tuple[int, ToolCall]]:
        built: list[tuple[int, ToolCall]] = []
        for index in sorted(self._tool_calls):
            buffer = self._tool_calls[index]
            if not buffer.name:
                raise LLMError(f"tool call #{index} is missing a function name", retryable=False)
            if not buffer.arguments_json.strip():
                arguments: dict[str, Any] = {}
            else:
                try:
                    parsed = json.loads(buffer.arguments_json)
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"tool call {buffer.name} has invalid JSON arguments: {exc}",
                        retryable=False,
                    ) from exc
                if not isinstance(parsed, dict):
                    raise LLMError(
                        f"tool call {buffer.name} arguments must be a JSON object",
                        retryable=False,
                    )
                arguments = parsed
            built.append((index, ToolCall(id=buffer.id, name=buffer.name, arguments=arguments)))
        return built

    def finalize(self) -> Iterator[StreamEvent]:
        """流结束：先补发 tool_call_end，再发携带完整消息的 done。"""
        built = self._build_tool_calls()
        for index, tool_call in built:
            yield ToolCallEndEvent(index=index, tool_call=tool_call)
        yield DoneEvent(
            message=AssistantMessage(
                content="".join(self._text),
                reasoning_content="".join(self._reasoning) or None,
                tool_calls=[call for _, call in built],
                stop_reason=_map_finish_reason(self._finish_reason),
                usage=self._usage,
            )
        )


class OpenAIClient(BaseLLMClient):
    """同步流式客户端；OpenAI 与 OpenAI-Compatible 共用此实现。"""

    api_key_env = "OPENAI_API_KEY"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        client: Any = None,
    ) -> None:
        super().__init__(model=model, max_retries=max_retries, sleep=sleep)
        if client is None:
            resolved_key = api_key or os.environ.get(self.api_key_env)
            if not resolved_key:
                raise MiniPiError(f"{self.api_key_env} is not set")
            # SDK 自身关闭重试，由 BaseLLMClient 统一控制，避免双重退避
            client = OpenAI(api_key=resolved_key, base_url=base_url, max_retries=0)
        self._client = client

    def _messages_to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        return to_openai_messages(messages)

    def _stream_once(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_wire(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = to_openai_tools(tools)
        # 请求发起失败：此时尚未产出任何事件，可交给基类重试
        try:
            stream = self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            raise _status_to_llm_error(exc) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise LLMError(f"connection error: {exc}", retryable=True) from exc
        yield StartEvent()
        accumulator = _AssistantAccumulator()
        # 流式过程中失败：StartEvent 已产出，基类不会重试
        try:
            for chunk in stream:
                yield from accumulator.consume(chunk)
            yield from accumulator.finalize()
        except APIStatusError as exc:
            raise _status_to_llm_error(exc) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise LLMError(f"connection error: {exc}", retryable=True) from exc
