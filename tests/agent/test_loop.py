"""Loop 完整消息提交、失败边界和工具改动记录测试。"""

from __future__ import annotations

from typing import Any

import pytest

from mini_pi.agent.events import AgentEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.state import AgentState
from mini_pi.llm.openai_client import to_openai_messages
from mini_pi.llm.types import AssistantMessage, Message, ToolMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant, tool_call


def test_assistant_commits_after_delta_and_before_message_end(
    echo_registry: ToolRegistry,
) -> None:
    """流式 delta 不提交；Done 后的完整 assistant 先提交，再发 message_end。"""
    state = AgentState(messages=[UserMessage(content="hello")])
    seen: list[str] = []

    def commit(message: Message) -> None:
        """记录消息提交与事件相对顺序。"""
        assert isinstance(message, AssistantMessage)
        assert [item.role for item in state.messages] == ["user"]
        seen.append("commit")

    def on_event(event: AgentEvent) -> None:
        """记录流式事件顺序。"""
        seen.append(event.type)

    run_loop(
        state,
        FakeLLMClient([assistant("hello")]),
        echo_registry,
        on_event=on_event,
        on_message_commit=commit,
    )

    assert seen.index("message_delta") < seen.index("commit") < seen.index("message_end")
    assert seen.count("commit") == 1


def test_tool_result_commits_after_execution_with_modified_files(
    echo_registry: ToolRegistry,
) -> None:
    """工具结果完整生成后提交，改动文件保存在 ToolMessage 且不进入 Provider wire。"""
    state = AgentState(messages=[UserMessage(content="write")])
    committed: list[Message] = []

    def commit(message: Message) -> None:
        """提交期间工具消息与改动文件尚未更新到内存。"""
        assert message not in state.messages
        if isinstance(message, ToolMessage):
            assert message.content == "hi"
            assert message.modified_files == ["echo/hi.txt"]
            assert state.modified_files == set()
        committed.append(message)

    run_loop(
        state,
        FakeLLMClient([
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant("done"),
        ]),
        echo_registry,
        on_message_commit=commit,
    )

    assert [message.role for message in committed] == ["assistant", "tool", "assistant"]
    assert state.modified_files == {"echo/hi.txt"}
    assert "modified_files" not in to_openai_messages(state.messages)[2]


def test_tool_commit_failure_stops_loop_without_recording_observation(
    echo_registry: ToolRegistry,
) -> None:
    """工具虽已执行，结果写盘失败时不进入历史或改动集合，且不再请求模型。"""
    state = AgentState(messages=[UserMessage(content="write")])
    llm = FakeLLMClient([
        assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
        assistant("unused"),
    ])

    def commit(message: Message) -> None:
        """只在工具结果提交时模拟失败。"""
        if isinstance(message, ToolMessage):
            raise OSError("tool write failed")

    with pytest.raises(OSError, match="tool write failed"):
        run_loop(state, llm, echo_registry, on_message_commit=commit)

    assert [message.role for message in state.messages] == ["user", "assistant"]
    assert state.modified_files == set()
    assert len(llm.calls) == 1


def test_assistant_commit_failure_does_not_append_or_execute(
    echo_registry: ToolRegistry,
) -> None:
    """完整 assistant 提交失败时不能执行它请求的工具。"""
    state = AgentState(messages=[UserMessage(content="write")])

    def fail(message: Message) -> None:
        """模拟 assistant entry 写入失败。"""
        raise OSError("assistant write failed")

    with pytest.raises(OSError, match="assistant write failed"):
        run_loop(
            state,
            FakeLLMClient([assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})])]),
            echo_registry,
            on_message_commit=fail,
        )

    assert [message.role for message in state.messages] == ["user"]
    assert state.modified_files == set()


@pytest.mark.parametrize(
    ("tool_name", "arguments", "stop_reason"),
    [
        ("inspect", {"path": "a.py"}, None),
        ("fail", {"reason": "nope"}, None),
        ("echo", {"text": "partial"}, "length"),
    ],
)
def test_non_modifying_observations_have_no_modified_files(
    echo_registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
    stop_reason: str | None,
) -> None:
    """只读、ToolError 和截断调用都不能虚报修改文件。"""
    state = AgentState(messages=[UserMessage(content="task")])
    committed: list[Message] = []
    run_loop(
        state,
        FakeLLMClient([
            assistant(
                tool_calls=[tool_call("c1", tool_name, arguments)],
                stop_reason=stop_reason,
            ),
            assistant("done"),
        ]),
        echo_registry,
        on_message_commit=committed.append,
    )

    tool_messages = [message for message in committed if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].modified_files == []
    assert state.modified_files == set()
