"""Agent 四类消息共用提交回调的入口测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.agent.agent import Agent
from mini_pi.llm.types import Message, SystemMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant


def test_run_commits_complete_messages_before_memory_append(tmp_path: Path) -> None:
    """system、user、assistant 按顺序提交，回调执行时消息尚未进入历史。"""
    llm = FakeLLMClient([assistant("done")])
    committed: list[Message] = []
    agent: Agent

    def commit(message: Message) -> None:
        """模拟持久化写入，观察 durable-first 顺序。"""
        assert len(agent.state.messages) == len(committed)
        committed.append(message)

    agent = Agent(llm=llm, registry=ToolRegistry(), cwd=tmp_path, on_message_commit=commit)
    agent.run("hello")

    assert [message.role for message in committed] == ["system", "user", "assistant"]
    assert committed == agent.state.messages


def test_prompt_patch_uses_same_commit_callback(tmp_path: Path) -> None:
    """项目规则变化产生的 patch 也先提交，再进入下一轮模型上下文。"""
    rules = tmp_path / "AGENTS.md"
    rules.write_text("before\n", encoding="utf-8")
    committed: list[Message] = []
    agent = Agent(
        llm=FakeLLMClient([assistant("one"), assistant("two")]),
        registry=ToolRegistry(),
        cwd=tmp_path,
        on_message_commit=committed.append,
    )
    agent.run("first")
    rules.write_text("after\n", encoding="utf-8")

    agent.run("second")

    assert [message.role for message in committed] == [
        "system", "user", "assistant", "system", "user", "assistant"
    ]
    assert isinstance(committed[3], SystemMessage)
    assert committed[3].section_patch is not None
    assert committed == agent.state.messages


def test_system_commit_failure_stops_before_user_and_llm(tmp_path: Path) -> None:
    """首次 system 写盘失败，不能留下内存消息或继续请求模型。"""
    llm = FakeLLMClient([assistant("unused")])

    def fail(message: Message) -> None:
        """模拟存储层失败，异常应原样冒泡。"""
        raise OSError("disk full")

    agent = Agent(llm=llm, registry=ToolRegistry(), cwd=tmp_path, on_message_commit=fail)
    with pytest.raises(OSError, match="disk full"):
        agent.run("hello")

    assert agent.state.messages == []
    assert llm.calls == []


def test_user_commit_failure_keeps_only_committed_system(tmp_path: Path) -> None:
    """user 写盘失败时只保留已提交 system，不进入 Agent Loop。"""
    llm = FakeLLMClient([assistant("unused")])

    def commit(message: Message) -> None:
        """第二条消息模拟写盘错误。"""
        if isinstance(message, UserMessage):
            raise OSError("user write failed")

    agent = Agent(llm=llm, registry=ToolRegistry(), cwd=tmp_path, on_message_commit=commit)
    with pytest.raises(OSError, match="user write failed"):
        agent.run("hello")

    assert [message.role for message in agent.state.messages] == ["system"]
    assert llm.calls == []
