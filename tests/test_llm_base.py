from __future__ import annotations

from collections.abc import Iterator

from mini_pi.errors import LLMError
from mini_pi.llm.base import BaseLLMClient, backoff_seconds
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    StreamEvent,
    TextDeltaEvent,
    ToolSchema,
)


class StubClient(BaseLLMClient):
    """可脚本化失败的假客户端，用于验证重试边界。"""

    def __init__(self, errors: list[LLMError], *, started: bool = False) -> None:
        self.sleeps: list[float] = []
        super().__init__(model="stub", max_retries=2, sleep=self.sleeps.append)
        self._errors = list(errors)
        # started=True 模拟"已经产出事件后才失败"，此时不允许重试
        self._started = started
        self.attempts = 0

    def _stream_once(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        self.attempts += 1
        if self._started:
            yield TextDeltaEvent(delta="partial")
        if self._errors:
            raise self._errors.pop(0)
        yield DoneEvent(message=AssistantMessage(content="ok"))


def test_retryable_error_is_retried_with_backoff() -> None:
    """可重试错误按指数退避重试，耗尽前成功则正常返回。"""
    client = StubClient([LLMError("429", retryable=True), LLMError("500", retryable=True)])
    events = list(client.stream([]))
    assert client.attempts == 3
    assert client.sleeps == [0.5, 1.0]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].message.content == "ok"


def test_retry_exhausted_yields_error_event() -> None:
    """重试次数用尽后编码为 ErrorEvent，而不是抛出异常。"""
    client = StubClient([LLMError("429", retryable=True)] * 3)
    events = list(client.stream([]))
    assert client.attempts == 3
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].retryable is True


def test_non_retryable_error_is_not_retried() -> None:
    """4xx 等不可重试错误立即返回 ErrorEvent。"""
    client = StubClient([LLMError("401", retryable=False)])
    events = list(client.stream([]))
    assert client.attempts == 1
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].retryable is False


def test_no_retry_after_output_started() -> None:
    """已产出部分输出后不重试，避免重复内容。"""
    client = StubClient([LLMError("stream broke", retryable=True)], started=True)
    events = list(client.stream([]))
    assert client.attempts == 1
    assert isinstance(events[-1], ErrorEvent)


def test_complete_returns_final_message() -> None:
    """complete() 便捷方法聚合出最终消息。"""
    client = StubClient([])
    message = client.complete([])
    assert message.content == "ok"


def test_complete_raises_on_error_event() -> None:
    """complete() 面向直接调用方，错误以异常形式暴露。"""
    client = StubClient([LLMError("401", retryable=False)])
    try:
        client.complete([])
    except LLMError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected LLMError")


def test_backoff_is_capped() -> None:
    """退避时间封顶 8 秒，避免无限增长。"""
    assert backoff_seconds(0) == 0.5
    assert backoff_seconds(10) == 8.0
