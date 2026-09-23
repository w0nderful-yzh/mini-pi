"""M7.6d：自动压缩失败时的终止语义与状态保留。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.context.policy import (
    DEFAULT_KEEP_RECENT_TOKENS,
    KNOWN_CONTEXT_WINDOWS,
    ContextPolicy,
)
from mini_pi.context.tokens import estimate_tokens
from mini_pi.errors import LLMError
from mini_pi.llm.types import ToolMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant, tool_call

# 小窗口模型：阈值 = 40000 - 8192 = 31808 token，保留预算沿用默认 20000
_SMALL_WINDOW_MODEL = "test-auto-compact-errors"
_SMALL_WINDOW = 40_000

# 紧窗口模型：阈值 = 24000 - 8192 = 15808，**小于**保留预算，用于构造无切点
_TIGHT_WINDOW_MODEL = "test-auto-compact-errors-tight"
_TIGHT_WINDOW = 24_000

# 约 35000 token：单独就越过小窗口阈值，用于 prompt 前触发的失败
_HUGE_REPLY = "x" * 140_000
# 约 25000 token：与一个工具轮相加后越过小窗口阈值
_OLD_REPLY = "y" * 100_000
# 约 15000 token 的工具结果：只够把工具轮推过阈值
_TOOL_OUTPUT = "x" * 60_000
# 约 50000 token：单个工具轮本身就超过阈值与保留预算
_GIANT_TOOL_OUTPUT = "z" * 200_000
# 约 17500 token 与 30000 token：用于紧窗口下的“全部都在保留预算内”与“压完仍超”
_MEDIUM_REPLY = "w" * 70_000
_BIG_REPLY = "v" * 120_000


class _DumpArgs(BaseModel):
    """无参数工具的参数模型。"""


class GiantOutputTool(Tool):
    """固定大输出工具；计数执行次数，用于证明终止后工具不重放。"""

    name = "dump"
    description = "Return a fixed large output."
    args_model = _DumpArgs

    def __init__(self, content: str) -> None:
        """固定每次的输出内容，并从 0 开始计数。"""
        self.content = content
        self.calls = 0

    def execute(self) -> ToolResult:
        """返回固定输出并累加计数；不声明 modified_files。"""
        self.calls += 1
        return ToolResult(content=self.content)


def _session(
    tmp_path: Path,
    llm: FakeLLMClient,
    *,
    model: str,
    tool: GiantOutputTool | None = None,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentSession:
    """创建持久会话；模型决定窗口策略，工具与事件收集器按需注入。"""
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    return AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
        on_event=on_event,
    )


def _small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """登记小窗口模型：阈值高于保留预算，工具轮可以把投影推过阈值。"""
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, _SMALL_WINDOW_MODEL, _SMALL_WINDOW)


def _tight_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """登记紧窗口模型：阈值低于保留预算，投影越线也可能没有安全切点。"""
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, _TIGHT_WINDOW_MODEL, _TIGHT_WINDOW)


def _entries(runtime: AgentSession) -> tuple[SessionEntry, ...]:
    """重新加载 JSONL，按磁盘事实断言。"""
    return JsonlSession.load(runtime.path).entries


def _compactions(runtime: AgentSession) -> list[CompactionEntry]:
    """活动链上的 compaction entry。"""
    return [entry for entry in _entries(runtime) if isinstance(entry, CompactionEntry)]


def _tool_contents(runtime: AgentSession) -> list[str]:
    """活动链上的 tool result 正文。"""
    return [
        entry.message.content
        for entry in _entries(runtime)
        if isinstance(entry, MessageEntry) and isinstance(entry.message, ToolMessage)
    ]


def _user_contents(runtime: AgentSession) -> list[str]:
    """活动链上的 user 消息正文。"""
    return [
        entry.message.content
        for entry in _entries(runtime)
        if isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage)
    ]


def _last_end(events: list[AgentEvent]) -> AgentEndEvent:
    """取最后一个 agent_end；终止语义只由它表达。"""
    ends = [event for event in events if isinstance(event, AgentEndEvent)]
    assert ends, "run 必须以 agent_end 结束"
    return ends[-1]


def test_tool_turn_summary_failure_ends_run_with_agent_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具轮之间摘要失败：以 agent error 收尾，不再发越界请求，工具不重放。"""
    _small_window(monkeypatch)
    events: list[AgentEvent] = []
    tool = GiantOutputTool(_TOOL_OUTPUT)
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            LLMError("summary down", retryable=False),
            assistant("should never run"),
        ]
    )
    runtime = _session(
        tmp_path, llm, model=_SMALL_WINDOW_MODEL, tool=tool, on_event=events.append
    )
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "automatic compaction failed" in reply.error_message
    assert "summary down" in reply.error_message
    # 第 2 次调用是带着工具的任务请求（prompt 前检查未越线），第 3 次是无工具摘要
    assert llm.tools_seen[1] is not None
    assert llm.tools_seen[2] is None
    # 摘要请求之后没有第 4 次调用：没有携超限上下文继续
    assert len(llm.calls) == 3
    # 事件成对，且以 agent error 收尾
    starts = [event for event in events if isinstance(event, TurnStartEvent)]
    ends = [event for event in events if isinstance(event, TurnEndEvent)]
    assert len(starts) == len(ends) == 2
    final = _last_end(events)
    assert final.reason == "error"
    assert final.error == reply.error_message
    assert events[-1] is final
    # 已提交的工具结果保留，压缩失败不留任何痕迹
    assert tool.calls == 1
    assert _tool_contents(runtime) == [_TOOL_OUTPUT]
    assert _compactions(runtime) == []
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)


