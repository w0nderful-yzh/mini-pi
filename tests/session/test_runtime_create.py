"""AgentSession 创建模式的消息持久化与失败边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_pi.errors import SessionError
from mini_pi.llm.types import AssistantMessage, SystemMessage, ToolMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


def create_runtime(
    workspace: Path,
    sessions_root: Path,
    llm: FakeLLMClient,
    registry: ToolRegistry | None = None,
) -> AgentSession:
    """使用独立 Session 根目录和无网络模型创建运行时。"""
    return AgentSession.create(
        cwd=workspace,
        llm=llm,
        registry=registry or ToolRegistry(),
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=sessions_root,
    )


def test_create_writes_header_and_plain_turn_entries(tmp_path: Path) -> None:
    """创建立即写 header；每轮完整消息按 durable-first 顺序落盘。"""
    llm = FakeLLMClient([assistant("first answer"), assistant("second answer")])
    runtime = create_runtime(tmp_path, tmp_path / "sessions", llm)
    lines_before = runtime.path.read_text(encoding="utf-8").splitlines()
    assert len(lines_before) == 1
    header = json.loads(lines_before[0])
    assert header["provider"] == "deepseek"
    assert header["model"] == "deepseek-chat"
    assert header["cwd"] == str(tmp_path.resolve())

    runtime.run("first task")
    runtime.run("second task")

    loaded = JsonlSession.load(runtime.path, expected_cwd=tmp_path)
    assert [entry.message.role for entry in loaded.entries] == [
        "system", "user", "assistant", "user", "assistant"
    ]
    assert [entry.step_count for entry in loaded.entries] == [0, 0, 1, 1, 2]
    assert all(entry.provider == "deepseek" for entry in loaded.entries)
    assert all(entry.model == "deepseek-chat" for entry in loaded.entries)
    assert loaded.entries[0].parent_id is None
    assert all(
        current.parent_id == previous.id
        for previous, current in zip(loaded.entries, loaded.entries[1:])
    )
    assert loaded.leaf_id == loaded.entries[-1].id
    assert [entry.message for entry in loaded.entries] == runtime.state.messages
    assert isinstance(loaded.entries[0].message, SystemMessage)
    assert isinstance(loaded.entries[1].message, UserMessage)
    assert isinstance(loaded.entries[2].message, AssistantMessage)


def test_tool_turn_persists_result_and_modified_files(tmp_path: Path) -> None:
    """tool result 在执行后独立写入 entry，保留本次修改文件供恢复。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLMClient([
        assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
        assistant("done"),
    ])
    runtime = create_runtime(tmp_path, tmp_path / "sessions", llm, registry)

    runtime.run("echo hi")

    loaded = JsonlSession.load(runtime.path)
    assert [entry.message.role for entry in loaded.entries] == [
        "system", "user", "assistant", "tool", "assistant"
    ]
    tool_message = loaded.entries[3].message
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "hi"
    assert tool_message.modified_files == ["echo/hi.txt"]
    assert loaded.entries[3].step_count == 1
    assert runtime.state.modified_files == {"echo/hi.txt"}
    assert len(llm.calls) == 2


def test_append_failure_keeps_only_durable_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user entry 写盘失败时，不进入内存，也不调用 LLM。"""
    llm = FakeLLMClient([assistant("unused")])
    runtime = create_runtime(tmp_path, tmp_path / "sessions", llm)
    original_append = runtime._session._append_line

    def fail_user(line: str) -> None:
        """保留 system 落盘，拒绝随后 user entry。"""
        if json.loads(line)["message"]["role"] == "user":
            raise OSError("disk full")
        original_append(line)

    monkeypatch.setattr(runtime._session, "_append_line", fail_user)
    with pytest.raises(SessionError, match="disk full"):
        runtime.run("task")

    loaded = JsonlSession.load(runtime.path)
    assert [entry.message.role for entry in loaded.entries] == ["system"]
    assert loaded.leaf_id == loaded.entries[0].id
    assert runtime.state.messages == [loaded.entries[0].message]
    assert llm.calls == []


def test_tool_append_failure_stops_before_next_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具 observation 写入失败后历史、leaf 和修改集合保持在上次提交处。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLMClient([
        assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
        assistant("unused"),
    ])
    runtime = create_runtime(tmp_path, tmp_path / "sessions", llm, registry)
    original_append = runtime._session._append_line

    def fail_tool(line: str) -> None:
        """模拟工具执行完成、结果提交时的磁盘错误。"""
        if json.loads(line)["message"]["role"] == "tool":
            raise OSError("tool append failed")
        original_append(line)

    monkeypatch.setattr(runtime._session, "_append_line", fail_tool)
    with pytest.raises(SessionError, match="tool append failed"):
        runtime.run("echo hi")

    loaded = JsonlSession.load(runtime.path)
    assert [entry.message.role for entry in loaded.entries] == ["system", "user", "assistant"]
    assert [message.role for message in runtime.state.messages] == [
        "system", "user", "assistant"
    ]
    assert runtime.state.modified_files == set()
    assert loaded.leaf_id == loaded.entries[-1].id
    assert len(llm.calls) == 1


def test_project_rule_patch_is_persisted_on_second_run(tmp_path: Path) -> None:
    """M7.2 的 prompt patch 也经同一提交路径成为 parent 链上的 entry。"""
    rules = tmp_path / "AGENTS.md"
    rules.write_text("old\n", encoding="utf-8")
    runtime = create_runtime(
        tmp_path,
        tmp_path / "sessions",
        FakeLLMClient([assistant("one"), assistant("two")]),
    )
    runtime.run("first")
    rules.write_text("new\n", encoding="utf-8")

    runtime.run("second")

    entries = JsonlSession.load(runtime.path).entries
    assert [entry.message.role for entry in entries] == [
        "system", "user", "assistant", "system", "user", "assistant"
    ]
    patch = entries[3].message
    assert isinstance(patch, SystemMessage)
    assert patch.section_patch is not None
    assert patch.section_patch[0].id == "project_context"
    assert entries[3].step_count == 1
