"""M7.6c：工具轮之间的自动压缩（M7.6a 钩子 + M7.5 事务）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_pi.context.policy import KNOWN_CONTEXT_WINDOWS, ContextPolicy
from mini_pi.context.projection import SUMMARY_TAG
from mini_pi.errors import LLMError
from mini_pi.llm.types import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant, tool_call

# 小窗口模型：阈值 = 40000 - 8192 = 31808 token，保留预算沿用默认 20000
_SMALL_WINDOW_MODEL = "test-small-window-tool-turn"
_SMALL_WINDOW = 40_000

# 约 25000 token 的旧回复：单独不到阈值，与一个工具结果相加后才越过阈值
_OLD_REPLY = "y" * 100_000
# 约 15000 token 的工具结果：本轮 turn 未超保留预算，切点应落在 user 边界
_TOOL_OUTPUT = "x" * 60_000
# 约 25000 token：单个工具轮就超过保留预算，用于验证第二次压缩的增量更新
_REPEAT_TOOL_OUTPUT = "z" * 100_000


class _DumpArgs(BaseModel):
    """无参数工具的参数模型。"""


class LargeOutputTool(Tool):
    """固定大输出工具；计数执行次数，用于证明压缩不会重放已执行的工具。"""

    name = "dump"
    description = "Return a fixed large output."
    args_model = _DumpArgs

    def __init__(self, content: str = _TOOL_OUTPUT) -> None:
        """固定每次的输出内容，并从 0 开始计数。"""
        self.content = content
        self.calls = 0

    def execute(self) -> ToolResult:
        """返回固定输出并累加计数；不声明 modified_files。"""
        self.calls += 1
        return ToolResult(content=self.content)


def _session(
    tmp_path: Path, llm: FakeLLMClient, tool: LargeOutputTool, *, model: str
) -> AgentSession:
    """创建持久会话；模型决定窗口策略是否生效，工具是唯一的工具轮来源。"""
    registry = ToolRegistry()
    registry.register(tool)
    return AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
    )


def _small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """把测试模型登记进已知窗口表，走真实的 M7.4g 策略判定。"""
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, _SMALL_WINDOW_MODEL, _SMALL_WINDOW)


def _entries(runtime: AgentSession) -> tuple[SessionEntry, ...]:
    """重新加载 JSONL，按磁盘事实断言。"""
    return JsonlSession.load(runtime.path).entries


def _kinds(runtime: AgentSession) -> list[str]:
    """活动链 entry 的类型名，用于确认追加顺序与数量。"""
    return [type(entry).__name__ for entry in _entries(runtime)]


def _compactions(runtime: AgentSession) -> list[CompactionEntry]:
    """活动链上的 compaction entry。"""
    return [entry for entry in _entries(runtime) if isinstance(entry, CompactionEntry)]


def _tool_contents(runtime: AgentSession) -> list[str]:
    """活动链上的 tool result 正文；只出现一次即工具未重放。"""
    return [
        entry.message.content
        for entry in _entries(runtime)
        if isinstance(entry, MessageEntry) and isinstance(entry.message, ToolMessage)
    ]


def test_tool_turn_over_threshold_compacts_and_continues_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具轮之间超阈值时压缩一次，同一次 run 以新投影继续，工具不重放。"""
    _small_window(monkeypatch)
    tool = LargeOutputTool()
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("## Goal\n完成"),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    # 工具只执行一次，工具结果原样留在 JSONL；摘要不改写 observation
    assert tool.calls == 1
    assert _tool_contents(runtime) == [_TOOL_OUTPUT]
    assert _kinds(runtime) == [
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "CompactionEntry",
        "MessageEntry",
    ]
    # 切点落在本轮 user 边界：prompt 前的检查未触发，本轮超出的只有工具轮
    entries = _entries(runtime)
    compaction = _compactions(runtime)[0]
    assert compaction.first_kept_entry_id == entries[3].id
    assert isinstance(entries[3], MessageEntry)
    assert isinstance(entries[3].message, UserMessage)
    assert entries[3].message.content == "second"
    # 摘要请求（第 3 次调用）只带压缩前历史，且 tools 必须为 None
    summary_request = llm.calls[2][1]
    assert llm.tools_seen[2] is None
    assert isinstance(summary_request, UserMessage)
    assert "[User]: task" in summary_request.content
    assert f"[Assistant]: {_OLD_REPLY}" in summary_request.content
    assert "second" not in summary_request.content
    # 继续请求（第 4 次调用）携带压缩后的投影：system 快照 + 摘要 + 保留区
    continuation = llm.calls[3]
    assert isinstance(continuation[0], SystemMessage)
    assert isinstance(continuation[1], UserMessage)
    assert SUMMARY_TAG in continuation[1].content
    assert isinstance(continuation[2], UserMessage)
    assert continuation[2].content == "second"
    assert isinstance(continuation[3], AssistantMessage)
    assert continuation[3].tool_calls[0].id == "c1"
    assert isinstance(continuation[4], ToolMessage)
    assert continuation[4].content == _TOOL_OUTPUT
    # 内存投影与磁盘事实一致：同一次 run 的后续请求用的是可重建投影
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)


