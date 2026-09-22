from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from mini_pi.errors import SessionError
from mini_pi.llm.types import SystemMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession, discover_session_files, session_dir_for_cwd
from mini_pi.session.models import MessageEntry


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


def create_session(workspace: Path, sessions_root: Path) -> JsonlSession:
    return JsonlSession.create(
        cwd=workspace,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=sessions_root,
    )


def write_lines(path: Path, lines: list[dict[str, object] | str]) -> None:
    """测试辅助：显式构造合法或损坏的 JSONL。"""
    rendered = [line if isinstance(line, str) else json.dumps(line) for line in lines]
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def valid_header(workspace: Path, *, version: int = 1) -> dict[str, object]:
    return {
        "type": "session",
        "version": version,
        "id": str(uuid4()),
        "timestamp": "2026-09-21T10:00:00+00:00",
        "cwd": str(workspace),
        "provider": "deepseek",
        "model": "deepseek-chat",
    }


def valid_message(
    entry_id: str, parent_id: str | None, *, content: str = "hello"
) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": "2026-09-21T10:00:01+00:00",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "message": {"role": "user", "content": content},
    }


def test_create_append_and_load_round_trip(
    tmp_path: Path, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次创建/追加都 fsync，新的 JsonlSession 对象可完整恢复。"""
    fsync_calls: list[int] = []
    monkeypatch.setattr("mini_pi.session.jsonl.os.fsync", fsync_calls.append)
    session = create_session(tmp_path, sessions_root)
    first = session.append_message(
        UserMessage(content="first"), provider="deepseek", model="deepseek-chat"
    )
    compaction = session.append_compaction(
        summary="summary",
        first_kept_entry_id=first.id,
        tokens_before=120,
        system_message=SystemMessage(content="system"),
        modified_files=["a.py"],
    )

    loaded = JsonlSession.load(session.path, expected_cwd=tmp_path)

    assert len(fsync_calls) == 3  # header + message + compaction
    assert loaded.header == session.header
    assert loaded.entries == session.entries
    assert loaded.leaf_id == compaction.id
    assert session.path.parent == session_dir_for_cwd(tmp_path, sessions_root=sessions_root)


def test_append_advances_leaf_and_uses_previous_leaf_as_parent(
    tmp_path: Path, sessions_root: Path
) -> None:
    """本阶段只追加单链：新 entry 必须成为当前 leaf 的 child。"""
    session = create_session(tmp_path, sessions_root)
    first = session.append_message(
        UserMessage(content="one"), provider="deepseek", model="deepseek-chat"
    )
    second = session.append_message(
        UserMessage(content="two"), provider="deepseek", model="deepseek-chat"
    )

    assert first.parent_id is None
    assert second.parent_id == first.id
    assert session.leaf_id == second.id


def test_append_failure_keeps_memory_state_unchanged(
    tmp_path: Path, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """durable-first：磁盘追加失败时不得提前推进 entries 或 leaf。"""
    session = create_session(tmp_path, sessions_root)
    entries_before = session.entries
    leaf_before = session.leaf_id

    def fail_append(line: str) -> None:
        raise OSError(f"disk full while writing {len(line)} bytes")

    monkeypatch.setattr(session, "_append_line", fail_append)
    with pytest.raises(SessionError, match="disk full"):
        session.append_message(
            UserMessage(content="not durable"),
            provider="deepseek",
            model="deepseek-chat",
        )

    assert session.entries == entries_before
    assert session.leaf_id == leaf_before


def test_discover_session_files_is_workspace_scoped_and_newest_first(
    tmp_path: Path, sessions_root: Path
) -> None:
    """路径发现只返回同一 resolved cwd 下的 JSONL，并按文件名倒序。"""
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    first = create_session(tmp_path, sessions_root)
    second = create_session(tmp_path, sessions_root)
    create_session(other_workspace, sessions_root)

    discovered = discover_session_files(tmp_path, sessions_root=sessions_root)

    assert discovered == sorted([first.path, second.path], reverse=True)


@pytest.mark.parametrize(
    ("lines", "match"),
    [
        (["{not json"], "line 1"),
        ([{"type": "wrong"}], "header"),
    ],
)
def test_load_rejects_malformed_or_wrong_header(
    tmp_path: Path, lines: list[dict[str, object] | str], match: str
) -> None:
    path = tmp_path / "broken.jsonl"
    write_lines(path, lines)
    with pytest.raises(SessionError, match=match):
        JsonlSession.load(path)


def test_load_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "future.jsonl"
    write_lines(path, [valid_header(tmp_path, version=2)])
    with pytest.raises(SessionError, match="line 1"):
        JsonlSession.load(path)


def test_load_rejects_blank_and_partial_last_line(tmp_path: Path) -> None:
    blank = tmp_path / "blank.jsonl"
    blank.write_text(json.dumps(valid_header(tmp_path)) + "\n\n", encoding="utf-8")
    with pytest.raises(SessionError, match="blank line 2"):
        JsonlSession.load(blank)

    partial = tmp_path / "partial.jsonl"
    partial.write_text(json.dumps(valid_header(tmp_path)) + '\n{"type":"message"', encoding="utf-8")
    with pytest.raises(SessionError, match="line 2"):
        JsonlSession.load(partial)


def test_load_rejects_duplicate_id_or_orphan_parent(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.jsonl"
    repeated = str(uuid4())
    write_lines(
        duplicate,
        [
            valid_header(tmp_path),
            valid_message(repeated, None),
            valid_message(repeated, repeated),
        ],
    )
    with pytest.raises(SessionError, match="duplicate entry id"):
        JsonlSession.load(duplicate)

    orphan = tmp_path / "orphan.jsonl"
    write_lines(
        orphan,
        [valid_header(tmp_path), valid_message(str(uuid4()), str(uuid4()))],
    )
    with pytest.raises(SessionError, match="unknown parentId"):
        JsonlSession.load(orphan)


def test_load_rejects_second_root_and_unknown_first_kept_entry(tmp_path: Path) -> None:
    second_root = tmp_path / "second-root.jsonl"
    write_lines(
        second_root,
        [
            valid_header(tmp_path),
            valid_message(str(uuid4()), None),
            valid_message(str(uuid4()), None),
        ],
    )
    with pytest.raises(SessionError, match="only the first entry"):
        JsonlSession.load(second_root)

    session = JsonlSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=tmp_path / "sessions",
    )
    with pytest.raises(SessionError, match="firstKeptEntryId"):
        session.append_compaction(
            summary="summary",
            first_kept_entry_id=uuid4(),
            tokens_before=10,
            system_message=SystemMessage(content="system"),
        )


def test_load_rejects_cwd_mismatch(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    path = tmp_path / "session.jsonl"
    write_lines(path, [valid_header(tmp_path)])

    with pytest.raises(SessionError, match="cwd mismatch"):
        JsonlSession.load(path, expected_cwd=other)


def test_loaded_tree_accepts_prior_parent_and_appends_to_last_leaf(tmp_path: Path) -> None:
    """格式允许未来分支；恢复后的当前 leaf 是文件最后一条 entry。"""
    path = tmp_path / "tree.jsonl"
    root_id = str(uuid4())
    left_id = str(uuid4())
    right_id = str(uuid4())
    write_lines(
        path,
        [
            valid_header(tmp_path),
            valid_message(root_id, None, content="root"),
            valid_message(left_id, root_id, content="left"),
            valid_message(right_id, root_id, content="right"),
        ],
    )

    session = JsonlSession.load(path)
    appended = session.append_message(
        UserMessage(content="after right"), provider="deepseek", model="deepseek-chat"
    )

    assert session.leaf_id == appended.id
    assert appended.parent_id == session.entries[-2].id
    assert isinstance(session.entries[0], MessageEntry)


def test_compaction_first_kept_entry_must_be_on_current_branch(tmp_path: Path) -> None:
    """存在但属于兄弟分支的 message 不能成为当前分支压缩切点。"""
    path = tmp_path / "tree.jsonl"
    root_id = str(uuid4())
    left_id = str(uuid4())
    right_id = str(uuid4())
    write_lines(
        path,
        [
            valid_header(tmp_path),
            valid_message(root_id, None, content="root"),
            valid_message(left_id, root_id, content="left"),
            valid_message(right_id, root_id, content="right"),
        ],
    )
    session = JsonlSession.load(path)

    with pytest.raises(SessionError, match="current branch"):
        session.append_compaction(
            summary="summary",
            first_kept_entry_id=UUID(left_id),
            tokens_before=10,
            system_message=SystemMessage(content="system"),
        )


def test_append_rejects_unpaired_surrogate_without_mutating_state(
    tmp_path: Path, sessions_root: Path
) -> None:
    """终端送来非 UTF-8 字节会解出孤立代理项，必须在落盘前明确失败。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = create_session(workspace, sessions_root)
    bad_text = b"\xe5".decode("utf-8", "surrogateescape")

    with pytest.raises(SessionError, match="surrogate"):
        session.append_message(
            UserMessage(content=bad_text), provider="deepseek", model="deepseek-chat"
        )

    assert session.entries == ()
    assert session.leaf_id is None
    assert len(session.path.read_text(encoding="utf-8").splitlines()) == 1
