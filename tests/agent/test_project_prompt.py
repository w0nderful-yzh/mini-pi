"""Agent 项目规则注入和跨 run prompt refresh 测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mini_pi.agent.agent import Agent
from mini_pi.context.sections import replay_system_messages
from mini_pi.llm.openai_client import to_openai_messages
from mini_pi.llm.types import SectionPatch, SystemMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant


def make_agent(workspace_root: Path, replies: int = 3) -> tuple[Agent, FakeLLMClient]:
    """构造不调用网络的多轮 Agent。"""
    llm = FakeLLMClient([assistant(f"answer {index}") for index in range(replies)])
    agent = Agent(llm=llm, registry=ToolRegistry(), cwd=workspace_root)
    return agent, llm


def system_messages(agent: Agent) -> list[SystemMessage]:
    """提取 transcript 中的 system 快照和 patch。"""
    return [
        message
        for message in agent.state.messages
        if isinstance(message, SystemMessage)
    ]


def test_first_run_injects_project_rules_with_source_path(tmp_path: Path) -> None:
    """首次运行生成完整 sections 快照，模型请求仅看到一个完整 prompt。"""
    (tmp_path / "AGENTS.md").write_text("Use Chinese comments.\n", encoding="utf-8")
    agent, llm = make_agent(tmp_path)

    agent.run("hello")

    systems = system_messages(agent)
    assert len(systems) == 1
    assert systems[0].section_patch is None
    assert systems[0].sections is not None
    assert systems[0].sections["project_context"] == (
        '<project_instructions path="AGENTS.md">\n'
        "Use Chinese comments.\n"
        "</project_instructions>"
    )
    assert isinstance(llm.calls[0][1], UserMessage)
    wire = to_openai_messages(llm.calls[0])
    assert [message["role"] for message in wire].count("system") == 1
    assert 'path="AGENTS.md"' in wire[0]["content"]


def test_unchanged_rules_do_not_add_system_message(tmp_path: Path) -> None:
    """连续运行但文件未变时，不追加无意义 patch。"""
    (tmp_path / "AGENTS.md").write_text("same\n", encoding="utf-8")
    agent, llm = make_agent(tmp_path)

    agent.run("first")
    agent.run("second")

    assert len(system_messages(agent)) == 1
    assert len(llm.calls) == 2


def test_new_rules_add_project_context_patch(tmp_path: Path) -> None:
    """首轮无规则时不建空 section，随后新增规则只写一个 set。"""
    agent, _ = make_agent(tmp_path)
    agent.run("first")
    snapshot = system_messages(agent)[0].sections
    assert snapshot is not None and "project_context" not in snapshot
    (tmp_path / "AGENTS.md").write_text("added", encoding="utf-8")

    agent.run("second")

    assert system_messages(agent)[1].section_patch == [
        SectionPatch(
            op="set",
            id="project_context",
            content='<project_instructions path="AGENTS.md">\nadded\n</project_instructions>',
        )
    ]


def test_changed_rules_add_only_project_context_patch(tmp_path: Path) -> None:
    """下次运行前重新读取规则，只提交发生变化的 section。"""
    rules = tmp_path / "AGENTS.md"
    rules.write_text("old\n", encoding="utf-8")
    agent, llm = make_agent(tmp_path)
    agent.run("first")
    rules.write_text("new\n", encoding="utf-8")

    agent.run("second")

    systems = system_messages(agent)
    assert len(systems) == 2
    assert systems[1].section_patch == [
        SectionPatch(
            op="set",
            id="project_context",
            content='<project_instructions path="AGENTS.md">\nnew\n</project_instructions>',
        )
    ]
    assert isinstance(llm.calls[1][-1], UserMessage)
    assert llm.calls[1][-2] == systems[1]
    assert to_openai_messages(llm.calls[1])[0]["content"].endswith(
        '# Project Context\n<project_instructions path="AGENTS.md">\nnew\n</project_instructions>\n'
    )


def test_deleted_rules_add_tombstone(tmp_path: Path) -> None:
    """规则文件被删除后下次运行写 delete，Provider 不再看到旧规则。"""
    rules = tmp_path / "AGENTS.md"
    rules.write_text("temporary\n", encoding="utf-8")
    agent, llm = make_agent(tmp_path)
    agent.run("first")
    rules.unlink()

    agent.run("second")

    systems = system_messages(agent)
    assert systems[1].section_patch == [
        SectionPatch(op="delete", id="project_context")
    ]
    projected = replay_system_messages(systems)
    assert projected is not None and projected.sections is not None
    assert "project_context" not in projected.sections
    assert "temporary" not in to_openai_messages(llm.calls[1])[0]["content"]


def test_parent_and_child_rules_keep_discovery_order(tmp_path: Path) -> None:
    """git root 到 workspace 的规则依序显示，并保留各自来源路径。"""
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    workspace_root = tmp_path / "packages" / "app"
    workspace_root.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root rule\n", encoding="utf-8")
    (tmp_path / "packages" / "AGENTS.md").write_text("package rule\n", encoding="utf-8")
    (workspace_root / "AGENTS.md").write_text("app rule\n", encoding="utf-8")
    agent, _ = make_agent(workspace_root)

    agent.run("hello")

    snapshot = system_messages(agent)[0].sections
    assert snapshot is not None
    context = snapshot["project_context"]
    assert context.index('path="../../AGENTS.md"') < context.index('path="../AGENTS.md"')
    assert context.index('path="../AGENTS.md"') < context.index('path="AGENTS.md"')
    assert context.index("root rule") < context.index("package rule") < context.index("app rule")


def test_invalid_rules_fail_before_appending_user_or_calling_llm(tmp_path: Path) -> None:
    """规则读取失败时不得进入本轮 LLM 或追加新的 user 消息。"""
    rules = tmp_path / "AGENTS.md"
    rules.write_text("valid\n", encoding="utf-8")
    agent, llm = make_agent(tmp_path)
    agent.run("first")
    original_messages = list(agent.state.messages)
    rules.write_bytes(b"\xff\xfe")

    with pytest.raises(UnicodeDecodeError):
        agent.run("second")

    assert agent.state.messages == original_messages
    assert len(llm.calls) == 1


def test_reset_creates_fresh_snapshot(tmp_path: Path) -> None:
    """清空上下文后下次运行从当前规则创建新完整快照。"""
    rules = tmp_path / "AGENTS.md"
    rules.write_text("before\n", encoding="utf-8")
    agent, _ = make_agent(tmp_path)
    agent.run("first")
    agent.reset()
    rules.write_text("after\n", encoding="utf-8")

    agent.run("second")

    systems = system_messages(agent)
    assert len(systems) == 1
    assert systems[0].sections is not None
    assert "after\n" in systems[0].sections["project_context"]
