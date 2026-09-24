"""当前 workspace 的 Session 列表元数据与严格校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_pi.errors import SessionError
from mini_pi.llm.types import SystemMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession, list_session_summaries


def _set_timestamps(path: Path, *, created: str, active: str) -> None:
    """固定创建和活动时间，验证排序不依赖文件名。"""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[0]["timestamp"] = created
    for record in records[1:]:
        record["timestamp"] = active
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def test_list_summaries_uses_activity_model_and_active_compaction(tmp_path: Path) -> None:
    """列表按活动时间排序，并反映活动链模型与摘要事实。"""
    older = JsonlSession.create(
        cwd=tmp_path,
        provider="openai",
        model="gpt-old",
        sessions_root=tmp_path / "sessions",
    )
    older.append_message(
        UserMessage(content="old"),
        provider="deepseek",
        model="deepseek-reasoner",
        step_count=0,
    )
    newer = JsonlSession.create(
        cwd=tmp_path,
        provider="openai",
        model="gpt-new",
        sessions_root=tmp_path / "sessions",
    )
    system = newer.append_message(
        SystemMessage(sections={"preamble": "rules"}),
        provider="openai",
        model="gpt-new",
        step_count=0,
    )
    kept = newer.append_message(
        UserMessage(content="keep"),
        provider="openai",
        model="gpt-new",
        step_count=0,
    )
    newer.append_compaction(
        summary="summary",
        first_kept_entry_id=kept.id,
        tokens_before=100,
        system_message=system.message,
    )
    _set_timestamps(
        older.path,
        created="2026-01-01T00:00:00Z",
        active="2026-01-02T00:00:00Z",
    )
    _set_timestamps(
        newer.path,
        created="2026-01-01T00:00:00Z",
        active="2026-01-03T00:00:00Z",
    )

    summaries = list_session_summaries(tmp_path, sessions_root=tmp_path / "sessions")

    assert [item.id for item in summaries] == [newer.header.id, older.header.id]
    assert (summaries[0].provider, summaries[0].model) == ("openai", "gpt-new")
    assert summaries[0].has_summary is True
    assert (summaries[1].provider, summaries[1].model) == (
        "deepseek",
        "deepseek-reasoner",
    )
    assert summaries[1].has_summary is False


def test_list_summaries_rejects_any_corrupt_candidate(tmp_path: Path) -> None:
    """损坏候选必须让整个列表失败，不能给出看似完整的部分结果。"""
    valid = JsonlSession.create(
        cwd=tmp_path,
        provider="openai",
        model="gpt",
        sessions_root=tmp_path / "sessions",
    )
    bad = valid.path.with_name("broken.jsonl")
    bad.write_text("{broken}\n", encoding="utf-8")

    with pytest.raises(SessionError, match="invalid session candidate") as error:
        list_session_summaries(tmp_path, sessions_root=tmp_path / "sessions")

    assert str(bad) in str(error.value)


def test_list_summaries_without_directory_is_empty(tmp_path: Path) -> None:
    """没有保存记录时只返回空列表，不创建 Session 目录。"""
    root = tmp_path / "sessions"

    assert list_session_summaries(tmp_path, sessions_root=root) == []
    assert not root.exists()
