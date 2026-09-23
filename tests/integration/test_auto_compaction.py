"""M7.6e：自动压缩的离线端到端（真实工具、真实 JSONL、跨进程恢复）。

本目录不使用 `integration` marker：那个 marker 专指调用真实 API 的用例，默认被
`addopts = -m 'not integration'` 排除。这里的“端到端”指跨层链路：
CLI → AgentSession → Agent Loop → Tool → Workspace → Context → JSONL，全部离线运行。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.context.policy import (
    DEFAULT_KEEP_RECENT_TOKENS,
    KNOWN_CONTEXT_WINDOWS,
    ContextPolicy,
)
from mini_pi.context.projection import SUMMARY_TAG
from mini_pi.context.tokens import estimate_tokens
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.tools.read import ReadTool
from mini_pi.tools.registry import ToolRegistry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call

runner = CliRunner()

# 小窗口模型：阈值 = 40000 - 8192 = 31808 token，保留预算沿用默认 20000
_SMALL_WINDOW_MODEL = "test-e2e-small-window"
_SMALL_WINDOW = 40_000

# 旧回复约 25000 token：单独不到阈值，加上随后的工具轮才越线
_OLD_REPLY = "x" * 100_000
# 工作区里的真实日志文件：1000 行、约 43000 字节，read 后约 11000 token
_LOG_PATH = "build.log"
_LOG_LINE = "build log " + "y" * 29
_LOG_LINES = 1_000
# 真实写入的工作区文件，用于验证 modified_files 跨压缩与跨恢复保留
_NOTES_PATH = "notes.md"
_NOTES_TEXT = "压缩不应丢失写文件的事实\n"


def _log_text() -> str:
    """生成确定性的日志正文；行数与字节数都留在 read 的双限之内。"""
    return "\n".join(f"{_LOG_LINE} {index}" for index in range(_LOG_LINES))


def _registry(workspace: Workspace) -> ToolRegistry:
    """使用 CLI 同款默认工具表，让端到端覆盖真实 read/write 行为。"""
    return build_default_registry(workspace)


def _session(
    tmp_path: Path,
    llm: FakeLLMClient,
    *,
    model: str = _SMALL_WINDOW_MODEL,
    workspace: Workspace | None = None,
) -> AgentSession:
    """在真实工作区上创建持久会话，工具与事件都走生产装配路径。"""
    root = Workspace(tmp_path) if workspace is None else workspace
    return AgentSession.create(
        cwd=root.root,
        llm=llm,
        registry=_registry(root),
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
    )


def _small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """把测试模型登记进已知窗口表，走真实的 M7.4g 策略判定。"""
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, _SMALL_WINDOW_MODEL, _SMALL_WINDOW)


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm: FakeLLMClient
) -> None:
    """固定凭据、会话目录与假客户端，避免访问真实网络与用户目录。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)


def _entries(source: AgentSession | Path) -> tuple[SessionEntry, ...]:
    """从磁盘重新加载 entry；恢复后的断言只看持久化事实。"""
    path = source.path if isinstance(source, AgentSession) else source
    return JsonlSession.load(path).entries


def _compaction(entries: Sequence[SessionEntry]) -> CompactionEntry:
    """活动链上唯一的 compaction entry。"""
    found = [entry for entry in entries if isinstance(entry, CompactionEntry)]
    assert len(found) == 1
    return found[0]


def _message(entry: SessionEntry) -> Message | None:
    """message entry 的消息体；compaction entry 返回 None。"""
    return entry.message if isinstance(entry, MessageEntry) else None


def _assert_tool_pairs(entries: Sequence[SessionEntry]) -> None:
    """每个 tool call id 恰好对应一条 ToolMessage：不重放、不丢失、不悬空。"""
    call_ids = [
        call.id
        for message in (item.message for item in entries if isinstance(item, MessageEntry))
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls
    ]
    result_ids = [
        message.tool_call_id
        for message in (item.message for item in entries if isinstance(item, MessageEntry))
        if isinstance(message, ToolMessage)
    ]
    assert len(result_ids) == len(set(result_ids)), "同一个工具结果被写入了多次"
    assert sorted(result_ids) == sorted(call_ids)


