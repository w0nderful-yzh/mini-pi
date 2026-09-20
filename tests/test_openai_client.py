from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from mini_pi.errors import MiniPiError
from mini_pi.llm.openai_client import OpenAIClient, to_openai_messages, to_openai_tools
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolCallEndEvent,
    ToolMessage,
    ToolSchema,
    UserMessage,
)

READ_FILE_SCHEMA = ToolSchema(
    name="read_file",
    description="Read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


def make_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    """构造与 OpenAI SDK chunk 结构一致的假数据。"""
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def tool_delta(index: int, call_id: str | None, name: str | None, arguments: str | None):
    """构造流式 tool_call 参数分片。"""
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


class FakeCompletions:
    """记录调用参数并按脚本返回 chunk 或抛错。"""

    def __init__(
        self,
        chunks: list[SimpleNamespace] | None = None,
        error: Exception | None = None,
        failures: int = 0,
    ) -> None:
        self._chunks = chunks or []
        self._error = error
        self._failures = failures
        self.calls = 0
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self._error is not None and self.calls <= self._failures:
            raise self._error
        return iter(self._chunks)


class FakeSDK:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def make_client(
    chunks: list[SimpleNamespace] | None = None,
    *,
    error: Exception | None = None,
    failures: int = 0,
    max_retries: int = 2,
) -> tuple[OpenAIClient, FakeCompletions]:
    completions = FakeCompletions(chunks, error=error, failures=failures)
    client = OpenAIClient(
        model="gpt-test",
        api_key="test-key",
        client=FakeSDK(completions),
        max_retries=max_retries,
        sleep=lambda _: None,
    )
    return client, completions


def status_error(status: int) -> openai.APIStatusError:
    """用 httpx 响应构造真实的 SDK 状态错误，验证重试分类。"""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    if status == 429:
        return openai.RateLimitError("rate limited", response=response, body=None)
    return openai.APIStatusError("api error", response=response, body=None)


def test_text_stream_maps_to_events() -> None:
    """正文增量映射为 text_delta，并聚合出完整消息。"""
    client, _ = make_client(
        [
            make_chunk(content="Hel"),
            make_chunk(content="lo", finish_reason="stop"),
        ]
    )
    events = list(client.stream([UserMessage(content="hi")]))
    assert events[0].type == "start"
    assert [event.delta for event in events if isinstance(event, TextDeltaEvent)] == ["Hel", "lo"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.message.content == "Hello"
    assert done.message.stop_reason == "stop"


def test_usage_and_reasoning_are_captured() -> None:
    """reasoning_content 与 usage（含 reasoning_tokens）都要保留。"""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
    )
    client, _ = make_client(
        [
            make_chunk(reasoning="think", finish_reason=None),
            make_chunk(content="answer", finish_reason="stop", usage=usage),
        ]
    )
    events = list(client.stream([UserMessage(content="hi")]))
    assert [event.delta for event in events if isinstance(event, ThinkingDeltaEvent)] == ["think"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.message.reasoning_content == "think"
    assert done.message.usage is not None
    assert done.message.usage.reasoning_tokens == 3


def test_tool_call_arguments_are_assembled() -> None:
    """跨 chunk 的参数分片要按 index 拼装并解析为 dict。"""
    chunks = [
        make_chunk(
            tool_calls=[tool_delta(0, "call_1", "read_file", '{"pa')],
            finish_reason=None,
        ),
        make_chunk(
            tool_calls=[tool_delta(0, None, None, 'th": "a.py"}')],
            finish_reason="tool_calls",
        ),
    ]
    client, _ = make_client(chunks)
    events = list(client.stream([UserMessage(content="read")], tools=[READ_FILE_SCHEMA]))
    ends = [event for event in events if isinstance(event, ToolCallEndEvent)]
    assert len(ends) == 1
    assert ends[0].tool_call.name == "read_file"
    assert ends[0].tool_call.arguments == {"path": "a.py"}
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.message.stop_reason == "tool_calls"


def test_invalid_tool_arguments_yield_error_event() -> None:
    """非法 JSON 参数显式报错，禁止静默兜底成空对象。"""
    chunks = [
        make_chunk(
            tool_calls=[tool_delta(0, "call_1", "read_file", "{not json")],
            finish_reason="tool_calls",
        )
    ]
    client, _ = make_client(chunks)
    events = list(client.stream([UserMessage(content="read")], tools=[READ_FILE_SCHEMA]))
    assert isinstance(events[-1], ErrorEvent)
    assert "invalid JSON" in events[-1].message


def test_tools_are_passed_to_sdk() -> None:
    """有工具时以 OpenAI function schema 传给 SDK，且始终开启 stream。"""
    client, completions = make_client([make_chunk(content="ok", finish_reason="stop")])
    list(client.stream([UserMessage(content="hi")], tools=[READ_FILE_SCHEMA]))
    assert completions.last_kwargs is not None
    assert completions.last_kwargs["tools"][0]["function"]["name"] == "read_file"
    assert completions.last_kwargs["stream"] is True


def test_content_filter_is_an_error() -> None:
    """content_filter 属于不可继续的异常停止。"""
    client, _ = make_client([make_chunk(finish_reason="content_filter")])
    events = list(client.stream([UserMessage(content="hi")]))
    assert isinstance(events[-1], ErrorEvent)
    assert "content filter" in events[-1].message


def test_retryable_status_is_retried_then_errors() -> None:
    """429 会被重试，耗尽后产出可重试的 ErrorEvent。"""
    client, completions = make_client(error=status_error(429), failures=3)
    events = list(client.stream([UserMessage(content="hi")]))
    assert completions.calls == 3
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].retryable is True


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 API Key 时在构造期直接报错。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MiniPiError, match="OPENAI_API_KEY"):
        OpenAIClient(model="gpt-test")


def test_to_openai_messages_wire_format() -> None:
    """transcript 转 wire 格式：tool 消息、tool_calls 与空 content 的表示。"""
    wire = to_openai_messages(
        [
            ToolMessage(tool_call_id="c1", name="read_file", content="data"),
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
                stop_reason="tool_calls",
            ),
        ]
    )
    assert wire[0] == {"role": "tool", "tool_call_id": "c1", "content": "data"}
    assert wire[1]["role"] == "assistant"
    assert wire[1]["content"] is None
    assert wire[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'


def test_to_openai_tools_wire_format() -> None:
    """工具 schema 转 OpenAI function 定义。"""
    wire = to_openai_tools([READ_FILE_SCHEMA])
    assert wire[0]["type"] == "function"
    assert wire[0]["function"]["parameters"] == READ_FILE_SCHEMA.parameters
