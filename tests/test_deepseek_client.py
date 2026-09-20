from __future__ import annotations

from types import SimpleNamespace

import pytest

from mini_pi.errors import MiniPiError
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.types import AssistantMessage, DoneEvent, ThinkingDeltaEvent, UserMessage


class FakeCompletions:
    """返回带 reasoning_content 的单个 chunk，并记录请求参数。"""

    def __init__(self) -> None:
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        delta = SimpleNamespace(
            content="answer",
            reasoning_content="thinking",
            tool_calls=None,
        )
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
            usage=None,
        )
        return iter([chunk])


class FakeSDK:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def test_deepseek_replays_reasoning_content() -> None:
    """deepseek-reasoner 要求回放历史 assistant 的 reasoning_content。"""
    completions = FakeCompletions()
    client = DeepSeekClient(model="deepseek-reasoner", api_key="test", client=FakeSDK(completions))
    history = [AssistantMessage(content="old", reasoning_content="old-thought")]
    events = list(client.stream(history))
    assert isinstance(events[-1], DoneEvent)
    assert completions.last_kwargs is not None
    assert completions.last_kwargs["messages"][0]["reasoning_content"] == "old-thought"


def test_deepseek_maps_thought_delta() -> None:
    """DeepSeek 的 reasoning_content 增量映射为 ThinkingDeltaEvent。"""
    client = DeepSeekClient(
        model="deepseek-reasoner", api_key="test", client=FakeSDK(FakeCompletions())
    )
    events = list(client.stream([UserMessage(content="hi")]))
    assert [event.delta for event in events if isinstance(event, ThinkingDeltaEvent)] == [
        "thinking"
    ]


def test_deepseek_requires_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek 使用独立的 DEEPSEEK_API_KEY 环境变量。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MiniPiError, match="DEEPSEEK_API_KEY"):
        DeepSeekClient(model="deepseek-chat")
