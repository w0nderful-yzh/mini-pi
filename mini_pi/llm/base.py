"""LLM 客户端协议、重试与聚合逻辑。"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import Protocol

from mini_pi.errors import LLMError
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    StreamEvent,
    ToolSchema,
)


class LLMClient(Protocol):
    """Agent Loop 只依赖此协议，不感知具体 Provider。"""

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """契约：流式产出文本/思考增量、错误或 done 事件。"""
        ...

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """契约：一次性返回聚合后的完整 assistant 消息。"""
        ...


def backoff_seconds(attempt: int) -> float:
    """指数退避，封顶 8 秒（attempt 从 0 开始）。"""
    return min(0.5 * (2**attempt), 8.0)


class BaseLLMClient(ABC):
    """统一实现重试策略：只重试"尚未产出任何事件"的可重试错误。"""

    model: str
    max_retries: int

    def __init__(
        self,
        *,
        model: str,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """保存 model 与重试上限；sleep 可注入供测试避免真实等待。"""
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.model = model
        self.max_retries = max_retries
        # sleep 可注入，测试中避免真实等待
        self._sleep = sleep

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """带重试的流式入口：仅重试尚未产出任何事件的可重试错误。"""
        for attempt in range(self.max_retries + 1):
            started = False
            try:
                for event in self._stream_once(messages, tools):
                    started = True
                    yield event
                return
            except LLMError as exc:
                # 已输出过内容 / 不可重试 / 重试耗尽 => 编码错误，结束事件流
                if started or not exc.retryable or attempt == self.max_retries:
                    yield ErrorEvent(message=str(exc), retryable=exc.retryable)
                    return
                self._sleep(backoff_seconds(attempt))

    @abstractmethod
    def _stream_once(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """契约：子类实现单次向 Provider 请求并产出原始事件流（不含重试）。"""
        ...

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """一次调用聚合为完整消息；错误直接抛 LLMError（面向直接调用方）。"""
        for event in self.stream(messages, tools):
            if isinstance(event, DoneEvent):
                return event.message
            if isinstance(event, ErrorEvent):
                raise LLMError(event.message, retryable=event.retryable)
        raise LLMError("LLM stream ended without done or error event", retryable=False)
