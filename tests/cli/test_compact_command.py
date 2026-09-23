"""/compact 命令：压缩前后展示、instructions 隔离与 --no-session 拒绝。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.errors import LLMError
from mini_pi.llm.types import AssistantMessage, Usage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()

# 默认保留窗口是 20k token，因此历史里要有一条超过它的长回复才会产生切点
_LONG_REPLY = "x" * 100_000


def _patch_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm: FakeLLMClient
) -> None:
    """固定凭据与会话目录，避免访问真实网络与用户目录。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)


def _task_and_summary_script(
    *extra: AssistantMessage | LLMError,
) -> list[AssistantMessage | LLMError]:
    """两轮任务的回复（第二轮很短），再加调用方追加的摘要回复。

    纯文本回复会结束一轮 run，所以第二轮必须由新的 user 任务触发；这样长回复
    才会落在切点之前，成为被摘要的部分。
    """
    return [assistant(_LONG_REPLY), assistant("done"), *extra]


def _context_tokens(output: str) -> tuple[int, int]:
    """从展示行里取出压缩前后的估算值，用于断言确实变短。"""
    line = next(
        item
        for item in output.splitlines()
        if item.startswith("Current context (estimated): ~")
    )
    before_text, after_text = line.removeprefix("Current context (estimated): ~").split(" → ")
    after_tokens = int(after_text.removeprefix("~").split(" /")[0])
    return int(before_text), after_tokens


def test_compact_reports_estimates_and_keeps_original_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显示前后估算、切点与摘要成本；原始 message entry 保留且投影变短。"""
    usage = Usage(input_tokens=26000, output_tokens=400, total_tokens=26400)
    llm = FakeLLMClient(
        _task_and_summary_script(
            assistant("## Goal\n完成").model_copy(update={"usage": usage})
        )
    )
    _patch_environment(monkeypatch, tmp_path, llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\nsecond\n/compact\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Compaction: summarized 2 messages, kept 2 entries (cut boundary: user)" in result.output
    assert "Summary usage: in 26000 / out 400 tokens" in result.output
    before_tokens, after_tokens = _context_tokens(result.output)
    assert before_tokens > after_tokens
    # 压缩输出不打印完整 Session 路径；只有退出时那一次
    assert result.output.count("Session path:") == 1

    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    session = JsonlSession.load(path)
    # 原始 5 条 message entry 一条不删，压缩只追加 1 条 compaction entry
    assert len(session.entries) == 6
    assert all(isinstance(item, MessageEntry) for item in session.entries[:-1])
    entry = session.entries[-1]
    assert isinstance(entry, CompactionEntry)
    assert entry.summary == "## Goal\n完成"
    assert entry.usage == usage
    kept_start = next(
        item
        for item in session.entries
        if isinstance(item, MessageEntry)
        and isinstance(item.message, UserMessage)
        and item.message.content == "second"
    )
    assert entry.first_kept_entry_id == kept_start.id
    # 投影变短：system 快照 + 摘要 + 保留的第二轮
    replayed = session.replay().messages
    assert len(replayed) == 4
    assert isinstance(replayed[1], UserMessage)
    assert "## Goal\n完成" in replayed[1].content
    assert isinstance(replayed[2], UserMessage) and replayed[2].content == "second"
    assert len(llm.calls) == 3
    assert llm.tools_seen[-1] is None


def test_compact_instructions_only_reach_the_summary_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """instructions 进入本次摘要请求，但不写入 Session 文件。"""
    llm = FakeLLMClient(_task_and_summary_script(assistant("## Goal\n完成")))
    _patch_environment(monkeypatch, tmp_path, llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\nsecond\n/compact 关注登录模块\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    request = llm.calls[-1][1]
    assert isinstance(request, UserMessage)
    assert "Additional focus: 关注登录模块" in request.content
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    assert "关注登录模块" not in path.read_text(encoding="utf-8")


def test_compact_rejects_memory_only_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-session 明确拒绝，不隐式创建 JSONL，也不调用模型。"""
    llm = FakeLLMClient([])
    _patch_environment(monkeypatch, tmp_path, llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/compact\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "/compact requires a saved session" in result.output
    assert not (tmp_path / "sessions").exists()
    assert llm.calls == []


def test_compact_failure_reports_and_leaves_session_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要失败时明确报错，不写 entry，REPL 继续可用。"""
    llm = FakeLLMClient(_task_and_summary_script(LLMError("summary down", retryable=False)))
    _patch_environment(monkeypatch, tmp_path, llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\nsecond\n/compact\n/status\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "compaction failed: summary down" in result.output
    # 失败后 REPL 仍能响应后续命令
    assert "Current context (estimated):" in result.output
    entries = JsonlSession.load(next((tmp_path / "sessions").rglob("*.jsonl"))).entries
    assert len(entries) == 5
    assert all(isinstance(entry, MessageEntry) for entry in entries)
