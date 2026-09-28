"""M7.9.2：git_status 观察工作区状态，且不把观察结果当成“本工具改动的文件”。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mini_pi.errors import ToolError
from mini_pi.llm.types import ToolMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.tools.git_status import GitStatusTool, _parse_status
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """执行 git 命令并返回结果；显式提交身份，避免依赖全局配置。"""
    return subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def git(cwd: Path, *args: str) -> None:
    """执行应当成功的 git 命令，失败即让用例失败。"""
    result = _run_git(cwd, *args)
    assert result.returncode == 0, result.stderr


def branch_name(cwd: Path) -> str:
    """读取当前分支名，避免依赖宿主 init.defaultBranch。"""
    result = _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """已提交四个文件的临时仓库，供四种工作区状态使用。"""
    git(tmp_path, "init")
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (tmp_path / name).write_text(f"old-{name}\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_clean_workspace_reports_branch_and_clean(repo: Path) -> None:
    """干净工作区必须明确写 clean，并提供零改动事实。"""
    result = GitStatusTool(Workspace(repo)).execute()

    assert result.content == (
        f"branch: {branch_name(repo)}\n"
        "clean: no staged, unstaged, untracked or conflicted paths"
    )
    assert result.details == {
        "branch": branch_name(repo),
        "paths": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
    }


def test_untracked_staged_and_unstaged_paths_are_grouped(repo: Path) -> None:
    """未跟踪、已暂存、未暂存（含两种删除）分别落在各自分组里。"""
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")  # 未暂存修改
    (repo / "b.txt").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "b.txt")  # 已暂存修改
    (repo / "c.txt").unlink()  # 未暂存删除
    git(repo, "rm", "d.txt")  # 已暂存删除
    (repo / "new.txt").write_text("new\n", encoding="utf-8")  # 未跟踪

    result = GitStatusTool(Workspace(repo)).execute()

    assert result.content == "\n".join(
        [
            f"branch: {branch_name(repo)}",
            "staged (2):",
            "  M  b.txt",
            "  D  d.txt",
            "unstaged (2):",
            "   M a.txt",
            "   D c.txt",
            "untracked (1):",
            "  ?? new.txt",
        ]
    )
    assert result.details["staged"] == 2
    assert result.details["unstaged"] == 2
    assert result.details["untracked"] == 1
    assert result.details["paths"] == 5


def test_path_staged_and_unstaged_appears_in_both_groups(repo: Path) -> None:
    """同一路径两侧都有改动时两个分组都要出现，不能丢掉工作区侧事实。"""
    (repo / "a.txt").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("then changed again\n", encoding="utf-8")

    result = GitStatusTool(Workspace(repo)).execute()

    assert result.content == "\n".join(
        [
            f"branch: {branch_name(repo)}",
            "staged (1):",
            "  MM a.txt",
            "unstaged (1):",
            "  MM a.txt",
        ]
    )


def test_staged_rename_reports_source_and_target(repo: Path) -> None:
    """重命名按 `原路径 -> 新路径` 展示，-z 下的字段顺序不能读反。"""
    git(repo, "mv", "a.txt", "renamed.txt")

    result = GitStatusTool(Workspace(repo)).execute()

    assert "  R  a.txt -> renamed.txt" in result.content
    assert result.details["staged"] == 1


def test_merge_conflict_is_reported_as_conflicted(repo: Path) -> None:
    """合并冲突既不是暂存也不是未暂存改动，必须单独报告。"""
    git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "commit", "-am", "feature change")
    git(repo, "checkout", "-")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    git(repo, "commit", "-am", "base change")
    merge = _run_git(repo, "merge", "feature")
    assert merge.returncode != 0, "构造冲突失败，合并意外成功"

    result = GitStatusTool(Workspace(repo)).execute()

    assert "conflicted (1):" in result.content
    assert "  UU a.txt" in result.content
    assert result.details["conflicted"] == 1
    # 冲突条目不能同时被算成普通暂存/未暂存改动
    assert result.details["staged"] == 0
    assert result.details["unstaged"] == 0


def test_ignored_files_are_not_listed(repo: Path) -> None:
    """被 .gitignore 忽略的文件不算工作区改动。"""
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore logs")
    (repo / "debug.log").write_text("noise\n", encoding="utf-8")

    result = GitStatusTool(Workspace(repo)).execute()

    assert "debug.log" not in result.content
    assert result.details["paths"] == 0


def test_workspace_inside_repository_stays_workspace_scoped(tmp_path: Path) -> None:
    """workspace 是仓库子目录时只报告该子树，路径相对 workspace 而不是仓库根。"""
    git(tmp_path, "init")
    (tmp_path / "a.txt").write_text("root\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("inner\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")

    (tmp_path / "a.txt").write_text("root changed\n", encoding="utf-8")  # 仓库根改动不属于本工作区
    (sub / "c.txt").write_text("inner changed\n", encoding="utf-8")
    (sub / "new.txt").write_text("new\n", encoding="utf-8")

    result = GitStatusTool(Workspace(sub)).execute()

    assert "c.txt" in result.content
    assert "new.txt" in result.content
    assert "a.txt" not in result.content
    assert "sub/c.txt" not in result.content
    assert result.details["paths"] == 2


def test_parse_status_rejects_paths_outside_workspace() -> None:
    """子目录 workspace 下路径必须先落在前缀内，否则不能算作本工作区状态。"""
    branch, entries = _parse_status("## main\x00 M sub/a.txt\x00", "sub/")

    assert branch == "main"
    assert entries[0].path == "a.txt"
    assert entries[0].code == " M"

    with pytest.raises(ToolError, match="outside the workspace"):
        _parse_status("## main\x00 M other/a.txt\x00", "sub/")


def test_not_a_repo_is_tool_error(tmp_path: Path) -> None:
    """非 git 工作区是可解释的失败：不能让“观察不到”被当成干净。"""
    tool = GitStatusTool(Workspace(tmp_path))

    with pytest.raises(ToolError, match="not inside a git worktree"):
        tool.execute()


def test_status_observation_declares_no_modified_files(repo: Path) -> None:
    """只读观察工具不得声明 modified_files。"""
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")

    result = GitStatusTool(Workspace(repo)).execute()

    assert result.modified_files == []


def test_status_does_not_pollute_session_metadata(repo: Path) -> None:
    """脏工作区路径只进入 observation 文本，不进入 ToolMessage 的 modifiedFiles。"""
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")  # 未暂存
    (repo / "new.txt").write_text("new\n", encoding="utf-8")  # 未跟踪
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "git_status", {})]),
            assistant("done"),
        ]
    )
    runtime = AgentSession.create(
        cwd=repo,
        llm=llm,
        registry=build_default_registry(Workspace(repo)),
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=repo / "sessions",
    )

    runtime.run("check the working tree")

    loaded = JsonlSession.load(runtime.path)
    entry = next(item for item in loaded.entries if item.message.role == "tool")
    assert isinstance(entry.message, ToolMessage)
    # 观察确实看到了脏路径……
    assert "a.txt" in entry.message.content
    assert "new.txt" in entry.message.content
    # ……但状态观察不等于本工具改动了这些文件
    assert entry.message.modified_files == []
    assert runtime.state.modified_files == set()
    raw = [
        json.loads(line)
        for line in runtime.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_line = next(
        item
        for item in raw
        if item.get("type") == "message" and item["message"]["role"] == "tool"
    )
    assert tool_line["message"]["modified_files"] == []