def test_prompt_before_context_without_safe_cut_ends_without_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """需要压缩但全部消息都在保留预算内：没有切点，任务不开始也不发请求。"""
    _tight_window(monkeypatch)
    events: list[AgentEvent] = []
    llm = FakeLLMClient([assistant(_MEDIUM_REPLY)])
    runtime = _session(tmp_path, llm, model=_TIGHT_WINDOW_MODEL, on_event=events.append)
    runtime.run("task")
    before_file = runtime.path.read_text(encoding="utf-8")
    before_messages = runtime.state.messages[:]
    policy = ContextPolicy(context_window=_TIGHT_WINDOW)
    estimate = estimate_tokens(before_messages).tokens
    # 夹具前提：确实越过阈值，但全部消息都在保留预算之内，因此没有安全切点
    assert estimate > policy.threshold_tokens
    assert estimate <= DEFAULT_KEEP_RECENT_TOKENS

    reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "no safe cut point" in reply.error_message
    # 唯一的模型调用是 run 1 的请求：没有摘要调用，也没有新的任务请求
    assert len(llm.calls) == 1
    assert runtime.path.read_text(encoding="utf-8") == before_file
    assert runtime.state.messages == before_messages
    assert _user_contents(runtime) == ["task"]
    assert _last_end(events).reason == "error"


def test_prompt_before_fully_compacted_context_ends_without_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """投影已没有可摘要内容且仍超阈值：prompt 前终止，不发送任何请求。"""
    _tight_window(monkeypatch)
    llm = FakeLLMClient([assistant(_BIG_REPLY), assistant("## Goal\n完成")])
    runtime = _session(tmp_path, llm, model=_TIGHT_WINDOW_MODEL)
    runtime.run("task")
    # 手动压缩吸收 user 请求后，保留区只剩单条超预算的 assistant 消息
    execution = runtime.compact(keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS)
    assert execution.result is not None
    before_messages = runtime.state.messages[:]
    policy = ContextPolicy(context_window=_TIGHT_WINDOW)
    assert estimate_tokens(before_messages).tokens > policy.threshold_tokens

    reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "no new messages to summarize" in reply.error_message
    # run 1 的请求与手动压缩的摘要调用之外没有新请求
    assert len(llm.calls) == 2
    assert runtime.state.messages == before_messages
    assert _user_contents(runtime) == ["task"]


