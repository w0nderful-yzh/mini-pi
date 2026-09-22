"""CLI 显式恢复与继续最近 Session 的离线验收。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """恢复测试只访问临时 Session，且不读取用户的默认连接。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr(
        "mini_pi.cli.app.load_last_connection",
        lambda: pytest.fail("resume must not load the last connection"),
    )


def saved_session(workspace: Path) -> JsonlSession:
    """创建含完整旧轮次的持久化会话。"""
    session = JsonlSession.create(cwd=workspace, provider="deepseek", model="deepseek-chat")
    session.append_message(
        UserMessage(content="before"), provider="deepseek", model="deepseek-chat", step_count=0
    )
    session.append_message(
        assistant("prior"), provider="deepseek", model="deepseek-chat", step_count=1
    )
    return session


def set_session_timestamps(path: Path, *, created: str, active: str) -> None:
    """控制创建与最后活动时间，验证排序依据而非文件名。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["timestamp"] = created
    entries = [json.loads(line) for line in lines[1:]]
    for entry in entries:
        entry["timestamp"] = active
    path.write_text(
        "\n".join(json.dumps(item) for item in [header, *entries]) + "\n",
        encoding="utf-8",
    )


def test_resume_replays_history_and_appends_to_same_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """跨进程恢复旧轮次，新增消息沿原 leaf 追加而非另建 Session。"""
    session = saved_session(tmp_path)
    old_leaf = session.leaf_id
    observed: list[tuple[str, str | None]] = []
    llm = FakeLLMClient([assistant("after")])
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None: observed.append((provider, model)) or llm,
    )

    result = runner.invoke(app, ["after", "--cwd", str(tmp_path), "--resume", str(session.path)])

    assert result.exit_code == 0, result.output
    assert observed == [("deepseek", "deepseek-chat")]
    assert [
        message.content for message in llm.calls[0] if isinstance(message, UserMessage)
    ] == ["before", "after"]
    loaded = JsonlSession.load(session.path)
    assert loaded.entries[len(session.entries)].parent_id == old_leaf
    assert loaded.entries[-1].message.content == "after"
    assert len(list((tmp_path / "sessions").rglob("*.jsonl"))) == 1
    assert f"Session path: {session.path}" in result.output


def test_resume_explicit_model_override_only_affects_new_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式模型覆盖不改旧 header 或 entry，恢复默认选择不参与决策。"""
    session = saved_session(tmp_path)
    observed: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None: observed.append((provider, model))
        or FakeLLMClient([assistant("after")]),
    )

    result = runner.invoke(
        app, ["after", "--cwd", str(tmp_path), "--resume", str(session.path), "--model", "custom"]
    )

    assert result.exit_code == 0, result.output
    assert observed == [("deepseek", "custom")]
    loaded = JsonlSession.load(session.path)
    assert loaded.header.model == "deepseek-chat"
    assert all(entry.model == "deepseek-chat" for entry in loaded.entries[:2])
    assert all(entry.model == "custom" for entry in loaded.entries[2:])


