"""M7.9.3 离线任务基线：四条固定任务形态走生产装配路径，指标必须可复现。

本目录不使用 `integration` marker（那个 marker 专指真实 API）：四条任务都用脚本化
FakeLLM，但工具、Workspace 与 JSONL 都是真实的，因此请求数、工具调用数、退出原因与
改动事实都能稳定复现，作为 M7.9.3 真实 Provider 基线的对照。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from mini_pi.agent.events import AgentEndEvent, AgentEvent
from mini_pi.llm.types import AssistantMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.session.usage import recent_session_run_usage
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call

FIXTURE_PROJECT = Path(__file__).resolve().parents[1] / "fixtures" / "sample_project"

# 用当前解释器绝对路径，避免依赖 PATH 里的 python
PYTEST = f"'{sys.executable}' -m pytest -q"
MISSING_PATH_COMMAND = f"'{sys.executable}' -m pytest tests -q"
BUG_LINE = "    return a - b"
FIXED_LINE = "    return a + b"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一条固定任务：提示词、脚本化回复与预期指标。"""

    name: str
    prompt: str
    script: tuple[AssistantMessage, ...]
    requests: int
    tool_calls: int
    modified_files: frozenset[str]
    tests_pass_after: bool


@dataclass(frozen=True, slots=True)
class TaskMetrics:
    """一次任务实际观测到的指标；全部来自事件流与落盘 JSONL。"""

    name: str
    end_reason: str
    requests: int
    tool_calls: int
    modified_files: frozenset[str]
    tests_pass: bool


# 只读：定位实现并解释行为，不改任何文件
_TASKS = (
    TaskSpec(
        name="read_only",
        prompt="calculator.py 里的 add 做了什么？",
        script=(
            assistant(tool_calls=[tool_call("c1", "read", {"path": "calculator.py"})]),
            assistant("add 返回 a - b，所以 add(2, 3) 是 -1，测试期望的 5 不会成立。"),
        ),
        requests=2,
        tool_calls=1,
        modified_files=frozenset(),
        tests_pass_after=False,
    ),
    # 跨文件定位：调用点与定义点都要看到
    TaskSpec(
        name="locate",
        prompt="找出 add 的定义位置和调用位置。",
        script=(
            assistant(tool_calls=[tool_call("c1", "search", {"pattern": "add"})]),
            assistant(tool_calls=[tool_call("c2", "read", {"path": "calculator.py"})]),
            assistant(tool_calls=[tool_call("c3", "read", {"path": "test_calculator.py"})]),
            assistant("add 定义在 calculator.py，test_calculator.py 里调用并断言 5。"),
        ),
        requests=4,
        tool_calls=3,
        modified_files=frozenset(),
        tests_pass_after=False,
    ),
    # 修改后验证：先用失败的测试确认现状，再改代码并复验
    TaskSpec(
        name="modify_and_verify",
        prompt="运行 pytest，修复失败用例，然后再次运行 pytest 验证。",
        script=(
            assistant(tool_calls=[tool_call("c1", "bash", {"command": PYTEST})]),
            assistant(tool_calls=[tool_call("c2", "read", {"path": "calculator.py"})]),
            assistant(
                tool_calls=[
                    tool_call(
                        "c3",
                        "edit",
                        {
                            "path": "calculator.py",
                            "edits": [{"old_text": BUG_LINE, "new_text": FIXED_LINE}],
                        },
                    )
                ]
            ),
            assistant(tool_calls=[tool_call("c4", "bash", {"command": PYTEST})]),
            assistant("已把 add 改为 a + b，pytest 通过。"),
        ),
        requests=5,
        tool_calls=4,
        modified_files=frozenset({"calculator.py"}),
        tests_pass_after=True,
    ),
    # 失败后恢复：命令本身先失败，观察后换命令，再走完整的修复与复验
    TaskSpec(
        name="recover_from_failure",
        prompt="运行 pytest 并修复失败用例。",
        script=(
            assistant(tool_calls=[tool_call("c1", "bash", {"command": MISSING_PATH_COMMAND})]),
            assistant(tool_calls=[tool_call("c2", "bash", {"command": PYTEST})]),
            assistant(tool_calls=[tool_call("c3", "read", {"path": "calculator.py"})]),
            assistant(
                tool_calls=[
                    tool_call(
                        "c4",
                        "edit",
                        {
                            "path": "calculator.py",
                            "edits": [{"old_text": BUG_LINE, "new_text": FIXED_LINE}],
                        },
                    )
                ]
            ),
            assistant(tool_calls=[tool_call("c5", "bash", {"command": PYTEST})]),
            assistant("第一次命令路径写错，改成在项目根运行 pytest 后修复并通过。"),
        ),
        requests=6,
        tool_calls=5,
        modified_files=frozenset({"calculator.py"}),
        tests_pass_after=True,
    ),
)


def make_workspace(tmp_path: Path) -> Path:
    """把样例项目复制到临时目录，不触碰仓库内夹具。"""
    workspace = tmp_path / "project"
    shutil.copytree(
        FIXTURE_PROJECT,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return workspace


def pytest_passes(workspace: Path) -> bool:
    """在任务结束后直接跑 pytest，用真实结果判断任务是否完成。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def run_task(spec: TaskSpec, tmp_path: Path) -> TaskMetrics:
    """按生产装配路径执行一条脚本化任务，并统计事件与 JSONL 里的指标。"""
    workspace = make_workspace(tmp_path)
    events: list[AgentEvent] = []
    llm = FakeLLMClient(list(spec.script))
    runtime = AgentSession.create(
        cwd=workspace,
        llm=llm,
        registry=build_default_registry(Workspace(workspace)),
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=tmp_path / "sessions",
        on_event=events.append,
    )

    runtime.run(spec.prompt)

    session = JsonlSession.load(runtime.path)
    usage = recent_session_run_usage(session.active_entries())
    assert usage is not None
    ends = [event for event in events if isinstance(event, AgentEndEvent)]
    assert len(ends) == 1, "一次 run 只应产生一个 agent_end"
    # 脚本必须被完全消费：请求数与脚本长度不一致说明 Loop 行为已变
    assert len(llm.calls) == spec.requests
    return TaskMetrics(
        name=spec.name,
        end_reason=ends[0].reason,
        requests=usage.requests,
        tool_calls=usage.tool_calls,
        modified_files=runtime.state.modified_files,
        tests_pass=pytest_passes(workspace),
    )


@pytest.mark.parametrize("spec", _TASKS, ids=[spec.name for spec in _TASKS])
def test_task_baseline_metrics_are_reproducible(spec: TaskSpec, tmp_path: Path) -> None:
    """四条任务形态的请求数、工具调用数、退出原因与改动事实都固定。"""
    metrics = run_task(spec, tmp_path)

    assert metrics.end_reason == "completed"
    assert metrics.requests == spec.requests
    assert metrics.tool_calls == spec.tool_calls
    assert metrics.modified_files == spec.modified_files
    # 完成与否由真实 pytest 结果判定，不采信脚本里的总结文本
    assert metrics.tests_pass is spec.tests_pass_after


def test_read_only_task_leaves_workspace_untouched(tmp_path: Path) -> None:
    """只读任务不得改动文件，样例项目的失败用例应当保持失败。"""
    spec = _TASKS[0]

    metrics = run_task(spec, tmp_path)

    assert metrics.modified_files == frozenset()
    assert metrics.tests_pass is False
