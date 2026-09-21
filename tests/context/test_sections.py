"""Prompt section diff、应用与历史 replay 测试。"""

from __future__ import annotations

import pytest

from mini_pi.context.sections import (
    apply_section_patch,
    diff_sections,
    replay_system_messages,
)
from mini_pi.llm.types import SectionPatch, SystemMessage


def test_diff_reports_add_modify_and_delete_in_stable_order() -> None:
    """先按当前顺序 set 新增/修改项，再按旧顺序追加 delete tombstone。"""
    previous = {
        "preamble": "old",
        "rules": "same",
        "tools": "old tools",
    }
    current = {
        "preamble": "new",
        "environment": "env",
        "rules": "same",
    }

    assert diff_sections(previous, current) == (
        SectionPatch(op="set", id="preamble", content="new"),
        SectionPatch(op="set", id="environment", content="env"),
        SectionPatch(op="delete", id="tools"),
    )


def test_diff_returns_none_when_sections_do_not_change() -> None:
    """内容和顺序相同不生成无意义 patch。"""
    sections = {"preamble": "same", "tools": "tools"}

    assert diff_sections(sections, dict(sections)) is None


def test_apply_patch_returns_new_state_without_mutating_previous() -> None:
    """patch 应用是纯函数，支持新增、替换和删除。"""
    previous = {"preamble": "old", "tools": "tools"}
    patch = (
        SectionPatch(op="set", id="preamble", content="new"),
        SectionPatch(op="set", id="rules", content="rules"),
        SectionPatch(op="delete", id="tools"),
    )

    result = apply_section_patch(previous, patch)

    assert result == {"preamble": "new", "rules": "rules"}
    assert previous == {"preamble": "old", "tools": "tools"}


def test_replay_snapshot_and_patch_is_repeatable() -> None:
    """同一历史重复 replay 得到相同结果且不改写消息载荷。"""
    messages = [
        SystemMessage(sections={"preamble": "hello", "tools": "old"}),
        SystemMessage(
            section_patch=[
                SectionPatch(op="set", id="tools", content="new"),
                SectionPatch(op="set", id="rules", content="rules"),
            ]
        ),
    ]

    first = replay_system_messages(messages)
    second = replay_system_messages(messages)

    assert first == second
    assert first is not None
    assert first.content is None
    assert first.sections == {
        "preamble": "hello",
        "tools": "new",
        "rules": "rules",
    }
    assert messages[0].sections == {"preamble": "hello", "tools": "old"}


def test_replay_preserves_legacy_content() -> None:
    """Phase 1 的纯 content SystemMessage 仍作为完整 opaque prompt 回放。"""
    state = replay_system_messages([SystemMessage(content="legacy prompt")])

    assert state is not None
    assert state.content == "legacy prompt"
    assert state.sections is None


def test_patch_requires_structured_snapshot() -> None:
    """patch 不能应用到空历史或无法分解的 legacy content。"""
    patch_message = SystemMessage(
        section_patch=[SectionPatch(op="set", id="tools", content="tools")]
    )

    with pytest.raises(ValueError, match="structured snapshot"):
        replay_system_messages([patch_message])
    with pytest.raises(ValueError, match="structured snapshot"):
        replay_system_messages([SystemMessage(content="legacy"), patch_message])


def test_apply_patch_rejects_duplicate_or_missing_delete_target() -> None:
    """绕过消息模型直接调用纯函数时仍执行完整 patch 校验。"""
    duplicate = (
        SectionPatch(op="set", id="tools", content="one"),
        SectionPatch(op="set", id="tools", content="two"),
    )
    with pytest.raises(ValueError, match="duplicate section id"):
        apply_section_patch({}, duplicate)
    with pytest.raises(ValueError, match="cannot delete missing section"):
        apply_section_patch({}, (SectionPatch(op="delete", id="tools"),))
