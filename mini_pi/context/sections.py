"""System prompt section 的 diff、patch 应用与历史回放。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from mini_pi.llm.types import (
    SYSTEM_PROMPT_SECTION_IDS,
    SectionPatch,
    SystemMessage,
    SystemPromptSectionId,
)

SectionMapping = Mapping[SystemPromptSectionId, str]
_VALID_SECTION_IDS = frozenset(SYSTEM_PROMPT_SECTION_IDS)


@dataclass(frozen=True, slots=True)
class SystemPromptState:
    """历史回放后的完整 prompt，保持 legacy content 或结构化 sections 二选一。"""

    content: str | None = None
    sections: dict[SystemPromptSectionId, str] | None = None

    def __post_init__(self) -> None:
        """回放结果必须且只能表示一种完整 prompt 形态。"""
        if (self.content is None) == (self.sections is None):
            raise ValueError("system prompt state requires exactly one payload")


def diff_sections(
    previous: SectionMapping,
    current: SectionMapping,
) -> tuple[SectionPatch, ...] | None:
    """比较两个完整快照，按稳定顺序返回最小 set/delete patch。"""
    previous_copy = _validate_sections(previous)
    current_copy = _validate_sections(current)
    operations: list[SectionPatch] = []

    for section_id, content in current_copy.items():
        if previous_copy.get(section_id) != content:
            operations.append(
                SectionPatch(op="set", id=section_id, content=content)
            )
    for section_id in previous_copy:
        if section_id not in current_copy:
            operations.append(SectionPatch(op="delete", id=section_id))

    return tuple(operations) or None


def apply_section_patch(
    sections: SectionMapping,
    patch: Sequence[SectionPatch],
) -> dict[SystemPromptSectionId, str]:
    """校验并应用 patch，返回新快照且不修改输入。"""
    result = _validate_sections(sections)
    if not patch:
        raise ValueError("section patch must not be empty")
    ids = [operation.id for operation in patch]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate section id in patch")

    for operation in patch:
        if operation.op == "set":
            if operation.content is None:
                raise ValueError("set operation requires content")
            result[operation.id] = operation.content
            continue
        if operation.id not in result:
            raise ValueError(f"cannot delete missing section: {operation.id}")
        del result[operation.id]
    return result


def replay_system_messages(
    messages: Iterable[SystemMessage],
) -> SystemPromptState | None:
    """顺序回放完整快照与 patch；legacy content 作为 opaque 完整快照。"""
    state: SystemPromptState | None = None
    for message in messages:
        if message.content is not None:
            state = SystemPromptState(content=message.content)
            continue
        if message.sections is not None:
            state = SystemPromptState(sections=_validate_sections(message.sections))
            continue
        if state is None or state.sections is None:
            raise ValueError("section patch requires a structured snapshot")
        if message.section_patch is None:
            # SystemMessage 已保证三类载荷互斥；触发表示模型被绕过或被并发改坏。
            raise ValueError("system message has no replayable payload")
        state = SystemPromptState(
            sections=apply_section_patch(state.sections, message.section_patch)
        )
    return state


def _validate_sections(
    sections: SectionMapping,
) -> dict[SystemPromptSectionId, str]:
    """复制完整快照，并在纯函数入口补做运行时边界校验。"""
    result: dict[SystemPromptSectionId, str] = {}
    for section_id, content in sections.items():
        if section_id not in _VALID_SECTION_IDS:
            raise ValueError(f"unknown system prompt section: {section_id}")
        if not isinstance(content, str):
            raise ValueError(f"section content must be text: {section_id}")
        result[section_id] = content
    return result
