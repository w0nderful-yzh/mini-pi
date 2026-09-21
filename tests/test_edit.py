from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.edit import EditSpec, EditTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_unique_replacement(workspace: Workspace) -> None:
    """唯一匹配替换成功，返回 diff 并声明 modified_files。"""
    tool = EditTool(workspace)
    result = tool.execute(path="app.py", edits=[EditSpec(old_text="a - b", new_text="a + b")])
    assert (workspace.root / "app.py").read_text(encoding="utf-8").endswith("return a + b\n")
    assert result.content == "Replaced 1 block(s) in app.py."
    assert result.modified_files == ["app.py"]
    assert result.details is not None
    assert "-    return a - b" in result.details["diff"]
    assert "+    return a + b" in result.details["diff"]


def test_multiple_edits_use_original_offsets(workspace: Workspace) -> None:
    """多个 edit 都相对原文定位，前一个 edit 变长不影响后一个。"""
    path = workspace.root / "multi.txt"
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    tool = EditTool(workspace)
    result = tool.execute(
        path="multi.txt",
        edits=[
            EditSpec(old_text="alpha", new_text="ALPHA-LONGER"),
            EditSpec(old_text="gamma", new_text="GAMMA"),
        ],
    )
    assert path.read_text(encoding="utf-8") == "ALPHA-LONGER beta GAMMA\n"
    assert result.content == "Replaced 2 block(s) in multi.txt."


def test_missing_old_text_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="not found"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="nope", new_text="x")])


def test_ambiguous_old_text_is_error(workspace: Workspace) -> None:
    """匹配多次时必须要求更多上下文，不能猜要改哪一处。"""
    path = workspace.root / "dup.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="matches 2 times"):
        tool.execute(path="dup.txt", edits=[EditSpec(old_text="same", new_text="other")])


def test_overlapping_edits_are_error(workspace: Workspace) -> None:
    path = workspace.root / "overlap.txt"
    path.write_text("abcdef\n", encoding="utf-8")
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="overlap"):
        tool.execute(
            path="overlap.txt",
            edits=[
                EditSpec(old_text="abcd", new_text="x"),
                EditSpec(old_text="cdef", new_text="y"),
            ],
        )


def test_empty_old_text_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="must not be empty"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="", new_text="x")])


def test_no_change_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="no change"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="a - b", new_text="a - b")])


def test_missing_file_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    with pytest.raises(ToolError, match="not a file"):
        tool.execute(path="missing.py", edits=[EditSpec(old_text="a", new_text="b")])


def test_edit_through_registry_receives_model_instances(workspace: Workspace) -> None:
    """回归：真实调用链由 Registry 传入 dict 参数，edit 必须能正常处理。"""
    from mini_pi.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(EditTool(workspace))
    result = registry.execute(
        "edit",
        {"path": "app.py", "edits": [{"old_text": "a - b", "new_text": "a + b"}]},
    )
    assert result.content == "Replaced 1 block(s) in app.py."
    assert (workspace.root / "app.py").read_text(encoding="utf-8").endswith("return a + b\n")


def test_edits_still_apply_when_first_edit_changes_length(workspace: Workspace) -> None:
    """第一个 edit 变长时，后续 edit 仍按原文偏移应用。"""
    path = workspace.root / "grow.txt"
    path.write_text("abc def\n", encoding="utf-8")
    tool = EditTool(workspace)
    tool.execute(
        path="grow.txt",
        edits=[
            EditSpec(old_text="abc", new_text="abcdefghij"),
            EditSpec(old_text="def", new_text="DEF"),
        ],
    )
    assert path.read_text(encoding="utf-8") == "abcdefghij DEF\n"
