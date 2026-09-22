"""Provider 请求前折叠 SystemMessage 历史的测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient, to_openai_messages
from mini_pi.llm.types import (
    AssistantMessage,
    SectionPatch,
    SystemMessage,
    UserMessage,
)


def system_history() -> list[SystemMessage | UserMessage | AssistantMessage]:
    """构造包含快照、增量、删除与交错对话的历史。"""
    return [
        SystemMessage(
            sections={
                "tools": "old tools",
                "preamble": "You are mini-pi.",
                "rules": "Follow rules.",
            }
        ),
        UserMessage(content="first"),
        AssistantMessage(content="done", reasoning_content="thought"),
        SystemMessage(
            section_patch=[
                SectionPatch(op="set", id="tools", content="new tools"),
                SectionPatch(op="set", id="project_context", content="project rules"),
            ]
        ),
        SystemMessage(section_patch=[SectionPatch(op="delete", id="rules")]),
        UserMessage(content="second"),
    ]


def test_wire_has_one_deterministic_system_prompt() -> None:
    """历史中的 system 消息只折叠为一条，非 system 对话保持原序。"""
    history = system_history()

    wire = to_openai_messages(history)

    assert wire == [
        {
            "role": "system",
            "content": "You are mini-pi.\n\n# Tools\nnew tools\n\n# Project Context\nproject rules\n",
        },
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
    ]
    assert to_openai_messages(history) == wire
    assert history[0].sections is not None and history[0].sections["tools"] == "old tools"


def test_legacy_system_messages_use_last_complete_content() -> None:
    """多个旧版完整 prompt 只保留最新内容，不泄漏历史 system 消息。"""
    wire = to_openai_messages(
        [
            SystemMessage(content="old"),
            UserMessage(content="one"),
            SystemMessage(content="new"),
            UserMessage(content="two"),
        ]
    )

    assert wire == [
        {"role": "system", "content": "new"},
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]


def test_no_system_message_does_not_invent_one() -> None:
    """没有 system 历史时保持原有请求格式。"""
    assert to_openai_messages([UserMessage(content="hello")]) == [
        {"role": "user", "content": "hello"}
    ]


def test_invalid_patch_fails_before_wire_request() -> None:
    """没有结构化快照的 patch 不能被发送为 content=null。"""
    with pytest.raises(ValueError, match="structured snapshot"):
        to_openai_messages(
            [SystemMessage(section_patch=[SectionPatch(op="set", id="tools", content="x")])]
        )


class RecordingCompletions:
    """记录两种 Provider 实际发送给 SDK 的消息。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] | None = None

    def create(self, **kwargs: object) -> object:
        self.messages = kwargs["messages"]  # type: ignore[assignment]
        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        return iter([chunk])


@pytest.mark.parametrize("client_type", [OpenAIClient, DeepSeekClient])
def test_provider_sends_single_system_message(client_type: type[OpenAIClient]) -> None:
    """OpenAI 与 DeepSeek 共用折叠逻辑，DeepSeek 仍回放 reasoning_content。"""
    completions = RecordingCompletions()
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = client_type(model="test-model", api_key="test-key", client=sdk)

    list(client.stream(system_history()))

    assert completions.messages is not None
    assert [item["role"] for item in completions.messages].count("system") == 1
    assert completions.messages[0]["content"] == (
        "You are mini-pi.\n\n# Tools\nnew tools\n\n# Project Context\nproject rules\n"
    )
    assistant = completions.messages[2]
    assert ("reasoning_content" in assistant) is (client_type is DeepSeekClient)
