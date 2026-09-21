from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.agent.agent import Agent
from mini_pi.agent.prompt import build_system_prompt
from mini_pi.llm.types import AssistantMessage, SystemMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def make_agent(tmp_path: Path, registry: ToolRegistry, script: list) -> Agent:
    return Agent(llm=FakeLLMClient(script), registry=registry, cwd=tmp_path, max_steps=5)


def test_run_injects_system_and_user_messages(tmp_path: Path, registry: ToolRegistry) -> None:
    """首次 run 注入 system prompt，并把用户任务追加为 user 消息。"""
    agent = make_agent(tmp_path, registry, [assistant("ok")])
    result = agent.run("do something")
    assert result.content == "ok"
    assert isinstance(agent.state.messages[0], SystemMessage)
    tools_section = agent.state.messages[0].content.split("# Tools")[1]
    assert "- echo:" in tools_section
    assert "read" not in tools_section
    assert isinstance(agent.state.messages[1], UserMessage)
    assert agent.state.messages[1].content == "do something"


def test_system_message_is_not_duplicated_across_runs(tmp_path: Path, registry: ToolRegistry) -> None:
    """多次 run 共享 transcript，system 消息只注入一次。"""
    agent = make_agent(tmp_path, registry, [assistant("first"), assistant("second")])
    agent.run("one")
    agent.run("two")
    systems = [message for message in agent.state.messages if isinstance(message, SystemMessage)]
    assert len(systems) == 1


def test_reset_clears_state(tmp_path: Path, registry: ToolRegistry) -> None:
    """reset 清空 transcript、步数与改动文件记录。"""
    agent = make_agent(tmp_path, registry, [assistant("ok")])
    agent.run("task")
    agent.reset()
    assert agent.state.messages == []
    assert agent.state.step_count == 0
    assert agent.state.modified_files == set()


def test_set_llm_swaps_client_and_keeps_transcript(tmp_path: Path, registry: ToolRegistry) -> None:
    """替换 LLM 客户端（/connect 换 Key/provider）时保留已有 transcript。"""
    agent = make_agent(tmp_path, registry, [assistant("first")])
    agent.run("one")
    agent.set_llm(FakeLLMClient([assistant("second")]))
    result = agent.run("two")
    assert result.content == "second"
    assert len(agent.state.messages) == 5  # system + user + assistant + user + assistant


def test_empty_task_is_rejected(tmp_path: Path, registry: ToolRegistry) -> None:
    """空任务在入口直接拒绝，不进入 Loop。"""
    agent = make_agent(tmp_path, registry, [])
    with pytest.raises(ValueError, match="empty"):
        agent.run("   ")


def test_invalid_max_steps_is_rejected(tmp_path: Path, registry: ToolRegistry) -> None:
    """max_steps 非法属于配置错误，构造期失败。"""
    with pytest.raises(ValueError, match="max_steps"):
        Agent(llm=FakeLLMClient([]), registry=registry, cwd=tmp_path, max_steps=0)


def test_prompt_contains_environment(tmp_path: Path) -> None:
    """system prompt 包含身份、workspace 根路径与工具清单。"""
    prompt = build_system_prompt(cwd=tmp_path, tools=[])
    assert str(tmp_path) in prompt
    assert "mini-pi" in prompt
