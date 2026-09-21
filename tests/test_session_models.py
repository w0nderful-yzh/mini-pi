from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from mini_pi.llm.types import SystemMessage, Usage, UserMessage
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry, SessionHeader


ENTRY_ADAPTER = TypeAdapter(SessionEntry)


def test_session_models_round_trip_with_json_aliases(tmp_path: Path) -> None:
    """Session 模型使用 JSONL 协议字段名，并完整恢复嵌套消息。"""
    now = datetime.now(timezone.utc)
    message_id = uuid4()
    header = SessionHeader(
        type="session",
        version=1,
        id=uuid4(),
        timestamp=now,
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-chat",
    )
    message = MessageEntry(
        type="message",
        id=message_id,
        parentId=None,
        timestamp=now,
        provider="deepseek",
        model="deepseek-chat",
        message=UserMessage(content="继续任务"),
    )
    compaction = CompactionEntry(
        type="compaction",
        id=uuid4(),
        parentId=message_id,
        timestamp=now,
        summary="Earlier work summary",
        firstKeptEntryId=message_id,
        tokensBefore=1234,
        systemMessage=SystemMessage(content="system"),
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        modifiedFiles=["mini_pi/session/models.py"],
    )

    restored_header = SessionHeader.model_validate_json(header.model_dump_json(by_alias=True))
    restored_message = ENTRY_ADAPTER.validate_json(message.model_dump_json(by_alias=True))
    restored_compaction = ENTRY_ADAPTER.validate_json(compaction.model_dump_json(by_alias=True))

    assert restored_header == header
    assert restored_message == message
    assert restored_compaction == compaction
    assert '"parentId"' in message.model_dump_json(by_alias=True)
    assert '"firstKeptEntryId"' in compaction.model_dump_json(by_alias=True)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "type": "session",
                "version": 2,
                "id": str(uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cwd": "/tmp/project",
                "provider": "openai",
                "model": "gpt-4o-mini",
            },
            "version",
        ),
        (
            {
                "type": "message",
                "id": str(uuid4()),
                "parentId": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "provider": "openai",
                "model": "gpt-4o-mini",
                "message": {"role": "user", "content": "hi"},
                "unexpected": True,
            },
            "extra_forbidden",
        ),
    ],
)
def test_session_models_reject_unknown_version_and_extra_fields(
    payload: dict[str, object], match: str
) -> None:
    """Session 协议不接受未来版本或静默忽略未知字段。"""
    adapter = TypeAdapter(SessionHeader) if payload["type"] == "session" else ENTRY_ADAPTER
    with pytest.raises(ValidationError, match=match):
        adapter.validate_python(payload)


def test_session_models_require_aware_timestamp_and_absolute_cwd(tmp_path: Path) -> None:
    """时间必须带时区，cwd 必须是绝对路径，避免跨进程解释不一致。"""
    common = {
        "type": "session",
        "version": 1,
        "id": uuid4(),
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    with pytest.raises(ValidationError, match="timezone"):
        SessionHeader(timestamp=datetime.now(), cwd=tmp_path, **common)
    with pytest.raises(ValidationError, match="absolute"):
        SessionHeader(
            timestamp=datetime.now(timezone.utc),
            cwd=Path("relative/project"),
            **common,
        )


def test_session_models_reject_blank_protocol_strings(tmp_path: Path) -> None:
    """provider、model 与摘要为空时直接拒绝，不写入不可恢复状态。"""
    with pytest.raises(ValidationError, match="string_too_short"):
        SessionHeader(
            type="session",
            version=1,
            id=uuid4(),
            timestamp=datetime.now(timezone.utc),
            cwd=tmp_path,
            provider="   ",
            model="gpt-4o-mini",
        )


def test_session_models_do_not_coerce_numeric_strings() -> None:
    """协议字段使用严格类型，禁止把字符串 token 数静默转成整数。"""
    payload = {
        "type": "compaction",
        "id": str(uuid4()),
        "parentId": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": "summary",
        "firstKeptEntryId": str(uuid4()),
        "tokensBefore": "123",
        "systemMessage": {"role": "system", "content": "system"},
        "modifiedFiles": [],
    }
    with pytest.raises(ValidationError, match="int_type"):
        ENTRY_ADAPTER.validate_json(json.dumps(payload))