def test_tool_turn_below_threshold_does_not_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具轮未把投影推过阈值时不压缩：没有摘要请求，也没有 compaction entry。"""
    _small_window(monkeypatch)
    tool = LargeOutputTool("small observation")
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert tool.calls == 1
    assert _compactions(runtime) == []
    assert _tool_contents(runtime) == ["small observation"]
    # 三次调用：两次任务请求 + 工具轮之后的继续请求，没有额外摘要调用
    assert len(llm.calls) == 3


def test_unknown_window_never_compacts_between_tool_turns(tmp_path: Path) -> None:
    """窗口未知时不猜百分比：即使工具轮很大也照常继续，不做压缩。"""
    tool = LargeOutputTool()
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool, model="mystery-model")
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert _compactions(runtime) == []
    assert tool.calls == 1
    assert len(llm.calls) == 3


def test_second_tool_turn_compacts_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个完整工具批次都重新检查：第二次压缩带 previous_summary，不重发已压缩原文。"""
    _small_window(monkeypatch)
    tool = LargeOutputTool(_REPEAT_TOOL_OUTPUT)
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("## Goal\n第一段"),
            assistant(tool_calls=[tool_call("c2", "dump", {})]),
            assistant("## Goal\n第二段"),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert tool.calls == 2
    assert _tool_contents(runtime) == [_REPEAT_TOOL_OUTPUT, _REPEAT_TOOL_OUTPUT]
    assert len(_compactions(runtime)) == 2
    # 第二次摘要走 UPDATE 模板：带上已有摘要，且不再重发第一次已压缩的原文
    second_summary = llm.calls[4][1]
    assert llm.tools_seen[4] is None
    assert isinstance(second_summary, UserMessage)
    assert "<previous-summary>" in second_summary.content
    assert "## Goal\n第一段" in second_summary.content
    assert _OLD_REPLY not in second_summary.content
    # 两次压缩后的投影仍在阈值内：最后一轮请求 = system 快照 + 最新摘要 + 保留区
    final_request = llm.calls[5]
    assert isinstance(final_request[0], SystemMessage)
    assert isinstance(final_request[1], UserMessage)
    assert SUMMARY_TAG in final_request[1].content
    assert isinstance(final_request[2], AssistantMessage)
    assert final_request[2].tool_calls[0].id == "c2"
    assert isinstance(final_request[3], ToolMessage)
    assert final_request[3].content == _REPEAT_TOOL_OUTPUT


def test_summary_failure_keeps_tool_result_without_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要失败时以 agent error 结束：已提交的工具结果保留，不写半成品压缩。

    终止语义（事件、状态保留、不发越界请求）由 M7.6d 的用例覆盖；这里固定
    “工具不重放、投影不半写”的边界。
    """
    _small_window(monkeypatch)
    tool = LargeOutputTool()
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            LLMError("summary down", retryable=False),
        ]
    )
    runtime = _session(tmp_path, llm, tool, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")
    before = runtime.state.messages[:]

    reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "summary down" in reply.error_message
    assert tool.calls == 1
    assert _compactions(runtime) == []
    assert _tool_contents(runtime) == [_TOOL_OUTPUT]
    # 失败点之前的消息都已提交；内存投影未被替换成半成品
    replayed = list(JsonlSession.load(runtime.path).replay().messages)
    assert runtime.state.messages == replayed
    assert runtime.state.messages[: len(before)] == before
    assert _kinds(runtime) == [
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
    ]


def test_fixture_truly_crosses_policy_threshold_only_with_tool_turn() -> None:
    """触发线沿用 M7.4g（窗口减 reserve）：旧回复单独不够，加上工具轮才越线。"""
    policy = ContextPolicy(context_window=_SMALL_WINDOW)

    assert policy.threshold_tokens == _SMALL_WINDOW - policy.reserve_tokens
    assert len(_OLD_REPLY) // 4 < policy.threshold_tokens
    assert (len(_OLD_REPLY) + len(_TOOL_OUTPUT)) // 4 > policy.threshold_tokens
