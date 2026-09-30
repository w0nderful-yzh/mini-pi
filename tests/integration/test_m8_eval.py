"""M8.0 离线验收：固定故障、外部判定、改动范围和版本指纹。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from mini_pi.llm.types import SystemMessage, ToolMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry
from tests.conftest import assistant, tool_call


def _driver(filename: str = "m8-0-eval.py") -> ModuleType:
    """按脚本路径加载评测驱动器，避免在生产包中加入评测依赖。"""
    path = Path(__file__).resolve().parents[2] / "docs" / "benchmarks" / filename
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRIVER = _driver()


def _repair(name: str, workspace: Path) -> None:
    """只按任务允许范围修复已知故障，供外部判定器做正例。"""
    if name == "multi_file":
        fees = workspace / "fees.py"
        fees.write_text(fees.read_text().replace("// 10", "// 100"))
        invoice = workspace / "invoice.py"
        invoice.write_text(invoice.read_text().replace("amount + discount", "amount - discount"))
    elif name == "failure_recovery":
        parser = workspace / "parser.py"
        parser.write_text(
            parser.read_text().replace(
                'for part in raw.split(",")', 'for part in raw.split(",") if part.strip()'
            )
        )
    elif name == "config_ci":
        config = workspace / "pyproject.toml"
        config.write_text(config.read_text().replace('addopts = "-k nonexistent_case"\n', ""))
    else:
        style = workspace / "mini_pi" / "cli" / "style.py"
        style.write_text(style.read_text().replace("return str(value)", 'return f"{value:,}"'))


@pytest.mark.parametrize("task", DRIVER.TASKS, ids=[task.name for task in DRIVER.TASKS])
def test_fixed_task_has_failing_initial_state_and_external_success(task: object, tmp_path: Path) -> None:
    """每种任务先真实失败，再由允许文件中的修复通过外部判定。"""
    workspace = tmp_path / "project"
    DRIVER.prepare(task, workspace)
    before = DRIVER._fingerprints(workspace)
    initial = DRIVER._score(task, workspace, before)
    assert initial["scorer_exit"] != 0
    assert not initial["external_pass"]

    _repair(task.name, workspace)
    scored = DRIVER._score(task, workspace, before)
    assert scored["scorer_exit"] == 0
    assert scored["external_pass"]
    assert not scored["unexpected_files"]
    assert set(scored["changed_files"]) <= task.allowed_changes


def test_scorer_rejects_test_tampering_and_fixture_identity_is_stable(tmp_path: Path) -> None:
    """测试文件被改动即使测试通过也失败；同样初态得到同样指纹。"""
    task = DRIVER.TASKS[0]
    first = tmp_path / "first"
    second = tmp_path / "second"
    DRIVER.prepare(task, first)
    DRIVER.prepare(task, second)
    before = DRIVER._fingerprints(first)
    assert DRIVER._fixture_identity(task, before) == DRIVER._fixture_identity(
        task, DRIVER._fingerprints(second)
    )

    _repair(task.name, first)
    test_file = first / "test_invoice.py"
    test_file.write_text(test_file.read_text() + "\n# altered\n")
    scored = DRIVER._score(task, first, before)
    assert scored["scorer_exit"] == 0
    assert not scored["external_pass"]
    assert scored["unexpected_files"] == ["test_invoice.py"]


def test_tool_counts_skip_compaction_entries() -> None:
    """真实长任务自动压缩后仍按工具消息统计，不读取检查点的 message。"""
    now = datetime.now(timezone.utc)
    message = MessageEntry.model_validate(
        {
            "type": "message",
            "id": uuid4(),
            "parentId": None,
            "timestamp": now,
            "provider": "deepseek",
            "model": "deepseek-flash",
            "message": ToolMessage(tool_call_id="c1", name="read", content="ok"),
        }
    )
    checkpoint = CompactionEntry.model_validate(
        {
            "type": "compaction",
            "id": uuid4(),
            "parentId": message.id,
            "timestamp": now,
            "summary": "已检查。",
            "firstKeptEntryId": message.id,
            "tokensBefore": 100,
            "systemMessage": SystemMessage(content="rules"),
        }
    )
    assert DRIVER._tool_counts([message, checkpoint]) == {"read": 1}


def test_external_pass_does_not_turn_step_limit_into_success() -> None:
    """真实仓库试跑已暴露外部测试通过但 run 未完成的情况。"""
    assert DRIVER._task_success("completed", external_pass=True)
    assert not DRIVER._task_success("step_limit", external_pass=True)
    assert not DRIVER._task_success("budget_limit", external_pass=True)
    assert not DRIVER._task_success("completed", external_pass=False)


def test_batch_metrics_distinguish_failed_reads_and_repeated_reads(tmp_path: Path) -> None:
    """合批数按 assistant 轮计；失败与同参读取分开计，避免冒充节省。"""
    session = JsonlSession.create(
        cwd=tmp_path, provider="deepseek", model="deepseek-flash", sessions_root=tmp_path / "s"
    )
    messages = [
        UserMessage(content="inspect"),
        assistant(tool_calls=[
            tool_call("c1", "read", {"path": "a.py"}),
            tool_call("c2", "read", {"path": "missing.py"}),
        ]),
        ToolMessage(tool_call_id="c1", name="read", content="a"),
        ToolMessage(tool_call_id="c2", name="read", content="not a file", is_error=True),
        assistant(tool_calls=[tool_call("c3", "read", {"path": "a.py"})]),
        ToolMessage(tool_call_id="c3", name="read", content="a"),
        assistant("done"),
    ]
    for message in messages:
        session.append_message(message, provider="deepseek", model="deepseek-flash")

    counts = DRIVER._tool_observations(session.active_entries())

    assert counts == {
        "repeated_exact_calls": 1,
        "verification_commands": 0,
        "tool_errors": 1,
        "failed_reads": 1,
        "repeated_exact_reads": 1,
        "tool_turns": 2,
        "multi_call_turns": 1,
        "max_calls_per_turn": 2,
    }


def test_m8_2_candidate_only_adds_one_rule() -> None:
    """两种评测规则从固定提交构造，差异只有候选的一段文本。"""
    driver = _driver("m8-2-eval.py")
    variants = driver.variants()

    assert len(variants["baseline"]) == 7
    assert variants["batched"] == (*variants["baseline"], driver.BATCH_RULE)
    assert "read-only" in driver.BATCH_RULE
    assert "fully known arguments" in driver.BATCH_RULE
    assert "Tools run sequentially" in driver.BATCH_RULE
    assert "wait for observations" in driver.BATCH_RULE
