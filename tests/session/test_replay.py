"""Session 活动 parent 链和未压缩消息状态回放测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from mini_pi.errors import SessionError
from mini_pi.llm.types import AssistantMessage, SystemMessage, ToolMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import MessageEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


def create_jsonl(tmp_path: Path) -> JsonlSession:
    """为每个用例创建独立 JSONL，避免触碰真实用户会话。"""
    return JsonlSession.create(
        cwd=tmp_path,
        provider="openai",
        model="initial-model",
        sessions_root=tmp_path / "sessions",
    )


def test_linear_chain_restores_runtime_state(tmp_path: Path) -> None:
    """两步工具轮恢复消息、累计步骤、文件集合和最后使用的模型。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient([
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant("done"),
        ]),
        registry=registry,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=tmp_path / "sessions",
    )
    runtime.run("echo hi")
    session = JsonlSession.load(runtime.path)

    path = session.active_entries()
    replay = session.replay()

    assert [entry.id for entry in path] == [entry.id for entry in session.entries]
    assert replay.messages == tuple(runtime.state.messages)
    assert replay.step_count == 2
    assert replay.modified_files == frozenset({"echo/hi.txt"})
    assert (replay.provider, replay.model) == ("deepseek", "deepseek-chat")
    # 返回的消息属于恢复投影，不可反向修改 Session 内部 entry。
    replay.messages[1].content = "changed"
    assert session.entries[1].message.content == "echo hi"


def test_non_leaf_branch_replays_only_selected_ancestors(tmp_path: Path) -> None:
    """指定历史 leaf 时只回溯其 parent 链，不混入最后写入的兄弟分支。"""
    session = create_jsonl(tmp_path)
    root = session.append_message(
        UserMessage(content="root"), provider="openai", model="root-model"
    )
    left = session.append_message(
        AssistantMessage(content="left"),
        provider="openai",
        model="left-model",
        step_count=1,
    )
    right = MessageEntry(
        type="message",
        id=uuid4(),
        parentId=root.id,
        timestamp=left.timestamp,
        provider="deepseek",
        model="right-model",
        stepCount=1,
        message=AssistantMessage(content="right"),
    )
    # M7 尚无 fork API，手工写入合法兄弟 entry 验证只读分支选择。
    session.path.write_text(
        session.path.read_text(encoding="utf-8") + right.model_dump_json(by_alias=True) + "\n",
        encoding="utf-8",
    )
    loaded = JsonlSession.load(session.path)

    selected = loaded.active_entries(leaf_id=left.id)
    replay = loaded.replay(leaf_id=left.id)

    assert [entry.id for entry in selected] == [root.id, left.id]
    assert [message.content for message in replay.messages] == ["root", "left"]
    assert replay.step_count == 1
    assert (replay.provider, replay.model) == ("openai", "left-model")
    assert loaded.leaf_id == right.id
    assert [entry.id for entry in loaded.active_entries()] == [root.id, right.id]
    selected[0].message.content = "not root"
    assert loaded.entries[0].message.content == "root"


def test_empty_chain_uses_header_defaults(tmp_path: Path) -> None:
    """只有 header 时返回空消息状态和 header 的模型配置。"""
    session = JsonlSession.load(create_jsonl(tmp_path).path)

    replay = session.replay()

    assert session.active_entries() == ()
    assert replay.messages == ()
    assert replay.step_count == 0
    assert replay.modified_files == frozenset()
    assert (replay.provider, replay.model) == ("openai", "initial-model")


def test_legacy_steps_and_duplicate_modified_files(tmp_path: Path) -> None:
    """旧 entry 缺少 stepCount 时按 assistant 计步，重复文件折叠为集合。"""
    session = create_jsonl(tmp_path)
    session.append_message(SystemMessage(content="system"), provider="openai", model="m")
    session.append_message(UserMessage(content="task"), provider="openai", model="m")
    session.append_message(
        assistant(tool_calls=[
            tool_call("c1", "write", {"path": "a.py"}),
            tool_call("c2", "edit", {"path": "a.py"}),
        ]),
        provider="openai",
        model="m",
    )
    for call_id in ("c1", "c2"):
        session.append_message(
            ToolMessage(
                tool_call_id=call_id,
                name="write",
                content="ok",
                modified_files=["a.py", "common.py"],
            ),
            provider="openai",
            model="m",
        )
    session.append_message(
        AssistantMessage(content="done"), provider="openai", model="final-model"
    )

    replay = JsonlSession.load(session.path).replay()

    assert len(replay.messages) == 6
    assert replay.step_count == 2
    assert replay.modified_files == frozenset({"a.py", "common.py"})
    assert (replay.provider, replay.model) == ("openai", "final-model")


def test_compaction_projects_snapshot_summary_and_kept(tmp_path: Path) -> None:
    """M7.4h：含 compaction 的 replay 返回 system 快照 + 摘要 + 保留消息。"""
    session = create_jsonl(tmp_path)
    first = session.append_message(
        UserMessage(content="task"), provider="openai", model="m"
    )
    checkpoint = session.append_compaction(
        summary="goal kept",
        first_kept_entry_id=first.id,
        tokens_before=100,
        system_message=SystemMessage(content="system"),
    )
    loaded = JsonlSession.load(session.path)

    assert [entry.id for entry in loaded.active_entries()] == [first.id, checkpoint.id]
    replay = loaded.replay()

    assert [message.role for message in replay.messages] == ["system", "user", "user"]
    assert replay.messages[0] == SystemMessage(content="system")
    assert "goal kept" in replay.messages[1].content
    assert replay.messages[2] == UserMessage(content="task")
    assert (replay.provider, replay.model) == ("openai", "m")


def test_unknown_leaf_fails_without_moving_current_leaf(tmp_path: Path) -> None:
    """任意 UUID 不能被当作空分支；拒绝后当前 leaf 保持不变。"""
    session = create_jsonl(tmp_path)
    first = session.append_message(
        UserMessage(content="task"), provider="openai", model="m"
    )

    with pytest.raises(SessionError, match="unknown leaf"):
        session.active_entries(leaf_id=uuid4())

    assert session.leaf_id == first.id
