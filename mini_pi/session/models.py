"""Session JSONL 的严格 Pydantic 数据模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal
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
    """完整消息 entry，记录提交时的模型与累计步骤。"""

    type: Literal["message"]
    provider: NonEmptyString
    model: NonEmptyString
    # M7.1 创建的旧文件没有该字段；新运行时每条消息都会显式写入。
    step_count: int | None = Field(default=None, alias="stepCount", ge=0)
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


class ControlEntry(SessionEntryBase):
    """执行前的宿主审批事实，仅保存身份和指纹，不恢复成授权。"""

    type: Literal["control"]
    target: NonEmptyString
    identity: NonEmptyString
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["allow", "deny"]


class CatalogTool(SessionModel):
    """冻结目录中的非秘密身份与 schema，仅用于历史事实而非恢复授权。"""

    server_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    original_name: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    alias: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    schema_fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    config_fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    access: Literal["read", "approval"]


class ToolCatalogEntry(SessionEntryBase):
    """当前目录落盘事实，模型投影、usage 与摘要明确排除。"""

    type: Literal["tool_catalog"]
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    tools: list[CatalogTool] = Field(max_length=32)


SessionEntry = Annotated[
    MessageEntry | CompactionEntry | ControlEntry | ToolCatalogEntry, Field(discriminator="type")
]