def _e2e_script() -> list[AssistantMessage]:
    """脚本：旧任务回复 → 读日志的工具轮 → 摘要 → 最终回答。"""
    return [
        assistant(_OLD_REPLY),
        assistant(
            tool_calls=[
                tool_call("w1", "write", {"path": _NOTES_PATH, "content": _NOTES_TEXT}),
                tool_call("r1", "read", {"path": _LOG_PATH}),
            ]
        ),
        assistant("## Goal\n整理构建日志"),
        assistant("done"),
    ]


def test_assistant_tool_compact_assistant_runs_every_tool_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整链路：真实工具轮把投影推过阈值，压缩后同一次 run 继续，工具不重放。"""
    _small_window(monkeypatch)
    (tmp_path / _LOG_PATH).write_text(_log_text(), encoding="utf-8")
    llm = FakeLLMClient(_e2e_script())
    runtime = _session(tmp_path, llm)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "done"
    # 真实工具确实执行过：文件被写入、日志全文被读回
    assert (tmp_path / _NOTES_PATH).read_text(encoding="utf-8") == _NOTES_TEXT
    entries = _entries(runtime)
    _assert_tool_pairs(entries)
    tool_messages = [
        item.message
        for item in entries
        if isinstance(item, MessageEntry) and isinstance(item.message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == ["w1", "r1"]
    assert tool_messages[1].content == _log_text()
    # 写文件的事实同时留在 ToolMessage 与 Agent 状态里
    assert tool_messages[0].modified_files == [_NOTES_PATH]
    assert runtime.state.modified_files == {_NOTES_PATH}
    # 摘要只覆盖切点之前的历史：写文件发生在保留区，因此不进摘要的改动清单
    compaction = _compaction(entries)
    assert compaction.modified_files == []
    # 切点落在本轮 user 边界，工具批次整体留在保留区
    assert isinstance(_message(entries[3]), UserMessage)
    assert compaction.first_kept_entry_id == entries[3].id
    # 请求序列：任务请求 → 带工具的任务请求 → 无工具摘要 → 压缩后的继续请求
    assert [call_tools is None for call_tools in llm.tools_seen] == [False, False, True, False]
    summary_request = llm.calls[2][1]
    assert isinstance(summary_request, UserMessage)
    assert "[User]: task" in summary_request.content
    assert "second" not in summary_request.content
    # 继续请求携带的是压缩后投影：system 快照 + 摘要 + 保留区原文
    continuation = llm.calls[3]
    assert isinstance(continuation[0], SystemMessage)
    assert isinstance(continuation[1], UserMessage) and SUMMARY_TAG in continuation[1].content
    assert isinstance(continuation[2], UserMessage) and continuation[2].content == "second"
    kept_tool = [message for message in continuation if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in kept_tool] == ["w1", "r1"]
    assert kept_tool[1].content == _log_text()
    # 内存投影就是磁盘投影
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)


def test_resume_after_compaction_matches_memory_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退出后恢复：投影、步骤与改动与内存一致，并能沿同一 leaf 继续追加。"""
    _small_window(monkeypatch)
    (tmp_path / _LOG_PATH).write_text(_log_text(), encoding="utf-8")
    runtime = _session(tmp_path, FakeLLMClient(_e2e_script()))
    runtime.run("task")
    runtime.run("second")
    before_ids = [entry.id for entry in _entries(runtime)]
    resumed_llm = FakeLLMClient([assistant("continued")])

    resumed = AgentSession.resume(
        runtime.path,
        registry=_registry(Workspace(tmp_path)),
        llm_factory=lambda provider, model: resumed_llm,
    )

    assert resumed.provider == runtime.provider == "deepseek"
    assert resumed.model == runtime.model == _SMALL_WINDOW_MODEL
    assert resumed.state.messages == runtime.state.messages
    assert resumed.state.step_count == runtime.state.step_count
    assert resumed.state.modified_files == runtime.state.modified_files == {_NOTES_PATH}
    # 恢复后的第一次请求就是压缩后投影加新任务：不重放任何旧消息
    assert resumed.run("third").content == "continued"
    request = resumed_llm.calls[0]
    assert request[:-1] == runtime.state.messages
    assert request[-1] == UserMessage(content="third")
    assert not any(
        isinstance(message, AssistantMessage) and message.content == _OLD_REPLY
        for message in request
    )
    # 追加式：旧 entry 一条不少，新 entry 沿同一 leaf 续写，工具配对仍然成立
    after = _entries(resumed)
    assert [entry.id for entry in after[: len(before_ids)]] == before_ids
    assert len(after) == len(before_ids) + 2
    _assert_tool_pairs(after)
    assert after[-2].parent_id == before_ids[-1]