def test_continue_uses_validated_activity_time_not_creation_or_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """较早创建的会话最近被使用时，应按最后 entry 时间选择。"""
    old = saved_session(tmp_path)
    newer = saved_session(tmp_path)
    old_path = old.path.rename(old.path.with_name("z-old.jsonl"))
    newer_path = newer.path.rename(newer.path.with_name("a-new.jsonl"))
    set_session_timestamps(
        old_path, created="2021-01-01T00:00:00Z", active="2021-01-02T00:00:00Z"
    )
    set_session_timestamps(
        newer_path, created="2020-01-01T00:00:00Z", active="2022-01-01T00:00:00Z"
    )
    llm = FakeLLMClient([assistant("after")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None: llm)

    result = runner.invoke(app, ["after", "--cwd", str(tmp_path), "--continue"])

    assert result.exit_code == 0, result.output
    assert f"Session path: {newer_path}" in result.output
    assert len(JsonlSession.load(old_path).entries) == 2
    assert len(JsonlSession.load(newer_path).entries) > 2


def test_continue_empty_session_uses_header_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没有 entry 的会话仍是合法候选，以 header 时间参与排序。"""
    old = saved_session(tmp_path)
    empty = JsonlSession.create(cwd=tmp_path, provider="deepseek", model="deepseek-chat")
    set_session_timestamps(
        old.path, created="2020-01-01T00:00:00Z", active="2021-01-01T00:00:00Z"
    )
    set_session_timestamps(
        empty.path, created="2022-01-01T00:00:00Z", active="2022-01-01T00:00:00Z"
    )
    llm = FakeLLMClient([assistant("after")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None: llm)

    result = runner.invoke(app, ["after", "--cwd", str(tmp_path), "--continue"])

    assert result.exit_code == 0, result.output
    assert f"Session path: {empty.path}" in result.output
    assert len(JsonlSession.load(old.path).entries) == 2


def test_continue_equal_activity_time_requires_explicit_path(tmp_path: Path) -> None:
    """并列最新无法仅凭时间分出先后，不拿文件名充当排序依据。"""
    first = saved_session(tmp_path)
    second = saved_session(tmp_path)
    for session in (first, second):
        set_session_timestamps(
            session.path, created="2020-01-01T00:00:00Z", active="2021-01-01T00:00:00Z"
        )

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--continue"])

    assert result.exit_code == 1
    assert "share the latest activity time" in result.output
    assert "--resume" in result.output


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--resume", "missing.jsonl", "--continue"], "mutually exclusive"),
        (["--resume", "missing.jsonl", "--no-session"], "cannot be used"),
        (["--continue", "--no-session"], "cannot be used"),
    ],
)
def test_incompatible_flags_fail(
    tmp_path: Path, options: list[str], message: str
) -> None:
    """相互矛盾的启动模式在读取文件或调用模型前被拒绝。"""
    result = runner.invoke(app, ["--cwd", str(tmp_path), *options])
    assert result.exit_code != 0
    assert message in result.output


def test_resume_missing_file_fails_nonzero(tmp_path: Path) -> None:
    """显式路径不存在时不创建新 Session，也不进入 REPL。"""
    path = tmp_path / "missing.jsonl"
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--resume", str(path)])
    assert result.exit_code == 1
    assert "session file does not exist" in result.output
    assert not (tmp_path / "sessions").exists()


def test_resume_corrupt_file_fails_nonzero(tmp_path: Path) -> None:
    """损坏文件直接失败，不当作空 Session。"""
    path = tmp_path / "broken.jsonl"
    path.write_text("{broken}\n", encoding="utf-8")
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--resume", str(path)])
    assert result.exit_code == 1
    assert "invalid session header" in result.output


def test_resume_cwd_mismatch_fails_nonzero(tmp_path: Path) -> None:
    """不同 workspace 的会话即使路径明确，也禁止在当前目录恢复。"""
    other = tmp_path / "other"
    other.mkdir()
    session = saved_session(other)
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--resume", str(session.path)])
    assert result.exit_code == 1
    assert "cwd mismatch" in result.output


def test_continue_without_candidate_fails_nonzero(tmp_path: Path) -> None:
    """没有同 cwd Session 时给出明确错误，不新建空会话。"""
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--continue"])
    assert result.exit_code == 1
    assert "no session found" in result.output
    assert not (tmp_path / "sessions").exists()


def test_continue_rejects_corrupt_candidate_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    """候选中存在损坏文件时 Fail Fast，不能悄悄退回旧历史。"""
    valid = saved_session(tmp_path)
    bad = valid.path.with_name("newer-broken.jsonl")
    bad.write_text("{broken}\n", encoding="utf-8")
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--continue"])
    assert result.exit_code == 1
    assert "invalid session candidate" in result.output
    assert str(bad) in result.output
    assert len(JsonlSession.load(valid.path).entries) == 2
