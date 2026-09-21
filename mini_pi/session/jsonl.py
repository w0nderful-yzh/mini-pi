"""追加式 JSONL Session：创建、严格恢复、entry 追加与路径发现。"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from mini_pi.errors import SessionError
from mini_pi.llm.types import Message, SystemMessage, Usage
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry, SessionHeader

_ENTRY_ADAPTER = TypeAdapter(SessionEntry)


def _resolved_workspace(cwd: str | Path, *, require_directory: bool) -> Path:
    """统一 cwd 身份；创建 Session 时要求目录真实存在。"""
    path = Path(cwd).expanduser().resolve()
    if require_directory and not path.is_dir():
        raise SessionError(f"session cwd is not an existing directory: {path}")
    return path


def _sessions_root(path: str | Path | None) -> Path:
    """返回 Session 根目录；显式参数便于测试与嵌入方隔离数据。"""
    if path is not None:
        return Path(path).expanduser().resolve()
    return Path.home() / ".mini-pi" / "sessions"


def session_dir_for_cwd(
    cwd: str | Path, *, sessions_root: str | Path | None = None
) -> Path:
    """按 resolved cwd 的稳定哈希生成 Session 目录，避免路径字符冲突。"""
    workspace = _resolved_workspace(cwd, require_directory=False)
    digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
    return _sessions_root(sessions_root) / digest


def discover_session_files(
    cwd: str | Path, *, sessions_root: str | Path | None = None
) -> list[Path]:
    """发现同一 workspace 的 Session 文件，按文件名从新到旧返回。"""
    directory = session_dir_for_cwd(cwd, sessions_root=sessions_root)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise SessionError(f"session path is not a directory: {directory}")
    return sorted((path for path in directory.glob("*.jsonl") if path.is_file()), reverse=True)


class JsonlSession:
    """管理单个追加式 JSONL 文件及当前 leaf。"""

    def __init__(
        self, *, path: Path, header: SessionHeader, entries: list[SessionEntry]
    ) -> None:
        self._path = path
        self._header = header
        self._entries = entries
        self._by_id = {entry.id: entry for entry in entries}
        self._leaf_id = entries[-1].id if entries else None

    @classmethod
    def create(
        cls,
        *,
        cwd: str | Path,
        provider: str,
        model: str,
        sessions_root: str | Path | None = None,
    ) -> JsonlSession:
        """独占创建新 Session，并同步持久化 header。"""
        workspace = _resolved_workspace(cwd, require_directory=True)
        now = datetime.now(timezone.utc)
        header = SessionHeader(
            type="session",
            version=1,
            id=uuid4(),
            timestamp=now,
            cwd=workspace,
            provider=provider,
            model=model,
        )
        directory = session_dir_for_cwd(workspace, sessions_root=sessions_root)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionError(f"failed to create session directory {directory}: {exc}") from exc
        timestamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        path = directory / f"{timestamp}_{header.id}.jsonl"
        session = cls(path=path, header=header, entries=[])
        try:
            session._write_new_file(header.model_dump_json(by_alias=True))
        except OSError as exc:
            raise SessionError(f"failed to create session file {path}: {exc}") from exc
        return session

    @classmethod
    def load(
        cls, path: str | Path, *, expected_cwd: str | Path | None = None
    ) -> JsonlSession:
        """严格加载完整 JSONL；任何坏行或树结构错误都立即失败。"""
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise SessionError(f"session file does not exist: {file_path}")

        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise SessionError(f"failed to read session file {file_path}: {exc}") from exc
        if not lines:
            raise SessionError(f"session file is empty: {file_path}")

        cls._ensure_non_blank_line(lines[0], line_number=1)
        try:
            header = SessionHeader.model_validate_json(lines[0])
        except ValidationError as exc:
            raise SessionError(f"invalid session header at line 1: {exc}") from exc

        if expected_cwd is not None:
            expected = _resolved_workspace(expected_cwd, require_directory=False)
            actual = header.cwd.expanduser().resolve()
            if actual != expected:
                raise SessionError(f"session cwd mismatch: expected {expected}, found {actual}")

        entries: list[SessionEntry] = []
        known: dict[UUID, SessionEntry] = {}
        for line_number, line in enumerate(lines[1:], start=2):
            cls._ensure_non_blank_line(line, line_number=line_number)
            try:
                entry = _ENTRY_ADAPTER.validate_json(line)
            except ValidationError as exc:
                raise SessionError(f"invalid session entry at line {line_number}: {exc}") from exc
            cls._validate_loaded_entry(entry, known, line_number=line_number)
            known[entry.id] = entry
            entries.append(entry)
        return cls(path=file_path, header=header, entries=entries)

    @staticmethod
    def _ensure_non_blank_line(line: str, *, line_number: int) -> None:
        """空行不是合法 entry，避免损坏文件被静默跳过。"""
        if not line.strip():
            raise SessionError(f"blank line {line_number} in session file")

    @staticmethod
    def _validate_loaded_entry(
        entry: SessionEntry, known: dict[UUID, SessionEntry], *, line_number: int
    ) -> None:
        """验证 entry 树：单根、parent 必须指向更早 entry、id 唯一。"""
        if entry.id in known:
            raise SessionError(f"duplicate entry id at line {line_number}: {entry.id}")
        if not known:
            if entry.parent_id is not None:
                raise SessionError(
                    f"unknown parentId at line {line_number}: {entry.parent_id}"
                )
        elif entry.parent_id is None:
            raise SessionError(
                f"only the first entry may have parentId=null (line {line_number})"
            )
        elif entry.parent_id not in known:
            raise SessionError(f"unknown parentId at line {line_number}: {entry.parent_id}")
        if isinstance(entry, CompactionEntry):
            kept = known.get(entry.first_kept_entry_id)
            if not isinstance(kept, MessageEntry):
                raise SessionError(
                    "compaction firstKeptEntryId must reference an earlier message "
                    f"(line {line_number}): {entry.first_kept_entry_id}"
                )
            if not JsonlSession._is_ancestor(
                entry.first_kept_entry_id, entry.parent_id, known
            ):
                raise SessionError(
                    "compaction firstKeptEntryId must belong to the current branch "
                    f"(line {line_number}): {entry.first_kept_entry_id}"
                )

    @staticmethod
    def _is_ancestor(
        candidate_id: UUID,
        leaf_id: UUID | None,
        entries: dict[UUID, SessionEntry],
    ) -> bool:
        """沿 parent 链判断 candidate 是否属于 leaf 的活动分支。"""
        current_id = leaf_id
        while current_id is not None:
            if current_id == candidate_id:
                return True
            current = entries.get(current_id)
            current_id = current.parent_id if current is not None else None
        return False

    @property
    def path(self) -> Path:
        """当前 JSONL 文件绝对路径。"""
        return self._path

    @property
    def header(self) -> SessionHeader:
        """只读 Session header。"""
        return self._header

    @property
    def entries(self) -> tuple[SessionEntry, ...]:
        """返回不可变 entry 视图，避免绕过 append 破坏 leaf/index。"""
        return tuple(self._entries)

    @property
    def leaf_id(self) -> UUID | None:
        """当前路径 leaf；空 Session 为 None。"""
        return self._leaf_id

    def append_message(
        self,
        message: Message,
        *,
        provider: str,
        model: str,
    ) -> MessageEntry:
        """把完整消息追加为当前 leaf 的 child。"""
        entry = MessageEntry(
            type="message",
            id=uuid4(),
            parentId=self._leaf_id,
            timestamp=datetime.now(timezone.utc),
            provider=provider,
            model=model,
            message=message,
        )
        self._append(entry)
        return entry

    def append_compaction(
        self,
        *,
        summary: str,
        first_kept_entry_id: UUID,
        tokens_before: int,
        system_message: SystemMessage,
        usage: Usage | None = None,
        modified_files: list[str] | None = None,
    ) -> CompactionEntry:
        """追加压缩检查点；firstKeptEntryId 必须指向已有 message。"""
        kept = self._by_id.get(first_kept_entry_id)
        if not isinstance(kept, MessageEntry):
            raise SessionError(
                "compaction firstKeptEntryId must reference an existing message: "
                f"{first_kept_entry_id}"
            )
        if not self._is_ancestor(first_kept_entry_id, self._leaf_id, self._by_id):
            raise SessionError(
                "compaction firstKeptEntryId must belong to the current branch: "
                f"{first_kept_entry_id}"
            )
        entry = CompactionEntry(
            type="compaction",
            id=uuid4(),
            parentId=self._leaf_id,
            timestamp=datetime.now(timezone.utc),
            summary=summary,
            firstKeptEntryId=first_kept_entry_id,
            tokensBefore=tokens_before,
            systemMessage=system_message,
            usage=usage,
            modifiedFiles=modified_files or [],
        )
        self._append(entry)
        return entry

    def _append(self, entry: SessionEntry) -> None:
        """先 fsync 再推进内存 leaf，写失败时保持对象状态不变。"""
        if entry.id in self._by_id:
            raise SessionError(f"duplicate entry id: {entry.id}")
        if entry.parent_id != self._leaf_id:
            raise SessionError(
                f"entry parentId {entry.parent_id} does not match current leaf {self._leaf_id}"
            )
        try:
            self._append_line(entry.model_dump_json(by_alias=True))
        except OSError as exc:
            raise SessionError(f"failed to append session entry to {self._path}: {exc}") from exc
        self._entries.append(entry)
        self._by_id[entry.id] = entry
        self._leaf_id = entry.id

    def _write_new_file(self, line: str) -> None:
        """使用 x 模式避免意外覆盖已有 Session。"""
        with self._path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(f"{line}\n")
            file.flush()
            os.fsync(file.fileno())

    def _append_line(self, line: str) -> None:
        """单行追加并同步到磁盘，确保新进程可立即恢复。"""
        with self._path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(f"{line}\n")
            file.flush()
            os.fsync(file.fileno())