def test_tool_turn_that_stays_above_threshold_never_sends_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个工具轮本身超过阈值：压缩成功但投影仍越界，必须终止而不是继续请求。"""
    _small_window(monkeypatch)
    events: list[AgentEvent] = []
    tool = GiantOutputTool(_GIANT_TOOL_OUTPUT)
    llm = FakeLLMClient(
        [
            assistant("ok"),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("## Goal\n完成"),
            assistant("should never run"),
        ]
    )
    runtime = _session(
        tmp_path, llm, model=_SMALL_WINDOW_MODEL, tool=tool, on_event=events.append
    )
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "above the window threshold" in reply.error_message
    # 压缩本身成功了：检查点已提交，保留区是原样的工具轮
    assert len(_compactions(runtime)) == 1
    assert tool.calls == 1
    assert _tool_contents(runtime) == [_GIANT_TOOL_OUTPUT]
    # 摘要调用之后没有第 4 次调用
    assert len(llm.calls) == 3
    assert llm.tools_seen[2] is None
    assert _last_end(events).reason == "error"
    # 内存与磁盘一致：压缩后的投影仍然有效，只是不足以安全继续
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)
    assert len(runtime.state.messages) == 4


def test_compaction_write_failure_keeps_jsonl_and_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败同样以 agent error 结束：JSONL 与内存投影保持压缩前状态。"""
    _small_window(monkeypatch)
    llm = FakeLLMClient([assistant(_HUGE_REPLY), assistant("## Goal\n完成")])
    runtime = _session(tmp_path, llm, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")
    before_file = runtime.path.read_text(encoding="utf-8")
    before_messages = runtime.state.messages[:]

    def fail_append(self: JsonlSession, **kwargs: object) -> None:
        """用 OSError 模拟磁盘写失败；签名与 append_compaction 的关键字参数一致。"""
        raise OSError("disk full")

    monkeypatch.setattr(JsonlSession, "append_compaction", fail_append)

    reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "disk full" in reply.error_message
    assert "automatic compaction failed" in reply.error_message
    # 摘要调用成功、写盘失败后没有第 3 次调用：任务请求没有发出
    assert len(llm.calls) == 2
    assert llm.tools_seen[1] is None
    # JSONL 一字未改，内存投影没有被半成品替换
    assert runtime.path.read_text(encoding="utf-8") == before_file
    assert runtime.state.messages == before_messages
    assert _compactions(runtime) == []
    assert _user_contents(runtime) == ["task"]


def test_failure_keeps_session_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """终止后会话仍可恢复：磁盘事实与内存一致，新进程拿到同一投影。"""
    _small_window(monkeypatch)
    tool = GiantOutputTool(_TOOL_OUTPUT)
    registry = ToolRegistry()
    registry.register(tool)
    llm = FakeLLMClient(
        [
            assistant(_OLD_REPLY),
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            LLMError("summary down", retryable=False),
        ]
    )
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model=_SMALL_WINDOW_MODEL,
        sessions_root=tmp_path / "sessions",
    )
    runtime.run("task")
    reply = runtime.run("second")
    assert reply.stop_reason == "error"

    resumed = AgentSession.resume(
        runtime.path,
        registry=registry,
        llm_factory=lambda provider, model: FakeLLMClient([]),
    )

    assert resumed.state.messages == runtime.state.messages
    assert resumed.state.step_count == runtime.state.step_count


def test_fixture_windows_keep_expected_threshold_relations() -> None:
    """两个夹具窗口的关系固定：小窗口可越线，紧窗口让保留预算大于阈值。"""
    small = ContextPolicy(context_window=_SMALL_WINDOW)
    tight = ContextPolicy(context_window=_TIGHT_WINDOW)

    assert small.threshold_tokens > DEFAULT_KEEP_RECENT_TOKENS
    assert tight.threshold_tokens < DEFAULT_KEEP_RECENT_TOKENS
