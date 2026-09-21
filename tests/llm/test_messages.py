"""SystemMessage 结构化快照与 patch 协议测试。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from mini_pi.llm.types import Message, SectionPatch, SystemMessage

MESSAGE_ADAPTER = TypeAdapter(Message)


def test_system_message_accepts_exactly_one_payload() -> None:
    """legacy content、完整 sections、section patch 三种载荷必须互斥。"""
    assert SystemMessage(content="legacy").content == "legacy"
    assert SystemMessage(sections={"preamble": "hello"}).sections == {
        "preamble": "hello"
    }
    patch = [SectionPatch(op="set", id="tools", content="tools")]
    assert SystemMessage(section_patch=patch).section_patch == patch

    with pytest.raises(ValidationError, match="exactly one"):
        SystemMessage()
    with pytest.raises(ValidationError, match="exactly one"):
        SystemMessage(content="legacy", sections={"preamble": "hello"})


def test_section_patch_rejects_unknown_operation_and_invalid_content() -> None:
    """未知操作及 set/delete 载荷不匹配必须在模型边界拒绝。"""
    with pytest.raises(ValidationError, match="literal_error"):
        SectionPatch.model_validate(
            {"op": "merge", "id": "tools", "content": "new"}
        )
    with pytest.raises(ValidationError, match="set operation requires content"):
        SectionPatch(op="set", id="tools")
    with pytest.raises(ValidationError, match="delete operation must not include content"):
        SectionPatch(op="delete", id="tools", content="unexpected")
    with pytest.raises(ValidationError, match="literal_error"):
        SectionPatch.model_validate(
            {"op": "set", "id": "custom", "content": "unexpected"}
        )


def test_system_message_rejects_duplicate_patch_ids() -> None:
    """同一消息内重复修改 section 会产生顺序歧义，直接拒绝。"""
    with pytest.raises(ValidationError, match="duplicate section id"):
        SystemMessage(
            section_patch=[
                SectionPatch(op="set", id="tools", content="first"),
                SectionPatch(op="delete", id="tools"),
            ]
        )


def test_structured_system_message_round_trip() -> None:
    """结构化载荷可通过 Message 判别联合序列化并恢复。"""
    message = SystemMessage(
        section_patch=[
            SectionPatch(op="set", id="project_context", content="rules"),
            SectionPatch(op="delete", id="tools"),
        ]
    )

    restored = MESSAGE_ADAPTER.validate_python(message.model_dump())

    assert restored == message
