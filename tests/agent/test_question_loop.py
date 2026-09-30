"""M9.1：关键问询的等待、整批拒绝、取消和提交边界。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from mini_pi.agent.events import AgentEndEvent, AgentEvent, TurnEndEvent, TurnStartEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.state import AgentState
from mini_pi.interaction import InteractionRequest, InteractionResponse
from mini_pi.llm.types import AssistantMessage, Message, ToolMessage, UserMessage
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call


def question() -> AssistantMessage:
    """构造独占批次的问询，不依赖模型推测。"""
    return assistant(tool_calls=[tool_call("q1", "ask_user", {
        "question": "选择方案？", "options": ["兼容", "替换"]
    })])


def test_wait_blocks_model_and_side_effects(tmp_path: Path) -> None:
    """用户提交前只有一次请求且没有修改，提交后才执行下一轮。"""
    entered, release = Event(), Event()

    def answer(request: InteractionRequest) -> InteractionResponse:
        """模拟用户尚未提交，只有测试显式放行才产生回答。"""
        entered.set()
        assert release.wait(5), "answer was not released"
        return InteractionResponse("兼容")

    registry = build_default_registry(Workspace(tmp_path), interaction=answer)
    state = AgentState(messages=[UserMessage(content="fix")])
    llm = FakeLLMClient([
        question(), assistant(tool_calls=[tool_call("w1", "write", {
            "path": "choice.txt", "content": "兼容"
        })]), assistant("verified"),
    ])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_loop, state, llm, registry)
        try:
            assert entered.wait(2)
            assert not future.done()
            assert len(llm.calls) == state.step_count == 1
            assert not (tmp_path / "choice.txt").exists()
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.content == "verified"
    assert (tmp_path / "choice.txt").read_text() == "兼容"
    observation = llm.calls[1][-1]
    assert isinstance(observation, ToolMessage)
    assert json.loads(observation.content) == {"source": "user", "answer": "兼容"}


@pytest.mark.parametrize("position", ["first", "last", "double"])
def test_mixed_question_batch_never_executes_any_call(position: str, tmp_path: Path) -> None:
    """问题无论排在哪都必须先拒绝整批，后续独占问询才能真正执行。"""
    asked: list[InteractionRequest] = []

    def answer(request: InteractionRequest) -> InteractionResponse:
        """仅记录纠正后的独占问询。"""
        asked.append(request)
        return InteractionResponse("兼容")

    q = question().tool_calls[0]
    write = tool_call("w0", "write", {"path": "bad.txt", "content": "unapproved"})
    other = tool_call("q0", "ask_user", {"question": "another?"})
    calls = [q, write] if position == "first" else [write, q]
    if position == "double":
        calls = [q, other]
    retry = assistant(tool_calls=[tool_call("q2", "ask_user", {"question": "retry?"})])
    llm = FakeLLMClient([assistant(tool_calls=calls), retry, assistant("done")])
    state = AgentState(messages=[UserMessage(content="fix")])

    run_loop(state, llm, build_default_registry(Workspace(tmp_path), interaction=answer))

    rejected = [message for message in llm.calls[1] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in rejected] == [call.id for call in calls]
    assert all(message.is_error and "only tool call" in message.content for message in rejected)
    assert not (tmp_path / "bad.txt").exists()
    assert asked == [InteractionRequest("retry?")]


def test_unavailable_host_stops_without_next_request(tmp_path: Path) -> None:
    """缺少交互端时提交成对错误并结束 error，不提交假的 assistant。"""
    llm = FakeLLMClient([question(), assistant("must not guess")])
    state = AgentState(messages=[UserMessage(content="choose")])
    events: list[AgentEvent] = []
    result = run_loop(state, llm, build_default_registry(Workspace(tmp_path)), on_event=events.append)
    assert len(llm.calls) == 1 and result.stop_reason == "error"
    assert [message.role for message in state.messages] == ["user", "assistant", "tool"]
    assert state.messages[-1].is_error
    assert events[-1] == AgentEndEvent(reason="error", message=result, error=result.error_message)
    assert sum(isinstance(event, TurnStartEvent) for event in events) == 1
    assert sum(isinstance(event, TurnEndEvent) for event in events) == 1


@pytest.mark.parametrize("action", ["cancel", "interrupt", "eof"])
def test_question_cancellation_is_paired_and_stops(action: str, tmp_path: Path) -> None:
    """所有正常取消来源都补结果、停止请求并报告 cancelled。"""
    def answer(request: InteractionRequest) -> InteractionResponse:
        """模拟取消按钮、Ctrl+C 或关闭输入。"""
        if action == "interrupt":
            raise KeyboardInterrupt
        if action == "eof":
            raise EOFError
        return InteractionResponse(None)

    llm = FakeLLMClient([question(), assistant("unused")])
    state = AgentState(messages=[UserMessage(content="choose")])
    events: list[AgentEvent] = []
    result = run_loop(
        state, llm, build_default_registry(Workspace(tmp_path), interaction=answer),
        on_event=events.append,
    )
    assert len(llm.calls) == 1 and result.stop_reason == "cancelled"
    assert isinstance(events[-1], AgentEndEvent) and events[-1].reason == "cancelled"
    observation = state.messages[-1]
    assert isinstance(observation, ToolMessage) and observation.tool_call_id == "q1"
    assert observation.is_error and "cancelled" in observation.content


@pytest.mark.parametrize("failed_role", ["assistant", "tool"])
def test_commit_failure_prevents_question_or_followup(failed_role: str, tmp_path: Path) -> None:
    """assistant 写盘失败不能提问，答案写盘失败不能进入内存或请求模型。"""
    asked: list[InteractionRequest] = []

    def answer(request: InteractionRequest) -> InteractionResponse:
        """记录是否实际询问了宿主。"""
        asked.append(request)
        return InteractionResponse("兼容")

    def commit(message: Message) -> None:
        """在指定持久化边界模拟磁盘失败。"""
        if message.role == failed_role:
            raise OSError("disk failed")

    state = AgentState(messages=[UserMessage(content="choose")])
    llm = FakeLLMClient([question(), assistant("unused")])
    with pytest.raises(OSError, match="disk failed"):
        run_loop(
            state, llm, build_default_registry(Workspace(tmp_path), interaction=answer),
            on_message_commit=commit,
        )
    assert len(llm.calls) == 1
    assert len(asked) == (1 if failed_role == "tool" else 0)
    assert not any(isinstance(message, ToolMessage) for message in state.messages)
