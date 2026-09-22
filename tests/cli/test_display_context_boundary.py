"""CLI 展示元数据与持久化/Provider 消息边界测试。"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel
from rich.console import Console

from mini_pi.cli.console import ConsoleRenderer
from mini_pi.llm.openai_client import to_openai_messages
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant, tool_call


class InspectArgs(BaseModel):
    """无参数工具只用于核对结果边界。"""


class InspectTool(Tool):
    """返回模型 observation 与仅供展示的不同内容。"""

    name: ClassVar[str] = "inspect_boundary"
    description: ClassVar[str] = "Inspect the display boundary."
    args_model: ClassVar[type[BaseModel]] = InspectArgs

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(content="safe observation", details={"ui_only": "secret marker"})


def test_display_metadata_does_not_enter_history_or_provider(tmp_path: Path) -> None:
    """图案、用量和 details 只能在 CLI，模型仍收到完整 observation。"""
    stream = io.StringIO()
    renderer = ConsoleRenderer(Console(file=stream, width=80, no_color=True))
    registry = ToolRegistry()
    registry.register(InspectTool())
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "inspect_boundary", {})]),
            assistant("done"),
        ]
    )
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        on_event=renderer.handle,
    )

    runtime.run("inspect")

    loaded = JsonlSession.load(runtime.path)
    history = str([entry.message.model_dump() for entry in loaded.entries])
    wire = str(to_openai_messages(runtime.state.messages, include_reasoning=True))
    assert "safe observation" in history
    assert "safe observation" in wire
    for ui_only in ("secret marker", "db         db", "provider tokens:", str(runtime.path)):
        assert ui_only not in history
        assert ui_only not in wire
    assert "safe observation" in llm.calls[1][-1].content
