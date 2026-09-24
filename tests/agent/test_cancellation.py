"""M7.D2：Loop 层用户取消语义（配对、事件、已提交事实保留）。"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel

from mini_pi.agent.events import AgentEndEvent, AgentEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.state import AgentState
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    Message,
    StreamEvent,
    TextDeltaEvent,
    ToolCall,
    ToolMessage,
    ToolSchema,
    UserMessage,
)
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


class InterruptingLLM:
    """脚本化客户端：正文流到一半抛 KeyboardInterrupt，模拟用户按下 Ctrl+C。"""

    def __init__(self) -> None:
        """初始化调用记录，便于断言取消后不再发起请求。"""
        self.calls: list[list[Message]] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """先产出一次正文增量再中断，确认半截内容不会进入历史。"""
        self.calls.append(list(messages))
        yield TextDeltaEvent(delta="partial answer")
        raise KeyboardInterrupt

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """取消场景不提供 complete。"""
        raise AssertionError("complete must not be used")


class InterruptArgs(BaseModel):
    pass


class InterruptTool(Tool):
    """执行时抛 KeyboardInterrupt，模拟工具内部被 Ctrl+C 打断。"""

    name = "interrupt"
    description = "Raise KeyboardInterrupt while executing."
    args_model = InterruptArgs

    def execute(self) -> ToolResult:
        """在工具执行中间中断，验证 Loop 补齐配对。"""
        raise KeyboardInterrupt


class ToolThenInterruptLLM:
    """第一轮请求工具，第二轮流式阶段被中断。"""

    def __init__(self) -> None:
        """记录收到的请求，便于断言取消后不再新增。"""
        self.calls: list[list[Message]] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """按请求次数回放：第一次请求 echo，第二次中断。"""
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            call = ToolCall(id="c1", name="echo", arguments={"text": "working"})
            yield DoneEvent(message=assistant(tool_calls=[call]))
            return
        raise KeyboardInterrupt

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """取消场景不提供 complete。"""
        raise AssertionError("complete must not be used")


def _registry(*tools: Tool) -> ToolRegistry:
    """按给定工具构造 Registry，保持用例内可见的依赖。"""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _end_event(events: list[AgentEvent]) -> AgentEndEvent:
    """取最后一个 agent_end 事件用于断言终止原因。"""
    ends = [event for event in events if isinstance(event, AgentEndEvent)]
    assert ends
    return ends[-1]


def test_interrupt_during_stream_ends_cancelled_without_fake_message() -> None:
    """流式轮被中断：不补假 assistant，turn 事件仍成对，reason=cancelled。"""
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = InterruptingLLM()
    events: list[AgentEvent] = []

    result = run_loop(state, llm, _registry(EchoTool()), max_steps=5, on_event=events.append)

    assert result.stop_reason == "cancelled"
    assert _end_event(events).reason == "cancelled"
    # 半截流内容没有提交：历史里只有已提交的 user 消息
    assert [message.role for message in state.messages] == ["user"]
    types = [event.type for event in events]
    assert types.count("turn_start") == types.count("turn_end") == 1
    # 取消后不得再请求模型
    assert len(llm.calls) == 1


def test_interrupt_in_tool_batch_keeps_pairing_and_skips_remaining() -> None:
    """工具轮中断：当前与剩余调用都补 observation，配对完整且剩余工具不执行。"""
    state = AgentState(messages=[UserMessage(content="run tools")])
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[
                    tool_call("c1", "interrupt", {}),
                    tool_call("c2", "echo", {"text": "should-not-run"}),
                ]
            )
        ]
    )
    events: list[AgentEvent] = []

    result = run_loop(
        state,
        llm,
        _registry(InterruptTool(), EchoTool()),
        max_steps=5,
        on_event=events.append,
    )

    assert result.stop_reason == "cancelled"
    assert _end_event(events).reason == "cancelled"
    assert [message.role for message in state.messages] == ["user", "assistant", "tool", "tool"]
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == ["c1", "c2"]
    assert all(message.is_error for message in tool_messages)
    assert all("cancelled" in message.content for message in tool_messages)
    # 被中断批次里排在后面的工具不执行，也不产生 modified_files
    assert state.modified_files == set()
    starts = [event for event in events if event.type == "tool_execution_start"]
    ends = [event for event in events if event.type == "tool_execution_end"]
    assert [event.tool_call.id for event in starts] == ["c1", "c2"]
    assert [event.tool_call.id for event in ends] == ["c1", "c2"]


def test_interrupt_preserves_results_committed_before_cancel() -> None:
    """先完成的工具结果与 modified_files 必须保留，取消不回滚已提交事实。"""
    state = AgentState(messages=[UserMessage(content="run tools")])
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[
                    tool_call("c1", "echo", {"text": "kept"}),
                    tool_call("c2", "interrupt", {}),
                ]
            )
        ]
    )
    events: list[AgentEvent] = []

    result = run_loop(
        state,
        llm,
        _registry(EchoTool(), InterruptTool()),
        max_steps=5,
        on_event=events.append,
    )

    assert result.stop_reason == "cancelled"
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert tool_messages[0].content == "kept"
    assert tool_messages[0].is_error is False
    assert tool_messages[0].modified_files == ["echo/kept.txt"]
    assert tool_messages[1].is_error is True and "cancelled" in tool_messages[1].content
    # 取消只结束当前 run，不回退 step_count 或已记录的改动
    assert state.step_count == 1
    assert state.modified_files == {"echo/kept.txt"}


def test_interrupt_in_prepare_next_turn_ends_cancelled() -> None:
    """钩子（压缩摘要）被中断：工具结果保留，不发出下一次请求。"""
    state = AgentState(messages=[UserMessage(content="run tool")])
    llm = FakeLLMClient(
        [assistant(tool_calls=[tool_call("c1", "echo", {"text": "done"})]), assistant("next")]
    )
    events: list[AgentEvent] = []

    def interrupt_hook() -> None:
        """模拟工具批次提交后、下一次请求前用户按 Ctrl+C。"""
        raise KeyboardInterrupt

    result = run_loop(
        state,
        llm,
        _registry(EchoTool()),
        max_steps=5,
        on_event=events.append,
        prepare_next_turn=interrupt_hook,
    )

    assert result.stop_reason == "cancelled"
    assert _end_event(events).reason == "cancelled"
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert [message.content for message in tool_messages] == ["done"]
    # 工具只执行一次，且没有为下一轮调用模型
    assert len(llm.calls) == 1


def test_interrupt_carries_last_assistant_message_in_end_event() -> None:
    """第二轮被中断时，agent_end 携带上一轮真实 assistant，便于调用方回看进度。"""
    state = AgentState(messages=[UserMessage(content="first")])
    llm = ToolThenInterruptLLM()
    events: list[AgentEvent] = []

    result = run_loop(state, llm, _registry(EchoTool()), max_steps=5, on_event=events.append)

    assert result.stop_reason == "cancelled"
    end = _end_event(events)
    assert end.reason == "cancelled"
    assert end.message is not None
    assert [call.id for call in end.message.tool_calls] == ["c1"]
