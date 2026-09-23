"""M7.5e：手动压缩的事务提交、原子替换与失败边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.context.projection import SUMMARY_TAG
from mini_pi.context.tokens import estimate_tokens
from mini_pi.errors import LLMError, SessionError
from mini_pi.llm.types import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    Usage,
    UserMessage,
)
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call

# 工具轮（调用 + 结果）约 14 token，末条 assistant 约 1 token
_KEEP_RECENT_TOKENS = 5


def _registry() -> ToolRegistry:
    """只注册 echo：一次完整工具轮，用于验证压缩不重放工具。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def _tool_round_script() -> list[AssistantMessage | LLMError]:
    """跑一次带 echo 工具调用的任务所需的两条回复。"""
    return [
        assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
        assistant("done"),
    ]


def _session(tmp_path: Path, llm: FakeLLMClient) -> AgentSession:
    """在工作区内创建持久会话，默认不挂事件回调。"""
    return AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=_registry(),
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )


def test_compact_appends_entry_and_replaces_projection(tmp_path: Path) -> None:
    """摘要成功后追加 CompactionEntry，内存投影重建为快照 + 摘要 + 保留消息。"""
    usage = Usage(input_tokens=1200, output_tokens=80, total_tokens=1280)
    llm = FakeLLMClient(
        [*_tool_round_script(), assistant("## Goal\n完成").model_copy(update={"usage": usage})]
    )
    runtime = _session(tmp_path, llm)
    runtime.run("hi")
    before_entries = JsonlSession.load(runtime.path).entries
    before_messages = list(runtime.state.messages)
    before_tokens = estimate_tokens(before_messages)

    execution = runtime.compact(keep_recent_tokens=_KEEP_RECENT_TOKENS)

    assert execution.result is not None
    assert execution.result.summary == "## Goal\n完成"
    assert execution.result.usage == usage

    entries = JsonlSession.load(runtime.path).entries
    assert len(entries) == len(before_entries) + 1
    entry = entries[-1]
    assert isinstance(entry, CompactionEntry)
    assert entry.parent_id == before_entries[-1].id
    assert entry.summary == "## Goal\n完成"
    assert entry.tokens_before == before_tokens.tokens
    assert entry.usage == usage
    assert entry.modified_files == ["echo/hi.txt"]
    # 切点落在最后一条 assistant：工具轮进入摘要，保留区只剩该条回复
    assert entry.first_kept_entry_id == before_entries[-1].id
    assert entry.system_message.sections is not None
    # 原始 message entry 一条不删，也没有新增 tool 消息（工具不重放）
    assert [item.id for item in entries[: len(before_entries)]] == [
        item.id for item in before_entries
    ]
    tool_messages = [
        item
        for item in entries
        if isinstance(item, MessageEntry) and isinstance(item.message, ToolMessage)
    ]
    assert len(tool_messages) == 1
    # 投影变短，且与全新进程 resume 出来的结果一致
    assert len(runtime.state.messages) < len(before_messages)
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)
    assert len(llm.calls) == 3
    assert llm.tools_seen[-1] is None


def test_run_after_compact_sends_compacted_projection(tmp_path: Path) -> None:
    """压缩后的下一次 run 发送快照 + 摘要 + 保留消息，不再携带被吸收的工具结果。"""
    llm = FakeLLMClient(
        [*_tool_round_script(), assistant("## Goal\n完成"), assistant("next answer")]
    )
    runtime = _session(tmp_path, llm)
    runtime.run("hi")
    before_messages = list(runtime.state.messages)
    runtime.compact(keep_recent_tokens=_KEEP_RECENT_TOKENS)

    runtime.run("more")

    request = llm.calls[-1]
    assert isinstance(request[0], SystemMessage)
    summary_message = request[1]
    assert isinstance(summary_message, UserMessage)
    assert SUMMARY_TAG in summary_message.content
    assert "## Goal\n完成" in summary_message.content
    assert not any(isinstance(message, ToolMessage) for message in request)
    assert len(request) < len(before_messages) + 1


def test_no_safe_cut_returns_reason_and_changes_nothing(tmp_path: Path) -> None:
    """没有安全切点时只返回原因：不写盘、不发摘要请求、不改投影。"""
    llm = FakeLLMClient(_tool_round_script())
    runtime = _session(tmp_path, llm)
    runtime.run("hi")
    before_file = runtime.path.read_text(encoding="utf-8")
    before_messages = list(runtime.state.messages)

    execution = runtime.compact(keep_recent_tokens=100_000)

    assert execution.result is None
    assert execution.reason.startswith("no safe cut point")
    assert runtime.path.read_text(encoding="utf-8") == before_file
    assert runtime.state.messages == before_messages
    assert len(llm.calls) == 2


def test_summary_failure_leaves_session_and_state_untouched(tmp_path: Path) -> None:
    """摘要失败时既不写 entry，也不改内存投影。"""
    llm = FakeLLMClient([*_tool_round_script(), LLMError("summary down", retryable=False)])
    runtime = _session(tmp_path, llm)
    runtime.run("hi")
    before_file = runtime.path.read_text(encoding="utf-8")
    before_messages = list(runtime.state.messages)

    with pytest.raises(LLMError, match="summary down"):
        runtime.compact(keep_recent_tokens=_KEEP_RECENT_TOKENS)

    assert runtime.path.read_text(encoding="utf-8") == before_file
    assert runtime.state.messages == before_messages


def test_write_failure_keeps_entry_and_projection_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败时 JSONL 与 AgentState 都保持压缩前状态。"""
    llm = FakeLLMClient([*_tool_round_script(), assistant("## Goal\n完成")])
    runtime = _session(tmp_path, llm)
    runtime.run("hi")
    before_file = runtime.path.read_text(encoding="utf-8")
    before_messages = list(runtime.state.messages)
    before_entries = JsonlSession.load(runtime.path).entries

    def fail_append(self: JsonlSession, line: str) -> None:
        """模拟磁盘写入失败。"""
        raise OSError("disk full")

    monkeypatch.setattr(JsonlSession, "_append_line", fail_append)

    with pytest.raises(SessionError, match="failed to append"):
        runtime.compact(keep_recent_tokens=_KEEP_RECENT_TOKENS)

    assert runtime.path.read_text(encoding="utf-8") == before_file
    assert runtime.state.messages == before_messages
    assert len(JsonlSession.load(runtime.path).entries) == len(before_entries)


def test_rebuild_failure_keeps_previous_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重建失败时内存保留旧投影；JSONL 里的完整 entry 仍可被新进程重建。"""
    llm = FakeLLMClient([*_tool_round_script(), assistant("## Goal\n完成")])
    runtime = _session(tmp_path, llm)
    runtime.run("hi")
    before_messages = list(runtime.state.messages)
    real_replay = JsonlSession.replay

    def fail_replay(self: JsonlSession, *, leaf_id: object = None) -> object:
        """模拟提交后重建投影失败。"""
        raise SessionError("rebuild failed")

    monkeypatch.setattr(JsonlSession, "replay", fail_replay)

    with pytest.raises(SessionError, match="rebuild failed"):
        runtime.compact(keep_recent_tokens=_KEEP_RECENT_TOKENS)

    assert runtime.state.messages == before_messages

    # 恢复真实投影后：entry 是完整的，重新加载即可得到压缩后的投影
    monkeypatch.setattr(JsonlSession, "replay", real_replay)
    rebuilt = JsonlSession.load(runtime.path).replay().messages
    assert len(rebuilt) < len(before_messages)
    assert runtime.state.messages != list(rebuilt)
