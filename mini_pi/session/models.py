"""Session JSONL 的严格 Pydantic 数据模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from mini_pi.llm.types import Message, SystemMessage, Usage

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SessionModel(BaseModel):
    """Session 协议基类：字段严格且支持 Python/JSON 两套命名。"""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, frozen=True, strict=True
    )


class SessionHeader(SessionModel):
    """JSONL 首行元数据；version=1 是 mini-pi 的首个持久化版本。"""

    type: Literal["session"]
    version: Literal[1]
    id: UUID
    timestamp: AwareDatetime
    cwd: Path
    provider: NonEmptyString
    model: NonEmptyString

    @field_validator("cwd")
    @classmethod
    def validate_absolute_cwd(cls, value: Path) -> Path:
        """持久化绝对 cwd，避免 resume 受调用进程当前目录影响。"""
        if not value.is_absolute():
            raise ValueError("cwd must be an absolute path")
        return value


class SessionEntryBase(SessionModel):
    """所有 Session entry 共享的树结构字段。"""

    id: UUID
    parent_id: UUID | None = Field(alias="parentId")
    timestamp: AwareDatetime


class MessageEntry(SessionEntryBase):
    """完整消息 entry，同时记录消息产生时使用的 provider/model。"""

    type: Literal["message"]
    provider: NonEmptyString
    model: NonEmptyString
    message: Message


class CompactionEntry(SessionEntryBase):
    """压缩检查点；原始消息仍保留在 JSONL 中。"""

    type: Literal["compaction"]
    summary: NonEmptyString
    first_kept_entry_id: UUID = Field(alias="firstKeptEntryId")
    tokens_before: int = Field(alias="tokensBefore", ge=0)
    system_message: SystemMessage = Field(alias="systemMessage")
    usage: Usage | None = None
    modified_files: list[str] = Field(default_factory=list, alias="modifiedFiles")


SessionEntry = Annotated[MessageEntry | CompactionEntry, Field(discriminator="type")]
