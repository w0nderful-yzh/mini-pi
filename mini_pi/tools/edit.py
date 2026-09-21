"""edit 工具：对原文做精确、唯一的文本替换。"""

from __future__ import annotations

import difflib

from pydantic import BaseModel, Field

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.workspace.workspace import Workspace


class EditSpec(BaseModel):
    old_text: str = Field(description="Exact text to replace; must match exactly once in the file.")
    new_text: str = Field(description="Replacement text.")


class EditArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    edits: list[EditSpec] = Field(
        min_length=1,
        description="Non-overlapping replacements applied to the original file.",
    )


class EditTool(Tool):
    name = "edit"
    description = "Apply exact, unique text replacements to an existing file. All edits match the original content and must not overlap. Returns a unified diff."
    args_model = EditArgs

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str, edits: list[EditSpec]) -> ToolResult:
        rel = self._workspace.relative(path)
        resolved = self._workspace.resolve(path)
        if not resolved.is_file():
            raise ToolError(f"not a file: {rel}")
        original = self._workspace.read_text(path)

        # 所有 edit 相对原始内容定位，并记录区间
        spans: list[tuple[int, int, EditSpec]] = []
        for index, edit in enumerate(edits):
            if edit.old_text == "":
                raise ToolArgumentError(f"edit[{index}].old_text must not be empty")
            count = original.count(edit.old_text)
            if count == 0:
                raise ToolArgumentError(f"edit[{index}].old_text not found in {rel}")
            if count > 1:
                # 歧义匹配要求模型补充上下文，而不是替它选择某一处
                raise ToolArgumentError(
                    f"edit[{index}].old_text matches {count} times in {rel}; "
                    "add more context to make it unique"
                )
            start = original.index(edit.old_text)
            spans.append((start, start + len(edit.old_text), edit))

        ordered = sorted(spans, key=lambda item: item[0])
        for (_, end, _), (next_start, _, _) in zip(ordered, ordered[1:]):
            if next_start < end:
                raise ToolArgumentError(f"edits overlap in {rel}")

        # 从后往前应用，避免前面的替换影响后面的偏移
        updated = original
        for start, end, edit in sorted(spans, key=lambda item: item[0], reverse=True):
            updated = updated[:start] + edit.new_text + updated[end:]
        if updated == original:
            raise ToolArgumentError(f"edits produce no change in {rel}")

        self._workspace.write_text(path, updated)
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        return ToolResult(
            content=f"Replaced {len(edits)} block(s) in {rel}.",
            details={"path": rel, "diff": diff},
            modified_files=[rel],
        )
