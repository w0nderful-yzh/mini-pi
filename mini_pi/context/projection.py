"""活动分支投影：把 Session entry 树还原为 root → leaf 的有序路径。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import UUID

from mini_pi.errors import SessionError

if TYPE_CHECKING:
    # 仅用于类型标注：运行时导入会触发 session 包与 jsonl 的循环依赖。
    from mini_pi.session.models import SessionEntry


def project_entry_path(
    entries: Iterable[SessionEntry],
    *,
    leaf_id: UUID | None,
) -> tuple[SessionEntry, ...]:
    """沿 leaf 的 parent 链投影 entry 路径；纯函数，不修改也不复制入参。

    - `leaf_id is None`（空会话）返回空路径
    - 未知 leaf、孤儿 parent、parent 环、重复 id 都属于损坏状态，明确失败
    """
    index = _index_entries(entries)
    if leaf_id is None:
        return ()
    if leaf_id not in index:
        raise SessionError(f"unknown leaf in session: {leaf_id}")

    path: list[SessionEntry] = []
    seen: set[UUID] = set()
    current_id: UUID | None = leaf_id
    while current_id is not None:
        if current_id in seen:
            raise SessionError(f"cycle in session parent chain: {current_id}")
        seen.add(current_id)
        entry = index.get(current_id)
        if entry is None:
            raise SessionError(f"unknown parentId in session: {current_id}")
        path.append(entry)
        current_id = entry.parent_id
    # 回溯得到 leaf→root，反转成调用方需要的 root→leaf。
    return tuple(reversed(path))


def _index_entries(entries: Iterable[SessionEntry]) -> dict[UUID, SessionEntry]:
    """建立 id 索引；重复 id 会让路径有歧义，按损坏状态处理。"""
    index: dict[UUID, SessionEntry] = {}
    for entry in entries:
        if entry.id in index:
            raise SessionError(f"duplicate entry id in session: {entry.id}")
        index[entry.id] = entry
    return index
