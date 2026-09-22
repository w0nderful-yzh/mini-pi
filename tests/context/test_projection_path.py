"""M7.4a：Session 活动分支的 entry path 投影测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from mini_pi.context.projection import project_entry_path
from mini_pi.errors import SessionError
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import MessageEntry

_TIMESTAMP = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def message_entry(entry_id: UUID, parent_id: UUID | None, *, content: str = "task") -> MessageEntry:
    """构造最小 MessageEntry，专注 parent 链形状而非消息语义。"""
    return MessageEntry(
        type="message",
        id=entry_id,
        parent_id=parent_id,
        timestamp=_TIMESTAMP,
        provider="deepseek",
        model="deepseek-chat",
        message=UserMessage(content=content),
    )


def test_projects_root_to_leaf_and_ignores_sibling_branch() -> None:
    """投影只包含 leaf 的祖先，按 root→leaf 顺序。"""
    root = message_entry(uuid4(), None)
    left = message_entry(uuid4(), root.id)
    sibling = message_entry(uuid4(), root.id)
    leaf = message_entry(uuid4(), left.id)

    path = project_entry_path([root, left, sibling, leaf], leaf_id=leaf.id)

    assert [entry.id for entry in path] == [root.id, left.id, leaf.id]
    assert sibling not in path


def test_none_leaf_projects_empty_path() -> None:
    """空会话没有活动分支，返回空路径而不是报错。"""
    assert project_entry_path([], leaf_id=None) == ()
    assert project_entry_path([message_entry(uuid4(), None)], leaf_id=None) == ()


def test_rejects_unknown_leaf() -> None:
    """leaf 不在 entry 集合中必须明确失败。"""
    root = message_entry(uuid4(), None)

    with pytest.raises(SessionError, match="unknown leaf"):
        project_entry_path([root], leaf_id=uuid4())


def test_rejects_orphan_parent() -> None:
    """parent 指向不存在的 entry 是孤儿，不得静默截断路径。"""
    orphan = message_entry(uuid4(), uuid4())

    with pytest.raises(SessionError, match="unknown parentId"):
        project_entry_path([orphan], leaf_id=orphan.id)


def test_rejects_parent_cycle() -> None:
    """parent 链成环时不能无限回溯。"""
    first_id, second_id = uuid4(), uuid4()
    first = message_entry(first_id, second_id)
    second = message_entry(second_id, first_id)

    with pytest.raises(SessionError, match="cycle"):
        project_entry_path([first, second], leaf_id=first_id)


def test_rejects_duplicate_entry_id() -> None:
    """重复 id 会让路径有歧义，必须明确失败。"""
    duplicate = uuid4()
    entries = [message_entry(duplicate, None), message_entry(duplicate, None)]

    with pytest.raises(SessionError, match="duplicate"):
        project_entry_path(entries, leaf_id=duplicate)


def test_input_is_not_mutated_and_result_is_immutable() -> None:
    """投影是纯函数：不改写入参，返回不可变 tuple。"""
    root = message_entry(uuid4(), None)
    leaf = message_entry(uuid4(), root.id)
    entries = [root, leaf]

    path = project_entry_path(entries, leaf_id=leaf.id)

    assert entries == [root, leaf]
    assert isinstance(path, tuple)
    assert path[0] is root and path[1] is leaf


def test_matches_jsonl_active_entries(tmp_path: Path) -> None:
    """JsonlSession 与 projection 使用同一套路径逻辑。"""
    session = JsonlSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=tmp_path / "sessions",
    )
    session.append_message(
        UserMessage(content="first"), provider="deepseek", model="deepseek-chat"
    )
    session.append_message(
        UserMessage(content="second"), provider="deepseek", model="deepseek-chat"
    )

    projected = project_entry_path(session.entries, leaf_id=session.leaf_id)

    assert [entry.id for entry in projected] == [entry.id for entry in session.active_entries()]