def test_cli_run_exits_then_resume_keeps_compacted_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 跨进程：第一次运行触发自动压缩并退出，--resume 后用的是压缩后投影。"""
    _small_window(monkeypatch)
    (tmp_path / _LOG_PATH).write_text(_log_text(), encoding="utf-8")
    first_llm = FakeLLMClient(_e2e_script())
    _patch_cli(monkeypatch, tmp_path, first_llm)

    first = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner", "-p", "deepseek", "-m", _SMALL_WINDOW_MODEL],
        input="task\nsecond\n/exit\n",
    )

    assert first.exit_code == 0, first.output
    assert "done" in first.output
    assert "Session path:" in first.output
    session_path = next((tmp_path / "sessions").rglob("*.jsonl"))
    entries = _entries(session_path)
    _assert_tool_pairs(entries)
    assert len([item for item in entries if isinstance(item, CompactionEntry)]) == 1

    second_llm = FakeLLMClient([assistant("continued")])
    _patch_cli(monkeypatch, tmp_path, second_llm)
    second = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--resume", str(session_path), "--no-banner"],
        input="third\n/exit\n",
    )

    assert second.exit_code == 0, second.output
    assert "continued" in second.output
    # 恢复后的首个请求：system 快照 + 摘要 + 保留区，且不含被摘要掉的旧回复
    request = second_llm.calls[0]
    assert isinstance(request[0], SystemMessage)
    assert isinstance(request[1], UserMessage) and SUMMARY_TAG in request[1].content
    kept_tool = [message for message in request if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in kept_tool] == ["w1", "r1"]
    assert not any(
        isinstance(message, AssistantMessage) and message.content == _OLD_REPLY
        for message in request
    )
    # 磁盘事实保持一致：原 entry 只增不改，工具配对未破坏
    after = _entries(session_path)
    assert len(after) == len(entries) + 2
    _assert_tool_pairs(after)
    assert (tmp_path / _NOTES_PATH).read_text(encoding="utf-8") == _NOTES_TEXT


def test_fixture_crosses_threshold_only_with_the_tool_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """夹具关系固定：旧回复不到阈值、加上真实日志后越线，工具轮本身在保留预算内。"""
    _small_window(monkeypatch)
    log_text = _log_text()
    # 日志必须能整篇被 read 读回，否则工具轮的规模就不再是夹具假设的值
    assert _LOG_LINES <= ReadTool.max_lines
    assert len(log_text.encode("utf-8")) <= ReadTool.max_bytes
    (tmp_path / _LOG_PATH).write_text(log_text, encoding="utf-8")
    runtime = _session(tmp_path, FakeLLMClient([assistant(_OLD_REPLY)]))
    runtime.run("task")
    policy = ContextPolicy(context_window=_SMALL_WINDOW)
    history = estimate_tokens(runtime.state.messages).tokens
    assert history < policy.threshold_tokens

    tool_result = _registry(Workspace(tmp_path)).execute("read", {"path": _LOG_PATH})
    tool_turn = estimate_tokens(
        [
            UserMessage(content="second"),
            assistant(),
            ToolMessage(tool_call_id="r1", name="read", content=tool_result.content),
        ]
    ).tokens

    assert history + tool_turn > policy.threshold_tokens
    # 工具轮本身在保留预算内，所以存在 user 边界切点，压缩能真正把投影降回去
    assert tool_turn <= DEFAULT_KEEP_RECENT_TOKENS
