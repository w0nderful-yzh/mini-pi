"""M9.1：真实问答和取消的持久化、恢复及强杀边界。"""

from __future__ import annotations

import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

from mini_pi.errors import SessionError
from mini_pi.interaction import InteractionResponse
from mini_pi.llm.types import ToolMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call


@pytest.mark.parametrize("answer", ["兼容", None])
def test_resume_keeps_answer_or_cancellation_without_reasking(answer: str | None, tmp_path: Path) -> None:
    """原问题结果按事实恢复，续跑只追加消息，不再次询问或改写旧字节。"""
    registry = build_default_registry(
        Workspace(tmp_path), interaction=lambda request: InteractionResponse(answer)
    )
    llm = FakeLLMClient([
        assistant(tool_calls=[tool_call("q1", "ask_user", {"question": "choose?"})]),
        assistant("done"),
    ])
    runtime = AgentSession.create(
        cwd=tmp_path, llm=llm, registry=registry, provider="deepseek", model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )
    runtime.run("choose and fix")
    old_bytes = runtime.path.read_bytes()
    old_messages = JsonlSession.load(runtime.path).replay().messages
    tool = next(message for message in old_messages if isinstance(message, ToolMessage))
    assert tool.is_error == (answer is None)
    if answer is not None:
        assert json.loads(tool.content)["answer"] == answer

    def forbidden(request: object) -> InteractionResponse:
        """恢复旧问答时不能重新调用宿主交互。"""
        pytest.fail("old question was re-executed")

    next_llm = FakeLLMClient([assistant("continued")])
    restored = AgentSession.resume(
        runtime.path, cwd=tmp_path, llm_factory=lambda provider, model: next_llm,
        registry=build_default_registry(Workspace(tmp_path), interaction=forbidden),
    )
    assert restored.state.messages == list(old_messages)
    assert restored.run("continue").content == "continued"
    assert runtime.path.read_bytes().startswith(old_bytes)
    assert next_llm.calls[0][:-1] == list(old_messages)


def test_sigkill_while_waiting_still_rejects_incomplete_session(tmp_path: Path) -> None:
    """强杀后的未答工具轮明确拒绝恢复，不能伪造回答或放宽损坏数据校验。"""
    script = '''
import sys, time
from pathlib import Path
from mini_pi.interaction import InteractionResponse
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call
def wait(request):
    """保持待答直到父测试强杀进程。"""
    print("WAITING", flush=True)
    time.sleep(60)
    return InteractionResponse("unused")
root = Path(sys.argv[1])
runtime = AgentSession.create(
    cwd=root, llm=FakeLLMClient([assistant(tool_calls=[
        tool_call("q1", "ask_user", {"question": "choose?"})
    ])]), registry=build_default_registry(Workspace(root), interaction=wait),
    provider="deepseek", model="deepseek-flash", sessions_root=root / "sessions",
)
runtime.run("choose")
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "child did not reach question"
        assert process.stdout.readline() == b"WAITING\n"
        process.kill()
        process.wait(timeout=5)
        path = next((tmp_path / "sessions").rglob("*.jsonl"))
        original = path.read_bytes()
        with pytest.raises(SessionError, match="missing tool result"):
            AgentSession.resume(
                path, registry=build_default_registry(Workspace(tmp_path)),
                llm_factory=lambda provider, model: FakeLLMClient([]),
            )
        assert path.read_bytes() == original
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
