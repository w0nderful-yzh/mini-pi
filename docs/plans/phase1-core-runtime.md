# Phase 1: Core Runtime 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Python 实现一个可自主完成“定位 → 修改 → 验证”闭环的最小 Coding Agent（M1-M6）。

**Architecture:** CLI → Agent → (LLMClient, ToolRegistry) → Tool → Workspace 单向依赖；`run_loop()` 为纯函数、同步执行、通过 `on_event` 发出 `AgentEvent`；LLM 层同步 SDK + `stream=True`，错误编码为 `ErrorEvent`；ToolError 转 `is_error` observation 回传模型，其他异常直接冒泡。

**Tech Stack:** Python 3.12+ / uv / Pydantic v2 / openai SDK（同步）/ Typer / Rich / pytest

**设计依据:** `/Users/yzh666/workspace/pi`（生产路径：`packages/agent`、`packages/coding-agent/src/core`、`packages/ai`）与 `/Users/yzh666/workspace/pi/AGENT-LEARNING-GUIDE.md`。取舍见 `README.md` 第 2 节，约束见 `AGENTS.md`。

---

## 前置条件

- [ ] `uv --version` 可用（本机已验证 0.12.x）
- [ ] 系统 Python 可以是 3.9，`requires-python = ">=3.12"` 由 uv 自动下载 3.12+
- [ ] git 仓库已初始化（`master`，尚无提交）
- [ ] 真实 API Key（仅 M1.6 / M5.3 集成测试需要）：`OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY`

## 目标文件结构

```text
mini-pi/
├── pyproject.toml
├── .gitignore
├── README.md
├── AGENTS.md
├── docs/plans/phase1-core-runtime.md
├── mini_pi/
│   ├── __init__.py
│   ├── errors.py
│   ├── cli/{__init__.py, app.py, console.py}
│   ├── agent/{__init__.py, agent.py, loop.py, events.py, state.py, prompt.py}
│   ├── llm/{__init__.py, types.py, base.py, openai_client.py, deepseek_client.py}
│   ├── tools/{__init__.py, base.py, registry.py, truncate.py, process.py,
│   │          read.py, write.py, edit.py, search.py, bash.py, git.py}
│   └── workspace/{__init__.py, workspace.py}
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_bootstrap.py
    ├── test_llm_types.py
    ├── test_llm_base.py
    ├── test_openai_client.py
    ├── test_deepseek_client.py
    ├── test_tool_base.py
    ├── test_registry.py
    ├── test_agent_events.py
    ├── test_loop.py
    ├── test_agent.py
    ├── test_workspace.py
    ├── test_truncate.py
    ├── test_process.py
    ├── test_read.py
    ├── test_write.py
    ├── test_edit.py
    ├── test_search.py
    ├── test_bash.py
    ├── test_git.py
    ├── test_registry_defaults.py
    ├── test_console.py
    ├── test_cli.py
    ├── test_integration_llm.py
    ├── test_integration_agent.py
    └── fixtures/sample_project/{calculator.py, test_calculator.py}
```

## 跨任务约定

1. 每个任务严格按步骤执行：先写失败测试 → 运行确认失败 → 写最小实现 → 运行通过 → commit。
2. 所有新文件加 `from __future__ import annotations`（除非有理由不加）。
3. 错误分类固定：
   - `ToolError`（含 `ToolNotFoundError` / `ToolArgumentError` / `WorkspaceViolationError`）→ Loop 转 error observation
   - `LLMError` → LLM 层编码为 `ErrorEvent`
   - 其他异常 → 冒泡
4. 测试命令统一 `uv run pytest <file> -v`；默认 `addopts = "-m 'not integration'"` 排除真实 API 测试。
5. 代码必须按 AGENTS.md 第 18 节附带简要中文注释；本计划代码块为节省篇幅可能省略部分注释，落地时补齐。类型标注完整。
6. 已完成的里程碑在本文档中精简为「交付物 + 验收」摘要，删除完整代码块；未完成部分保留完整步骤与代码。
6. 工具的文件操作只允许经过 `Workspace`，禁止直接 `open()` / `Path.read_text()`。

---

## M1 LLM 调通（已完成）

提交：`1d17742`（骨架）、`dc6bb75`（消息模型）、`fb46bf4`（错误与重试）、`850ed47`（OpenAI）、`c73aebd`（DeepSeek + 集成测试）

交付物：

- `mini_pi/errors.py`：`MiniPiError` / `ToolError` 体系（ToolNotFound / ToolArgument / WorkspaceViolation）/ `LLMError(retryable, status_code)`
- `mini_pi/llm/types.py`：`Message` 判别联合（system / user / assistant / tool）、`ToolCall`、`Usage`、`ToolSchema`、`StreamEvent` 判别联合
- `mini_pi/llm/base.py`：`LLMClient` Protocol；`BaseLLMClient` 重试策略（429/5xx/网络可重试，指数退避封顶 8s；已产出事件不重试；错误编码为 `ErrorEvent`）；`complete()` 聚合
- `mini_pi/llm/openai_client.py`：同步流式调用；`_AssistantAccumulator` 按 index 拼装 tool call 参数，非法 JSON 显式报错；usage / reasoning 捕获；wire 消息与工具转换
- `mini_pi/llm/deepseek_client.py`：仅差异三项（base_url、`DEEPSEEK_API_KEY`、`reasoning_content` 回放）
- 测试：`test_llm_types` / `test_llm_base` / `test_openai_client` / `test_deepseek_client`，以及 opt-in 的 `test_integration_llm`

验收：`uv run pytest` → 24 passed, 2 deselected；`uv run pytest -m integration` 在配置 API Key 后通过。

---

## M2 Tool Calling

### Task M2.1: Tool 基类与 ToolResult

**Files:**
- Create: `mini_pi/tools/__init__.py`（本任务先留空）
- Create: `mini_pi/tools/base.py`
- Test: `tests/test_tool_base.py`

- [x] **Step 1: 写失败测试 `tests/test_tool_base.py`**

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.tools.base import Tool, ToolResult


class EchoArgs(BaseModel):
    text: str = Field(description="Text to echo back.")


class EchoTool(Tool):
    name = "echo"
    description = "Echo the provided text."
    args_model = EchoArgs

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=text, details={"length": len(text)})


def test_schema_is_built_from_args_model() -> None:
    schema = EchoTool().schema()
    assert schema.name == "echo"
    assert schema.description == "Echo the provided text."
    assert schema.parameters["properties"]["text"]["description"] == "Text to echo back."
    assert schema.parameters["required"] == ["text"]


def test_execute_returns_structured_result() -> None:
    result = EchoTool().execute(text="hi")
    assert result.content == "hi"
    assert result.details == {"length": 2}


def test_tool_result_details_default_none() -> None:
    assert ToolResult(content="x").details is None
```

- [x] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tool_base.py -v`
Expected: FAIL，`No module named 'mini_pi.tools'`

- [x] **Step 3: 写 `mini_pi/tools/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from mini_pi.llm.types import ToolSchema


class ToolResult(BaseModel):
    content: str
    details: dict[str, Any] | None = None


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult: ...
```

- [x] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_tool_base.py -v`
Expected: `3 passed`

- [x] **Step 5: Commit**

```bash
git add mini_pi/tools/__init__.py mini_pi/tools/base.py tests/test_tool_base.py
git commit -m "feat: add Tool base class and ToolResult"
```

---

### Task M2.2: ToolRegistry

**Files:**
- Create: `mini_pi/tools/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: 写失败测试 `tests/test_registry.py`**

```python
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from mini_pi.errors import ToolArgumentError, ToolError, ToolNotFoundError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry


class AddArgs(BaseModel):
    a: int
    b: int = Field(default=1, description="Second operand.")


class AddTool(Tool):
    name = "add"
    description = "Add two integers."
    args_model = AddArgs

    def execute(self, a: int, b: int = 1) -> ToolResult:
        return ToolResult(content=str(a + b))


class ExplodingTool(Tool):
    name = "explode"
    description = "Always fails with ToolError."
    args_model = AddArgs

    def execute(self, a: int, b: int = 1) -> ToolResult:
        raise ToolError("expected failure")


def test_register_and_schemas() -> None:
    registry = ToolRegistry()
    registry.register(AddTool())
    schemas = registry.schemas()
    assert [schema.name for schema in schemas] == ["add"]


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(AddTool())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(AddTool())


def test_execute_validates_and_applies_defaults() -> None:
    registry = ToolRegistry()
    registry.register(AddTool())
    assert registry.execute("add", {"a": 2}).content == "3"
    assert registry.execute("add", {"a": 2, "b": 5}).content == "7"


def test_unknown_tool_raises() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError, match="unknown tool"):
        registry.execute("nope", {})


def test_invalid_arguments_raise() -> None:
    registry = ToolRegistry()
    registry.register(AddTool())
    with pytest.raises(ToolArgumentError, match="invalid arguments"):
        registry.execute("add", {"a": "not-an-int"})
    with pytest.raises(ToolArgumentError, match="invalid arguments"):
        registry.execute("add", {})


def test_tool_error_propagates() -> None:
    registry = ToolRegistry()
    registry.register(ExplodingTool())
    with pytest.raises(ToolError, match="expected failure"):
        registry.execute("explode", {"a": 1})
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.registry'`

- [ ] **Step 3: 写 `mini_pi/tools/registry.py`**

```python
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from mini_pi.errors import ToolArgumentError, ToolNotFoundError
from mini_pi.llm.types import ToolSchema
from mini_pi.tools.base import Tool, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[ToolSchema]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        try:
            validated = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolArgumentError(f"invalid arguments for {name!r}: {exc}") from exc
        return tool.execute(**validated.model_dump())
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_registry.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/registry.py tests/test_registry.py
git commit -m "feat: add tool registry with schema validation"
```

---

### Task M2.3: AgentState 与 AgentEvent

**Files:**
- Create: `mini_pi/agent/__init__.py`（空文件）
- Create: `mini_pi/agent/state.py`
- Create: `mini_pi/agent/events.py`
- Test: `tests/test_agent_events.py`

- [ ] **Step 1: 写失败测试 `tests/test_agent_events.py`**

```python
from __future__ import annotations

from pydantic import TypeAdapter

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.agent.state import AgentState
from mini_pi.llm.types import AssistantMessage, ToolCall, UserMessage
from mini_pi.tools.base import ToolResult

EVENT_ADAPTER = TypeAdapter(list[AgentEvent])


def test_state_defaults_and_mutation() -> None:
    state = AgentState()
    assert state.messages == []
    assert state.step_count == 0
    assert state.modified_files == set()
    state.modified_files.add("a.py")
    assert state.modified_files == {"a.py"}


def test_event_union_roundtrip() -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    events = [
        AgentStartEvent(),
        TurnStartEvent(step=1),
        MessageDeltaEvent(kind="text", delta="hi"),
        MessageEndEvent(message=AssistantMessage(content="hi")),
        ToolExecutionStartEvent(tool_call=call),
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="hi"), is_error=False),
        TurnEndEvent(step=1),
        AgentEndEvent(reason="completed", message=AssistantMessage(content="done")),
    ]
    dumped = [event.model_dump() for event in events]
    restored = EVENT_ADAPTER.validate_python(dumped)
    assert [event.type for event in restored] == [
        "agent_start",
        "turn_start",
        "message_delta",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
        "agent_end",
    ]
    assert restored[5].result.content == "hi"


def test_state_accepts_messages() -> None:
    state = AgentState(messages=[UserMessage(content="hi")])
    assert state.messages[0].content == "hi"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent_events.py -v`
Expected: FAIL，`No module named 'mini_pi.agent'`

- [ ] **Step 3: 写 `mini_pi/agent/state.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field

from mini_pi.llm.types import Message


@dataclass
class AgentState:
    messages: list[Message] = field(default_factory=list)
    step_count: int = 0
    modified_files: set[str] = field(default_factory=set)
```

- [ ] **Step 4: 写 `mini_pi/agent/events.py`**

```python
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mini_pi.llm.types import AssistantMessage, ToolCall
from mini_pi.tools.base import ToolResult


class AgentStartEvent(BaseModel):
    type: Literal["agent_start"] = "agent_start"


class TurnStartEvent(BaseModel):
    type: Literal["turn_start"] = "turn_start"
    step: int


class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"


class MessageDeltaEvent(BaseModel):
    type: Literal["message_delta"] = "message_delta"
    kind: Literal["text", "thinking"]
    delta: str


class MessageEndEvent(BaseModel):
    type: Literal["message_end"] = "message_end"
    message: AssistantMessage


class ToolExecutionStartEvent(BaseModel):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call: ToolCall


class ToolExecutionEndEvent(BaseModel):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call: ToolCall
    result: ToolResult
    is_error: bool


class TurnEndEvent(BaseModel):
    type: Literal["turn_end"] = "turn_end"
    step: int


class AgentEndEvent(BaseModel):
    type: Literal["agent_end"] = "agent_end"
    reason: Literal["completed", "step_limit", "error"]
    message: AssistantMessage | None = None
    error: str | None = None


AgentEvent = Annotated[
    AgentStartEvent
    | TurnStartEvent
    | MessageStartEvent
    | MessageDeltaEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionEndEvent
    | TurnEndEvent
    | AgentEndEvent,
    Field(discriminator="type"),
]
```

- [ ] **Step 5: 运行测试通过**

Run: `uv run pytest tests/test_agent_events.py -v`
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add mini_pi/agent/__init__.py mini_pi/agent/state.py mini_pi/agent/events.py tests/test_agent_events.py
git commit -m "feat: add AgentState and AgentEvent models"
```

---

### Task M2.4: run_loop（工具调用循环）

**Files:**
- Create: `mini_pi/agent/loop.py`
- Create: `tests/__init__.py`（空文件，使 `from tests.conftest import ...` 可导入）
- Create: `tests/conftest.py`
- Test: `tests/test_loop.py`

- [ ] **Step 1: 创建 `tests/__init__.py` 并写 `tests/conftest.py`（FakeLLMClient 与工具 fixtures）**

先创建空文件 `tests/__init__.py`。

```python
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from mini_pi.errors import LLMError
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    StartEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolSchema,
)
from mini_pi.tools.base import Tool, ToolResult


def assistant(
    content: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    stop_reason: str | None = None,
    reasoning_content: str | None = None,
) -> AssistantMessage:
    calls = tool_calls or []
    if stop_reason is None:
        stop_reason = "tool_calls" if calls else "stop"
    return AssistantMessage(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=calls,
        stop_reason=stop_reason,
    )


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


class FakeLLMClient:
    def __init__(self, script: list[AssistantMessage | LLMError]) -> None:
        self._script = list(script)
        self.calls: list[list[Message]] = []
        self.tools_seen: list[list[ToolSchema] | None] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if not self._script:
            raise AssertionError("FakeLLMClient script exhausted")
        item = self._script.pop(0)
        if isinstance(item, LLMError):
            yield ErrorEvent(message=str(item), retryable=item.retryable)
            return
        yield StartEvent()
        if item.content:
            yield TextDeltaEvent(delta=item.content)
        for index, call in enumerate(item.tool_calls):
            yield ToolCallStartEvent(index=index, id=call.id, name=call.name)
            yield ToolCallDeltaEvent(index=index, arguments_delta=json.dumps(call.arguments))
            yield ToolCallEndEvent(index=index, tool_call=call)
        yield DoneEvent(message=item)

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        for event in self.stream(messages, tools):
            if isinstance(event, DoneEvent):
                return event.message
            if isinstance(event, ErrorEvent):
                raise LLMError(event.message, retryable=event.retryable)
        raise AssertionError("unreachable")


class EchoArgs(BaseModel):
    text: str


class EchoTool(Tool):
    name = "echo"
    description = "Echo the input text."
    args_model = EchoArgs

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=text, details={"path": f"echo/{text}.txt"})


class FailingArgs(BaseModel):
    reason: str


class FailingTool(Tool):
    name = "fail"
    description = "Always raises ToolError."
    args_model = FailingArgs

    def execute(self, reason: str) -> ToolResult:
        from mini_pi.errors import ToolError

        raise ToolError(reason)


class CrashArgs(BaseModel):
    pass


class CrashTool(Tool):
    name = "crash"
    description = "Raises an unexpected exception."
    args_model = CrashArgs

    def execute(self) -> ToolResult:
        raise RuntimeError("boom")


@pytest.fixture
def events() -> list:
    return []


@pytest.fixture
def echo_registry() -> "ToolRegistry":
    from mini_pi.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(FailingTool())
    registry.register(CrashTool())
    return registry
```

- [ ] **Step 2: 写失败测试 `tests/test_loop.py`**

```python
from __future__ import annotations

import pytest

from mini_pi.agent.events import (
    AgentEndEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.agent.loop import run_loop
from mini_pi.agent.state import AgentState
from mini_pi.llm.types import ToolMessage, UserMessage
from tests.conftest import FakeLLMClient, assistant, tool_call


def test_plain_answer_completes(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([assistant("hello")])
    result = run_loop(state, llm, echo_registry, on_event=None)
    assert result.content == "hello"
    assert state.step_count == 1
    assert state.messages[-1].content == "hello"


def test_tool_call_becomes_observation(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="echo it")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant("done"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "done"
    assert state.step_count == 2
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == "hi"
    assert tool_messages[0].is_error is False
    assert state.modified_files == {"echo/hi.txt"}


def test_event_sequence(echo_registry, events) -> None:
    state = AgentState(messages=[UserMessage(content="echo")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "x"})]),
            assistant("done"),
        ]
    )
    run_loop(state, llm, echo_registry, on_event=events.append)
    types = [event.type for event in events]
    assert types[0] == "agent_start"
    assert types[1:3] == ["turn_start", "message_start"]
    assert "message_delta" in types
    assert "tool_execution_start" in types
    assert "tool_execution_end" in types
    assert types[-1] == "agent_end"
    assert events[-1].reason == "completed"
    assert any(isinstance(event, TurnEndEvent) for event in events)


def test_tool_error_becomes_error_observation(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="fail")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "fail", {"reason": "nope"})]),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_message = [message for message in state.messages if isinstance(message, ToolMessage)][0]
    assert tool_message.is_error is True
    assert "nope" in tool_message.content


def test_unknown_tool_becomes_error_observation(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="unknown")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "nope", {})]),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_message = [message for message in state.messages if isinstance(message, ToolMessage)][0]
    assert tool_message.is_error is True
    assert "ToolNotFoundError" in tool_message.content


def test_invalid_arguments_become_error_observation(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="bad args")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"wrong": 1})]),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_message = [message for message in state.messages if isinstance(message, ToolMessage)][0]
    assert tool_message.is_error is True
    assert "ToolArgumentError" in tool_message.content


def test_unexpected_tool_exception_propagates(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="crash")])
    llm = FakeLLMClient([assistant(tool_calls=[tool_call("c1", "crash", {})])])
    with pytest.raises(RuntimeError, match="boom"):
        run_loop(state, llm, echo_registry)


def test_step_limit_stops_loop(echo_registry, events) -> None:
    state = AgentState(messages=[UserMessage(content="loop")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "1"})]),
            assistant(tool_calls=[tool_call("c2", "echo", {"text": "2"})]),
        ]
    )
    result = run_loop(state, llm, echo_registry, max_steps=2, on_event=events.append)
    assert state.step_count == 2
    assert result.tool_calls != []
    assert events[-1].reason == "step_limit"


def test_empty_user_message_is_allowed(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="")])
    llm = FakeLLMClient([assistant("ok")])
    assert run_loop(state, llm, echo_registry).content == "ok"
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_loop.py -v`
Expected: FAIL，`No module named 'mini_pi.agent.loop'`

- [ ] **Step 4: 写 `mini_pi/agent/loop.py`**

```python
from __future__ import annotations

from collections.abc import Callable

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.agent.state import AgentState
from mini_pi.errors import MiniPiError, ToolError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    Message,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolMessage,
)
from mini_pi.tools.base import ToolResult
from mini_pi.tools.registry import ToolRegistry

EventSink = Callable[[AgentEvent], None]


def run_loop(
    state: AgentState,
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    max_steps: int = 50,
    on_event: EventSink | None = None,
) -> AssistantMessage:
    if max_steps <= 0:
        raise ValueError("max_steps must be > 0")
    emit = on_event if on_event is not None else _noop
    emit(AgentStartEvent())
    last: AssistantMessage | None = None
    steps_this_run = 0
    while steps_this_run < max_steps:
        steps_this_run += 1
        state.step_count += 1
        step = state.step_count
        emit(TurnStartEvent(step=step))
        assistant = _stream_assistant(state, llm, registry, emit)
        last = assistant
        if not assistant.tool_calls:
            emit(AgentEndEvent(reason="completed", message=assistant))
            return assistant
        _execute_tool_calls(state, registry, assistant.tool_calls, emit)
        emit(TurnEndEvent(step=step))
    assert last is not None
    emit(AgentEndEvent(reason="step_limit", message=last))
    return last


def _stream_assistant(
    state: AgentState,
    llm: LLMClient,
    registry: ToolRegistry,
    emit: EventSink,
) -> AssistantMessage:
    emit(MessageStartEvent())
    final: AssistantMessage | None = None
    for event in llm.stream(state.messages, registry.schemas()):
        if isinstance(event, TextDeltaEvent):
            emit(MessageDeltaEvent(kind="text", delta=event.delta))
        elif isinstance(event, ThinkingDeltaEvent):
            emit(MessageDeltaEvent(kind="thinking", delta=event.delta))
        elif isinstance(event, DoneEvent):
            final = event.message
    if final is None:
        raise MiniPiError("LLM stream ended without a done event")
    state.messages.append(final)
    emit(MessageEndEvent(message=final))
    return final


def _execute_tool_calls(
    state: AgentState,
    registry: ToolRegistry,
    calls: list[ToolCall],
    emit: EventSink,
) -> None:
    for call in calls:
        emit(ToolExecutionStartEvent(tool_call=call))
        is_error = False
        try:
            result = registry.execute(call.name, call.arguments)
        except ToolError as exc:
            result = ToolResult(content=f"{type(exc).__name__}: {exc}")
            is_error = True
        if not is_error and result.details and "path" in result.details:
            state.modified_files.add(str(result.details["path"]))
        _append_tool_message(state, call, result, is_error)
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=is_error))


def _append_tool_message(
    state: AgentState, call: ToolCall, result: ToolResult, is_error: bool
) -> None:
    message: Message = ToolMessage(
        tool_call_id=call.id,
        name=call.name,
        content=result.content,
        is_error=is_error,
    )
    state.messages.append(message)


def _noop(event: AgentEvent) -> None:
    pass
```

- [ ] **Step 5: 运行测试通过**

Run: `uv run pytest tests/test_loop.py -v`
Expected: `9 passed`

- [ ] **Step 6: Commit**

```bash
git add mini_pi/agent/loop.py tests/conftest.py tests/test_loop.py
git commit -m "feat: add synchronous agent loop with tool execution and events"
```

---

## M3 Agent Loop 完善

### Task M3.1: LLM 错误处理（stop_reason=error）

**Files:**
- Modify: `mini_pi/agent/loop.py`
- Test: `tests/test_loop.py`（追加）

- [ ] **Step 1: 追加失败测试到 `tests/test_loop.py`**

```python
from mini_pi.errors import LLMError


def test_llm_error_event_ends_agent(echo_registry, events) -> None:
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([LLMError("api down", retryable=False)])
    result = run_loop(state, llm, echo_registry, on_event=events.append)
    assert result.stop_reason == "error"
    assert result.error_message == "api down"
    assert state.step_count == 1
    assert events[-1].type == "agent_end"
    assert events[-1].reason == "error"
    assert events[-1].error == "api down"


def test_llm_error_message_is_in_transcript(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([LLMError("api down", retryable=False)])
    run_loop(state, llm, echo_registry)
    assert state.messages[-1].stop_reason == "error"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_loop.py -v`
Expected: FAIL（`result.stop_reason == "stop"`，`error_message is None`）

- [ ] **Step 3: 修改 `mini_pi/agent/loop.py`**

`_stream_assistant` 中 import `ErrorEvent` 并处理：

```python
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolMessage,
)
```

```python
        elif isinstance(event, ErrorEvent):
            final = AssistantMessage(stop_reason="error", error_message=event.message)
        elif isinstance(event, DoneEvent):
            final = event.message
```

`run_loop` 内在 `_stream_assistant` 之后、判断 tool_calls 之前插入：

```python
        if assistant.stop_reason == "error":
            emit(AgentEndEvent(reason="error", message=assistant, error=assistant.error_message))
            return assistant
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_loop.py -v`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/agent/loop.py tests/test_loop.py
git commit -m "feat: encode LLM errors as error stop reason in agent loop"
```

---

### Task M3.2: length 截断保护

**Files:**
- Modify: `mini_pi/agent/loop.py`
- Test: `tests/test_loop.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
def test_length_truncated_tool_calls_are_not_executed(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="long")])
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[tool_call("c1", "echo", {"text": "partial"})],
                stop_reason="length",
            ),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].is_error is True
    assert "truncated" in tool_messages[0].content
    assert state.modified_files == set()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_loop.py::test_length_truncated_tool_calls_are_not_executed -v`
Expected: FAIL（工具被真实执行，`is_error is False`）

- [ ] **Step 3: 修改 `run_loop`**

在 error 分支之后插入：

```python
        if assistant.stop_reason == "length":
            _record_truncated_calls(state, assistant.tool_calls, emit)
            emit(TurnEndEvent(step=step))
            continue
```

新增函数：

```python
_TRUNCATED_MESSAGE = (
    "Tool call was truncated because the model reached the output token limit. "
    "Re-issue the call with complete arguments."
)


def _record_truncated_calls(
    state: AgentState, calls: list[ToolCall], emit: EventSink
) -> None:
    for call in calls:
        emit(ToolExecutionStartEvent(tool_call=call))
        result = ToolResult(content=_TRUNCATED_MESSAGE)
        _append_tool_message(state, call, result, is_error=True)
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=True))
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_loop.py -v`
Expected: `12 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/agent/loop.py tests/test_loop.py
git commit -m "feat: reject truncated tool calls on output length limit"
```

---

### Task M3.3: Agent 封装与 system prompt

**Files:**
- Create: `mini_pi/agent/prompt.py`
- Create: `mini_pi/agent/agent.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: 写失败测试 `tests/test_agent.py`**

```python
from __future__ import annotations

import pytest

from mini_pi.agent.agent import Agent
from mini_pi.agent.prompt import build_system_prompt
from mini_pi.errors import ToolError
from mini_pi.llm.types import AssistantMessage, SystemMessage, ToolMessage, UserMessage
from mini_pi.workspace.workspace import Workspace
from tests.conftest import EchoTool, FakeLLMClient, assistant


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    return Workspace(tmp_path)


@pytest.fixture
def registry(workspace):
    from mini_pi.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def make_agent(workspace, registry, script) -> Agent:
    llm = FakeLLMClient(script)
    return Agent(llm=llm, registry=registry, workspace=workspace, max_steps=5)


def test_run_injects_system_and_user_messages(workspace, registry) -> None:
    agent = make_agent(workspace, registry, [assistant("ok")])
    result = agent.run("do something")
    assert result.content == "ok"
    assert isinstance(agent.state.messages[0], SystemMessage)
    tools_section = agent.state.messages[0].content.split("# Tools")[1]
    assert "- echo:" in tools_section
    assert "read_file" not in tools_section
    assert isinstance(agent.state.messages[1], UserMessage)
    assert agent.state.messages[1].content == "do something"


def test_system_message_is_not_duplicated_across_runs(workspace, registry) -> None:
    agent = make_agent(workspace, registry, [assistant("first"), assistant("second")])
    agent.run("one")
    agent.run("two")
    systems = [m for m in agent.state.messages if isinstance(m, SystemMessage)]
    assert len(systems) == 1


def test_reset_clears_state(workspace, registry) -> None:
    agent = make_agent(workspace, registry, [assistant("ok")])
    agent.run("task")
    agent.reset()
    assert agent.state.messages == []
    assert agent.state.step_count == 0
    assert agent.state.modified_files == set()


def test_empty_task_is_rejected(workspace, registry) -> None:
    agent = make_agent(workspace, registry, [])
    with pytest.raises(ValueError, match="empty"):
        agent.run("   ")


def test_invalid_max_steps_is_rejected(workspace, registry) -> None:
    with pytest.raises(ValueError, match="max_steps"):
        Agent(llm=FakeLLMClient([]), registry=registry, workspace=workspace, max_steps=0)


def test_prompt_contains_environment(workspace) -> None:
    prompt = build_system_prompt(workspace=workspace, tools=[])
    assert str(workspace.root) in prompt
    assert "mini-pi" in prompt
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL，`No module named 'mini_pi.agent.prompt'`

- [ ] **Step 3: 写 `mini_pi/agent/prompt.py`**

```python
from __future__ import annotations

import platform

from mini_pi.llm.types import ToolSchema
from mini_pi.workspace.workspace import Workspace


def build_system_prompt(*, workspace: Workspace, tools: list[ToolSchema]) -> str:
    tool_lines = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
    return f"""You are mini-pi, a coding agent working inside a local workspace.

# Environment
- Workspace root: {workspace.root}
- Platform: {platform.system().lower()}

# Working rules
- All paths are resolved inside the workspace; paths outside the workspace are rejected.
- Before changing code, locate it with search_code and read it with read_file. Never guess file contents.
- Prefer edit_file for minimal changes; use write_file only for new files or full rewrites.
- Verify changes with run_command (tests/build/lint) and inspect diffs with git_diff.
- Tool errors are returned to you as error observations; read them and adjust instead of repeating the same call.
- When the task is complete, stop calling tools and summarize what changed and how it was verified.

# Tools
{tool_lines}
"""
```

- [ ] **Step 4: 写 `mini_pi/agent/agent.py`**

```python
from __future__ import annotations

from collections.abc import Callable

from mini_pi.agent.events import AgentEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.prompt import build_system_prompt
from mini_pi.agent.state import AgentState
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import AssistantMessage, SystemMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from mini_pi.workspace.workspace import Workspace


class Agent:
    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        workspace: Workspace,
        max_steps: int = 50,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be > 0")
        self._llm = llm
        self._registry = registry
        self._workspace = workspace
        self._max_steps = max_steps
        self._on_event = on_event
        self._system_prompt = build_system_prompt(
            workspace=workspace, tools=registry.schemas()
        )
        self.state = AgentState()

    def run(self, task: str) -> AssistantMessage:
        if not task.strip():
            raise ValueError("task must not be empty")
        if not self.state.messages:
            self.state.messages.append(SystemMessage(content=self._system_prompt))
        self.state.messages.append(UserMessage(content=task))
        return run_loop(
            self.state,
            self._llm,
            self._registry,
            max_steps=self._max_steps,
            on_event=self._on_event,
        )

    def reset(self) -> None:
        self.state.messages.clear()
        self.state.step_count = 0
        self.state.modified_files.clear()
```

- [ ] **Step 5: 运行测试通过**

Run: `uv run pytest tests/test_agent.py -v`
Expected: `6 passed`

- [ ] **Step 6: 完整回归并提交**

Run: `uv run pytest -v`
Expected: 全部通过（M1-M3）

```bash
git add mini_pi/agent/prompt.py mini_pi/agent/agent.py tests/test_agent.py
git commit -m "feat: add Agent wrapper and system prompt builder"
```

> M3 完成标准：`uv run pytest` 全绿；Loop 的错误、截断、step limit、事件序列均有测试覆盖。

## M4 文件 / Shell Tool

### Task M4.1: Workspace 安全边界

**Files:**
- Create: `mini_pi/workspace/__init__.py`（空文件）
- Create: `mini_pi/workspace/workspace.py`
- Test: `tests/test_workspace.py`

- [ ] **Step 1: 写失败测试 `tests/test_workspace.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import WorkspaceViolationError
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_resolve_relative_path(workspace: Workspace) -> None:
    assert workspace.resolve("src/app.py") == workspace.root / "src" / "app.py"


def test_resolve_dot_is_root(workspace: Workspace) -> None:
    assert workspace.resolve(".") == workspace.root


def test_resolve_traversal_inside_is_allowed(workspace: Workspace) -> None:
    assert workspace.resolve("src/../src/app.py") == workspace.root / "src" / "app.py"


def test_absolute_path_inside_is_allowed(workspace: Workspace) -> None:
    assert workspace.resolve(workspace.root / "src" / "app.py").name == "app.py"


def test_parent_escape_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolationError, match="escapes workspace"):
        workspace.resolve("../secret.txt")


def test_absolute_escape_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolationError, match="escapes workspace"):
        workspace.resolve("/etc/passwd")


def test_symlink_file_escape_is_rejected(workspace: Workspace) -> None:
    outside = workspace.root.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace.root / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceViolationError):
        workspace.read_text("link.txt")


def test_symlink_dir_escape_is_rejected(workspace: Workspace) -> None:
    outside = workspace.root.parent / "outside_dir"
    outside.mkdir()
    (outside / "data.txt").write_text("secret", encoding="utf-8")
    (workspace.root / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceViolationError):
        workspace.read_text("linkdir/data.txt")


def test_read_text(workspace: Workspace) -> None:
    assert workspace.read_text("src/app.py") == "print('hi')\n"


def test_write_text_is_atomic_and_creates_parents(workspace: Workspace) -> None:
    target = workspace.write_text("pkg/sub/new.txt", "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    leftovers = list((workspace.root / "pkg" / "sub").glob(".*tmp"))
    assert leftovers == []


def test_relative_uses_posix_separators(workspace: Workspace) -> None:
    assert workspace.relative("src/app.py") == "src/app.py"


def test_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        Workspace(tmp_path / "missing")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: FAIL，`No module named 'mini_pi.workspace'`

- [ ] **Step 3: 写 `mini_pi/workspace/workspace.py`**

```python
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from mini_pi.errors import WorkspaceViolationError


class Workspace:
    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_dir():
            raise NotADirectoryError(f"workspace root is not a directory: {candidate}")
        self._root = candidate.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str | Path) -> Path:
        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else self._root / raw
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._root):
            raise WorkspaceViolationError(f"path escapes workspace: {path!r}")
        return resolved

    def relative(self, path: str | Path) -> str:
        return self.resolve(path).relative_to(self._root).as_posix()

    def read_text(self, path: str | Path, *, encoding: str = "utf-8") -> str:
        return self.resolve(path).read_text(encoding=encoding)

    def write_text(self, path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                handle.write(content)
            os.replace(tmp_name, target)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return target
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: `12 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/workspace/__init__.py mini_pi/workspace/workspace.py tests/test_workspace.py
git commit -m "feat: add workspace path guard and atomic writes"
```

---

### Task M4.2: 截断工具

**Files:**
- Create: `mini_pi/tools/truncate.py`
- Test: `tests/test_truncate.py`

- [ ] **Step 1: 写失败测试 `tests/test_truncate.py`**

```python
from __future__ import annotations

import pytest

from mini_pi.tools.truncate import truncate_text


def test_head_lines() -> None:
    text, truncated = truncate_text("\n".join(str(i) for i in range(10)), max_lines=3, max_bytes=10_000)
    assert text == "0\n1\n2"
    assert truncated is True


def test_tail_lines() -> None:
    text, truncated = truncate_text(
        "\n".join(str(i) for i in range(10)), max_lines=3, max_bytes=10_000, keep="tail"
    )
    assert text == "7\n8\n9"
    assert truncated is True


def test_head_bytes() -> None:
    text, truncated = truncate_text("a" * 100, max_lines=10, max_bytes=10)
    assert len(text) <= 9
    assert truncated is True


def test_tail_bytes() -> None:
    lines = ["x" * 20, "y" * 20, "z" * 20]
    text, truncated = truncate_text("\n".join(lines), max_lines=10, max_bytes=45, keep="tail")
    assert text == "z" * 20
    assert truncated is True


def test_no_truncation() -> None:
    text, truncated = truncate_text("short", max_lines=10, max_bytes=100)
    assert text == "short"
    assert truncated is False


def test_invalid_limits() -> None:
    with pytest.raises(ValueError, match="limits"):
        truncate_text("x", max_lines=0, max_bytes=10)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_truncate.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.truncate'`

- [ ] **Step 3: 写 `mini_pi/tools/truncate.py`**

```python
from __future__ import annotations

from typing import Literal


def truncate_text(
    text: str, *, max_lines: int, max_bytes: int, keep: Literal["head", "tail"] = "head"
) -> tuple[str, bool]:
    if max_lines <= 0 or max_bytes <= 0:
        raise ValueError("limits must be > 0")
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines] if keep == "head" else lines[-max_lines:]
        truncated = True

    def encoded_length(candidate: list[str]) -> int:
        return len("\n".join(candidate).encode("utf-8"))

    while lines and encoded_length(lines) > max_bytes:
        lines.pop() if keep == "head" else lines.pop(0)
        truncated = True
    return "\n".join(lines), truncated
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_truncate.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/truncate.py tests/test_truncate.py
git commit -m "feat: add line and byte truncation helper"
```

---

### Task M4.3: 进程执行器（超时杀进程组）

**Files:**
- Create: `mini_pi/tools/process.py`
- Test: `tests/test_process.py`

- [ ] **Step 1: 写失败测试 `tests/test_process.py`**

```python
from __future__ import annotations

import sys
import time
from pathlib import Path

from mini_pi.tools.process import ProcessResult, run_process, run_shell


def test_run_process_captures_stdout(tmp_path: Path) -> None:
    result = run_process([sys.executable, "-c", "print('hello')"], cwd=tmp_path, timeout_s=30)
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.timed_out is False


def test_run_process_captures_stderr_and_exit_code(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = run_process([sys.executable, "-c", code], cwd=tmp_path, timeout_s=30)
    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_run_shell_executes_pipeline(tmp_path: Path) -> None:
    result = run_shell("echo hi | tr a-z A-Z", cwd=tmp_path, timeout_s=30)
    assert result.exit_code == 0
    assert result.stdout.strip() == "HI"


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout_s=1
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 10
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_process.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.process'`

- [ ] **Step 3: 写 `mini_pi/tools/process.py`**

```python
from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_process(argv: Sequence[str], *, cwd: Path, timeout_s: int) -> ProcessResult:
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return _communicate(process, timeout_s)


def run_shell(command: str, *, cwd: Path, timeout_s: int) -> ProcessResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return _communicate(process, timeout_s)


def _communicate(process: subprocess.Popen[str], timeout_s: int) -> ProcessResult:
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return ProcessResult(
            exit_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return ProcessResult(
            exit_code=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_process.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/process.py tests/test_process.py
git commit -m "feat: add subprocess runner with process-group timeout kill"
```

---

### Task M4.4: read_file

**Files:**
- Create: `mini_pi/tools/read.py`
- Test: `tests/test_read.py`

- [ ] **Step 1: 写失败测试 `tests/test_read.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import ToolError, WorkspaceViolationError
from mini_pi.tools.read import ReadFileTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path) -> ReadFileTool:
    (tmp_path / "notes.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return ReadFileTool(Workspace(tmp_path))


def test_reads_whole_file(tool: ReadFileTool) -> None:
    result = tool.execute(path="notes.txt")
    assert "line1\nline2\nline3" in result.content
    assert result.details == {"path": "notes.txt", "total_lines": 3}


def test_offset_and_limit(tool: ReadFileTool) -> None:
    result = tool.execute(path="notes.txt", offset=2, limit=1)
    assert result.content.splitlines()[0] == "line2"
    assert "Use offset=3" in result.content


def test_offset_beyond_end_is_error(tool: ReadFileTool) -> None:
    with pytest.raises(ToolError, match="beyond end of file"):
        tool.execute(path="notes.txt", offset=99)


def test_missing_file_is_error(tool: ReadFileTool) -> None:
    with pytest.raises(ToolError, match="not a file"):
        tool.execute(path="missing.txt")


def test_escape_is_rejected(tool: ReadFileTool) -> None:
    with pytest.raises(WorkspaceViolationError):
        tool.execute(path="../secret.txt")


def test_binary_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    tool = ReadFileTool(Workspace(tmp_path))
    with pytest.raises(ToolError, match="binary"):
        tool.execute(path="bin.dat")


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00\x41")
    tool = ReadFileTool(Workspace(tmp_path))
    with pytest.raises(ToolError):
        tool.execute(path="bad.txt")


def test_line_truncation_adds_hint(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("\n".join(f"l{i}" for i in range(5000)), encoding="utf-8")
    tool = ReadFileTool(Workspace(tmp_path))
    result = tool.execute(path="long.txt")
    assert "[Showing lines 1-2000 of 5000. Use offset=2001 to continue.]" in result.content
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_read.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.read'`

- [ ] **Step 3: 写 `mini_pi/tools/read.py`**

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.errors import ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    offset: int = Field(default=1, ge=1, description="1-based line number to start reading from.")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to read.")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace. Supports offset/limit paging and reports a continuation hint when truncated."
    args_model = ReadFileArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str, offset: int = 1, limit: int | None = None) -> ToolResult:
        rel = self._workspace.relative(path)
        resolved = self._workspace.resolve(path)
        if not resolved.is_file():
            raise ToolError(f"not a file: {rel}")
        data = resolved.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolError(f"binary file is not supported: {rel}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not valid UTF-8: {rel} ({exc})") from exc
        lines = text.splitlines()
        total = len(lines)
        if offset > total:
            raise ToolError(f"offset {offset} is beyond end of file ({total} lines)")
        window = lines[offset - 1 : offset - 1 + (limit or self.max_lines)]
        body, truncated = truncate_text(
            "\n".join(window), max_lines=self.max_lines, max_bytes=self.max_bytes
        )
        if body == "" and window:
            return ToolResult(
                content=f"Selected line is too long to display (byte cap {self.max_bytes}). Use run_command with sed or awk to inspect it.",
                details={"path": rel, "total_lines": total},
            )
        shown = body.count("\n") + 1 if body else 0
        last = offset + shown - 1
        if truncated or last < total:
            body += (
                f"\n[Showing lines {offset}-{last} of {total}. "
                f"Use offset={last + 1} to continue.]"
            )
        return ToolResult(content=body, details={"path": rel, "total_lines": total})
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_read.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/read.py tests/test_read.py
git commit -m "feat: add read_file tool"
```

---

### Task M4.5: write_file

**Files:**
- Create: `mini_pi/tools/write.py`
- Test: `tests/test_write.py`

- [ ] **Step 1: 写失败测试 `tests/test_write.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import WorkspaceViolationError
from mini_pi.tools.write import WriteFileTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_writes_new_file_and_creates_parents(workspace: Workspace) -> None:
    tool = WriteFileTool(workspace)
    result = tool.execute(path="pkg/mod.py", content="x = 1\n")
    assert (workspace.root / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert result.content == "Wrote 6 bytes to pkg/mod.py."
    assert result.details == {"path": "pkg/mod.py"}


def test_overwrites_existing_file(workspace: Workspace) -> None:
    target = workspace.root / "a.txt"
    target.write_text("old", encoding="utf-8")
    WriteFileTool(workspace).execute(path="a.txt", content="new")
    assert target.read_text(encoding="utf-8") == "new"


def test_escape_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolationError):
        WriteFileTool(workspace).execute(path="../out.txt", content="x")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_write.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.write'`

- [ ] **Step 3: 写 `mini_pi/tools/write.py`**

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.tools.base import Tool, ToolResult
from mini_pi.workspace.workspace import Workspace


class WriteFileArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    content: str = Field(description="Full file content to write.")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or fully rewrite a file inside the workspace. Parent directories are created automatically and writes are atomic."
    args_model = WriteFileArgs

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str, content: str) -> ToolResult:
        rel = self._workspace.relative(path)
        self._workspace.write_text(path, content)
        size = len(content.encode("utf-8"))
        return ToolResult(content=f"Wrote {size} bytes to {rel}.", details={"path": rel})
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_write.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/write.py tests/test_write.py
git commit -m "feat: add atomic write_file tool"
```

---

### Task M4.6: edit_file

**Files:**
- Create: `mini_pi/tools/edit.py`
- Test: `tests/test_edit.py`

- [ ] **Step 1: 写失败测试 `tests/test_edit.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.edit import EditFileTool, EditSpec
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_unique_replacement(workspace: Workspace) -> None:
    tool = EditFileTool(workspace)
    result = tool.execute(path="app.py", edits=[EditSpec(old_text="a - b", new_text="a + b")])
    assert (workspace.root / "app.py").read_text(encoding="utf-8").endswith("return a + b\n")
    assert result.content == "Replaced 1 block(s) in app.py."
    assert result.details is not None
    assert "-    return a - b" in result.details["diff"]
    assert "+    return a + b" in result.details["diff"]


def test_multiple_edits_use_original_offsets(workspace: Workspace) -> None:
    path = workspace.root / "multi.txt"
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    tool = EditFileTool(workspace)
    result = tool.execute(
        path="multi.txt",
        edits=[
            EditSpec(old_text="alpha", new_text="ALPHA-LONGER"),
            EditSpec(old_text="gamma", new_text="GAMMA"),
        ],
    )
    assert path.read_text(encoding="utf-8") == "ALPHA-LONGER beta GAMMA\n"
    assert result.content == "Replaced 2 block(s) in multi.txt."


def test_missing_old_text_is_error(workspace: Workspace) -> None:
    tool = EditFileTool(workspace)
    with pytest.raises(ToolArgumentError, match="not found"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="nope", new_text="x")])


def test_ambiguous_old_text_is_error(workspace: Workspace) -> None:
    path = workspace.root / "dup.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    tool = EditFileTool(workspace)
    with pytest.raises(ToolArgumentError, match="matches 2 times"):
        tool.execute(path="dup.txt", edits=[EditSpec(old_text="same", new_text="other")])


def test_overlapping_edits_are_error(workspace: Workspace) -> None:
    path = workspace.root / "overlap.txt"
    path.write_text("abcdef\n", encoding="utf-8")
    tool = EditFileTool(workspace)
    with pytest.raises(ToolArgumentError, match="overlap"):
        tool.execute(
            path="overlap.txt",
            edits=[
                EditSpec(old_text="abcd", new_text="x"),
                EditSpec(old_text="cdef", new_text="y"),
            ],
        )


def test_empty_old_text_is_error(workspace: Workspace) -> None:
    tool = EditFileTool(workspace)
    with pytest.raises(ToolArgumentError, match="must not be empty"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="", new_text="x")])


def test_no_change_is_error(workspace: Workspace) -> None:
    tool = EditFileTool(workspace)
    with pytest.raises(ToolArgumentError, match="no change"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="a - b", new_text="a - b")])


def test_missing_file_is_error(workspace: Workspace) -> None:
    tool = EditFileTool(workspace)
    with pytest.raises(ToolError, match="not a file"):
        tool.execute(path="missing.py", edits=[EditSpec(old_text="a", new_text="b")])
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_edit.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.edit'`

- [ ] **Step 3: 写 `mini_pi/tools/edit.py`**

```python
from __future__ import annotations

import difflib

from pydantic import BaseModel, Field

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.workspace.workspace import Workspace


class EditSpec(BaseModel):
    old_text: str = Field(description="Exact text to replace; must match exactly once in the file.")
    new_text: str = Field(description="Replacement text.")


class EditFileArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    edits: list[EditSpec] = Field(min_length=1, description="Non-overlapping replacements applied to the original file.")


class EditFileTool(Tool):
    name = "edit_file"
    description = "Apply exact, unique text replacements to an existing file. All edits match the original content and must not overlap. Returns a unified diff."
    args_model = EditFileArgs

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str, edits: list[EditSpec]) -> ToolResult:
        rel = self._workspace.relative(path)
        resolved = self._workspace.resolve(path)
        if not resolved.is_file():
            raise ToolError(f"not a file: {rel}")
        original = self._workspace.read_text(path)
        spans: list[tuple[int, int, EditSpec]] = []
        for index, edit in enumerate(edits):
            if edit.old_text == "":
                raise ToolArgumentError(f"edit[{index}].old_text must not be empty")
            count = original.count(edit.old_text)
            if count == 0:
                raise ToolArgumentError(f"edit[{index}].old_text not found in {rel}")
            if count > 1:
                raise ToolArgumentError(
                    f"edit[{index}].old_text matches {count} times in {rel}; add more context to make it unique"
                )
            start = original.index(edit.old_text)
            spans.append((start, start + len(edit.old_text), edit))
        ordered = sorted(spans, key=lambda item: item[0])
        for (_, end, _), (next_start, _, _) in zip(ordered, ordered[1:]):
            if next_start < end:
                raise ToolArgumentError(f"edits overlap in {rel}")
        updated = original
        for start, end, edit in sorted(spans, key=lambda item: item[0], reverse=True):
            updated = updated[:start] + edit.new_text + updated[end:]
        if updated == original:
            raise ToolArgumentError(f"edits produce no change in {rel}")
        self._workspace.write_text(path, updated)
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        return ToolResult(
            content=f"Replaced {len(edits)} block(s) in {rel}.",
            details={"path": rel, "diff": diff},
        )
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_edit.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/edit.py tests/test_edit.py
git commit -m "feat: add exact-match edit_file tool"
```

---

### Task M4.7: search_code

**Files:**
- Create: `mini_pi/tools/search.py`
- Test: `tests/test_search.py`

- [ ] **Step 1: 写失败测试 `tests/test_search.py`**

```python
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mini_pi.errors import ToolArgumentError
from mini_pi.tools.search import SearchCodeTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SearchCodeTool:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("value = 42\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("add documentation here\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("add ignored\n", encoding="utf-8")
    return SearchCodeTool(Workspace(tmp_path))


def test_literal_search(tool: SearchCodeTool) -> None:
    result = tool.execute(pattern="add")
    lines = result.content.splitlines()
    assert "src/app.py:1:def add(a, b):" in lines
    assert "notes.md:1:add documentation here" in lines
    assert all(".venv" not in line for line in lines)
    assert result.details is not None
    assert result.details["engine"] == "python"


def test_regex_search(tool: SearchCodeTool) -> None:
    result = tool.execute(pattern=r"return a \+ b", is_regex=True)
    assert "src/app.py:2:    return a + b" in result.content


def test_glob_filter(tool: SearchCodeTool) -> None:
    result = tool.execute(pattern="add", glob="*.py")
    assert "notes.md" not in result.content


def test_no_matches(tool: SearchCodeTool) -> None:
    result = tool.execute(pattern="does-not-exist")
    assert result.content == "No matches found"


def test_limit_adds_truncation_hint(tool: SearchCodeTool) -> None:
    result = tool.execute(pattern="add", limit=1)
    assert "[Truncated at 1 matches" in result.content


def test_invalid_regex_is_argument_error(tool: SearchCodeTool) -> None:
    with pytest.raises(ToolArgumentError, match="invalid regex"):
        tool.execute(pattern="[", is_regex=True)


def test_search_single_file(tool: SearchCodeTool) -> None:
    result = tool.execute(pattern="add", path="src/app.py")
    assert "src/app.py:1" in result.content
    assert "notes.md" not in result.content


def test_binary_files_are_skipped(tool: SearchCodeTool, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00add\x00")
    result = tool.execute(pattern="add")
    assert "blob.bin" not in result.content


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_rg_engine(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    tool = SearchCodeTool(Workspace(tmp_path))
    result = tool.execute(pattern="needle")
    assert result.details is not None
    assert result.details["engine"] == "rg"
    assert "a.py:1:needle" in result.content
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.search'`

- [ ] **Step 3: 写 `mini_pi/tools/search.py`**

```python
from __future__ import annotations

import fnmatch
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_process
from mini_pi.workspace.workspace import Workspace


class SearchCodeArgs(BaseModel):
    pattern: str = Field(description="Literal text or regular expression to search for.")
    path: str = Field(default=".", description="File or directory to search, relative to the workspace root.")
    glob: str | None = Field(default=None, description="Only search files matching this glob, e.g. '*.py'.")
    is_regex: bool = Field(default=False, description="Treat pattern as a regular expression.")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of matching lines to return.")


class SearchCodeTool(Tool):
    name = "search_code"
    description = "Search text or regex across workspace files and return file:line:text matches. Skips .git/.venv/node_modules and binary files."
    args_model = SearchCodeArgs
    skip_dirs = frozenset(
        {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea"}
    )
    max_line_chars = 500
    max_file_bytes = 1_000_000

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        is_regex: bool = False,
        limit: int = 100,
    ) -> ToolResult:
        if not pattern:
            raise ToolArgumentError("pattern must not be empty")
        if is_regex:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ToolArgumentError(f"invalid regex pattern: {exc}") from exc
        base = self._workspace.resolve(path)
        if not base.exists():
            raise ToolError(f"path not found: {self._workspace.relative(path)}")
        rg = shutil.which("rg")
        if rg is not None and base.is_dir():
            engine = "rg"
            matches, truncated = self._search_rg(rg, pattern, base, glob, is_regex, limit)
        else:
            engine = "python"
            matches, truncated = self._search_python(pattern, base, glob, is_regex, limit)
        if not matches:
            return ToolResult(content="No matches found", details={"engine": engine, "count": 0})
        body = "\n".join(matches)
        if truncated:
            body += f"\n[Truncated at {limit} matches. Narrow the search or raise limit.]"
        return ToolResult(content=body, details={"engine": engine, "count": len(matches)})

    def _search_rg(
        self,
        rg: str,
        pattern: str,
        base: Path,
        glob: str | None,
        is_regex: bool,
        limit: int,
    ) -> tuple[list[str], bool]:
        argv = [rg, "--line-number", "--no-heading", "--color", "never"]
        if not is_regex:
            argv.append("--fixed-strings")
        if glob:
            argv.extend(["--glob", glob])
        argv.extend(["--", pattern, str(base)])
        result = run_process(argv, cwd=self._workspace.root, timeout_s=60)
        if result.exit_code not in (0, 1):
            raise ToolError(f"rg failed (exit {result.exit_code}): {result.stderr.strip()}")
        matches: list[str] = []
        truncated = False
        for line in result.stdout.splitlines():
            if len(matches) >= limit:
                truncated = True
                break
            parts = line.split(":", 2)
            if len(parts) != 3:
                raise ToolError(f"unexpected rg output line: {line!r}")
            file_part, line_no, text = parts
            rel = Path(file_part).resolve().relative_to(self._workspace.root).as_posix()
            matches.append(f"{rel}:{line_no}:{text[: self.max_line_chars]}")
        return matches, truncated

    def _search_python(
        self,
        pattern: str,
        base: Path,
        glob: str | None,
        is_regex: bool,
        limit: int,
    ) -> tuple[list[str], bool]:
        files = [base] if base.is_file() else list(self._iter_files(base))
        matches: list[str] = []
        truncated = False
        for file_path in files:
            if len(matches) >= limit:
                truncated = True
                break
            rel = self._workspace.relative(file_path)
            if glob and not (fnmatch.fnmatch(file_path.name, glob) or fnmatch.fnmatch(rel, glob)):
                continue
            try:
                if file_path.stat().st_size > self.max_file_bytes:
                    continue
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if len(matches) >= limit:
                    truncated = True
                    break
                hit = re.search(pattern, line) if is_regex else pattern in line
                if hit:
                    matches.append(f"{rel}:{line_no}:{line[: self.max_line_chars]}")
        return matches, truncated

    def _iter_files(self, root: Path) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in self.skip_dirs)
            for name in sorted(filenames):
                yield Path(dirpath) / name
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_search.py -v`
Expected: `9 passed`（无 rg 时 `8 passed, 1 skipped`）

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/search.py tests/test_search.py
git commit -m "feat: add search_code tool with rg and python engines"
```

---

### Task M4.8: run_command

**Files:**
- Create: `mini_pi/tools/bash.py`
- Test: `tests/test_bash.py`

- [ ] **Step 1: 写失败测试 `tests/test_bash.py`**

```python
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_pi.tools.bash import RunCommandTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path) -> RunCommandTool:
    return RunCommandTool(Workspace(tmp_path))


def test_successful_command(tool: RunCommandTool) -> None:
    result = tool.execute(command="echo hello")
    assert "exit_code: 0" in result.content
    assert "hello" in result.content
    assert result.details is not None
    assert result.details["exit_code"] == 0


def test_non_zero_exit_is_a_normal_result(tool: RunCommandTool) -> None:
    result = tool.execute(command="exit 3")
    assert "exit_code: 3" in result.content


def test_stderr_is_preserved(tool: RunCommandTool) -> None:
    result = tool.execute(command="echo oops 1>&2")
    assert "oops" in result.content
    assert "stderr:" in result.content


def test_cwd_is_workspace_root(tool: RunCommandTool, tmp_path: Path) -> None:
    tool.execute(command="pwd > cwd.txt")
    recorded = Path((tmp_path / "cwd.txt").read_text(encoding="utf-8").strip()).resolve()
    assert recorded == tmp_path.resolve()


def test_timeout_is_reported(tool: RunCommandTool) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    result = tool.execute(command=command, timeout=1)
    assert "timed out" in result.content
    assert result.details is not None
    assert result.details["timed_out"] is True


def test_invalid_timeout_is_rejected_by_schema(tool: RunCommandTool) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunCommandTool.args_model.model_validate({"command": "echo hi", "timeout": 0})
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_bash.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.bash'`

- [ ] **Step 3: 写 `mini_pi/tools/bash.py`**

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_shell
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace


class RunCommandArgs(BaseModel):
    command: str = Field(description="Shell command to run with cwd set to the workspace root.")
    timeout: int = Field(default=120, ge=1, le=600, description="Timeout in seconds.")


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run a shell command in the workspace root. Returns exit code, stdout and stderr. Non-zero exit codes are reported, not raised."
    args_model = RunCommandArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, command: str, timeout: int = 120) -> ToolResult:
        result = run_shell(command, cwd=self._workspace.root, timeout_s=timeout)
        stdout, stdout_truncated = truncate_text(
            result.stdout, max_lines=self.max_lines, max_bytes=self.max_bytes, keep="tail"
        )
        stderr, stderr_truncated = truncate_text(
            result.stderr, max_lines=self.max_lines, max_bytes=self.max_bytes, keep="tail"
        )
        header = f"exit_code: {result.exit_code}"
        if result.timed_out:
            header += f" (timed out after {timeout}s, process group killed)"
        parts = [
            header,
            "stdout:",
            stdout if stdout else "(empty)",
            "stderr:",
            stderr if stderr else "(empty)",
        ]
        if stdout_truncated or stderr_truncated:
            parts.append("[output truncated]")
        return ToolResult(
            content="\n".join(parts),
            details={
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        )
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_bash.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/bash.py tests/test_bash.py
git commit -m "feat: add run_command tool with timeout and output truncation"
```

---

### Task M4.9: git_diff

**Files:**
- Create: `mini_pi/tools/git.py`
- Test: `tests/test_git.py`

- [ ] **Step 1: 写失败测试 `tests/test_git.py`**

```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mini_pi.errors import ToolError
from mini_pi.tools.git import GitDiffTool
from mini_pi.workspace.workspace import Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("old-b\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_unstaged_diff(repo: Path) -> None:
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    tool = GitDiffTool(Workspace(repo))
    result = tool.execute()
    assert "-old" in result.content
    assert "+new" in result.content


def test_staged_diff(repo: Path) -> None:
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    tool = GitDiffTool(Workspace(repo))
    result = tool.execute(staged=True)
    assert "+new" in result.content


def test_no_changes(repo: Path) -> None:
    tool = GitDiffTool(Workspace(repo))
    assert tool.execute().content == "No changes."


def test_path_filter(repo: Path) -> None:
    (repo / "a.txt").write_text("new-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("new-b\n", encoding="utf-8")
    tool = GitDiffTool(Workspace(repo))
    result = tool.execute(path="b.txt")
    assert "new-b" in result.content
    assert "new-a" not in result.content


def test_not_a_repo_is_tool_error(tmp_path: Path) -> None:
    tool = GitDiffTool(Workspace(tmp_path))
    with pytest.raises(ToolError, match="git diff failed"):
        tool.execute()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_git.py -v`
Expected: FAIL，`No module named 'mini_pi.tools.git'`

- [ ] **Step 3: 写 `mini_pi/tools/git.py`**

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.errors import ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_process
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace


class GitDiffArgs(BaseModel):
    path: str | None = Field(default=None, description="Limit the diff to this path inside the workspace.")
    staged: bool = Field(default=False, description="Show staged changes (git diff --cached).")


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show uncommitted changes in the workspace git repository as a unified diff."
    args_model = GitDiffArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str | None = None, staged: bool = False) -> ToolResult:
        argv = ["git", "diff", "--no-color"]
        if staged:
            argv.append("--cached")
        if path is not None:
            argv.extend(["--", self._workspace.relative(path)])
        result = run_process(argv, cwd=self._workspace.root, timeout_s=30)
        if result.exit_code != 0:
            raise ToolError(
                f"git diff failed (exit {result.exit_code}): {result.stderr.strip()}"
            )
        body, truncated = truncate_text(
            result.stdout, max_lines=self.max_lines, max_bytes=self.max_bytes
        )
        if not body.strip():
            body = "No changes."
        elif truncated:
            body += "\n[output truncated]"
        return ToolResult(content=body, details={"staged": staged, "path": path})
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_git.py -v`
Expected: `5 passed`（无 git 则 skipped）

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/git.py tests/test_git.py
git commit -m "feat: add git_diff tool"
```

---

### Task M4.10: 默认工具注册

**Files:**
- Modify: `mini_pi/tools/__init__.py`
- Test: `tests/test_registry_defaults.py`

- [ ] **Step 1: 写失败测试 `tests/test_registry_defaults.py`**

```python
from __future__ import annotations

from pathlib import Path

from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace


def test_default_registry_contains_phase1_tools(tmp_path: Path) -> None:
    registry = build_default_registry(Workspace(tmp_path))
    names = [schema.name for schema in registry.schemas()]
    assert names == [
        "read_file",
        "write_file",
        "edit_file",
        "search_code",
        "run_command",
        "git_diff",
    ]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_registry_defaults.py -v`
Expected: FAIL，`ImportError: cannot import name 'build_default_registry'`

- [ ] **Step 3: 写 `mini_pi/tools/__init__.py`**

```python
from __future__ import annotations

from mini_pi.tools.bash import RunCommandTool
from mini_pi.tools.edit import EditFileTool
from mini_pi.tools.git import GitDiffTool
from mini_pi.tools.read import ReadFileTool
from mini_pi.tools.registry import ToolRegistry
from mini_pi.tools.search import SearchCodeTool
from mini_pi.tools.write import WriteFileTool
from mini_pi.workspace.workspace import Workspace

__all__ = ["build_default_registry"]


def build_default_registry(workspace: Workspace) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    registry.register(WriteFileTool(workspace))
    registry.register(EditFileTool(workspace))
    registry.register(SearchCodeTool(workspace))
    registry.register(RunCommandTool(workspace))
    registry.register(GitDiffTool(workspace))
    return registry
```

- [ ] **Step 4: 运行测试通过并全量回归**

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/__init__.py tests/test_registry_defaults.py
git commit -m "feat: register the six phase-1 tools by default"
```

> M4 完成标准：`uv run pytest` 全绿；路径逃逸、编辑歧义、Shell 超时、二进制文件都有测试覆盖。

---

## M5 真实代码修改闭环

### Task M5.1: CLI 渲染器

**Files:**
- Create: `mini_pi/cli/__init__.py`（空文件）
- Create: `mini_pi/cli/console.py`
- Test: `tests/test_console.py`

- [ ] **Step 1: 写失败测试 `tests/test_console.py`**

```python
from __future__ import annotations

import io

from rich.console import Console

from mini_pi.agent.events import (
    AgentEndEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.llm.types import AssistantMessage, ToolCall
from mini_pi.tools.base import ToolResult


def make_renderer() -> tuple[ConsoleRenderer, io.StringIO]:
    stream = io.StringIO()
    return ConsoleRenderer(Console(file=stream, width=200, no_color=True)), stream


def test_renders_text_deltas() -> None:
    renderer, stream = make_renderer()
    renderer.handle(MessageDeltaEvent(kind="text", delta="Hel"))
    renderer.handle(MessageDeltaEvent(kind="text", delta="lo"))
    renderer.handle(MessageEndEvent(message=AssistantMessage(content="Hello")))
    assert "Hello" in stream.getvalue()


def test_renders_tool_starts_and_results() -> None:
    renderer, stream = make_renderer()
    call = ToolCall(id="c1", name="run_command", arguments={"command": "pytest"})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    renderer.handle(
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="exit_code: 0"), is_error=False)
    )
    output = stream.getvalue()
    assert "run_command" in output
    assert "pytest" in output
    assert "exit_code: 0" in output


def test_renders_errors() -> None:
    renderer, stream = make_renderer()
    renderer.handle(AgentEndEvent(reason="error", error="api down"))
    assert "api down" in stream.getvalue()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_console.py -v`
Expected: FAIL，`No module named 'mini_pi.cli'`

- [ ] **Step 3: 写 `mini_pi/cli/console.py`**

```python
from __future__ import annotations

import json

from rich.console import Console

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)


class ConsoleRenderer:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._printing_text = False

    def handle(self, event: AgentEvent) -> None:
        if isinstance(event, MessageDeltaEvent):
            style = "dim" if event.kind == "thinking" else None
            self.console.print(event.delta, end="", style=style, markup=False, highlight=False)
            self._printing_text = True
        elif isinstance(event, MessageEndEvent):
            if self._printing_text:
                self.console.print()
                self._printing_text = False
        elif isinstance(event, ToolExecutionStartEvent):
            arguments = json.dumps(event.tool_call.arguments, ensure_ascii=False)
            self.console.print(f"→ {event.tool_call.name} {arguments}", style="cyan", markup=False)
        elif isinstance(event, ToolExecutionEndEvent):
            style = "red" if event.is_error else "green"
            preview = event.result.content.splitlines()[0] if event.result.content else "(empty)"
            self.console.print(f"  {preview[:200]}", style=style, markup=False)
        elif isinstance(event, AgentEndEvent):
            self._render_end(event)

    def _render_end(self, event: AgentEndEvent) -> None:
        if event.reason == "step_limit":
            self.console.print(
                "Reached the step limit before finishing the task.", style="yellow", markup=False
            )
        elif event.reason == "error":
            self.console.print(f"Agent stopped with an error: {event.error}", style="red", markup=False)
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_console.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/cli/__init__.py mini_pi/cli/console.py tests/test_console.py
git commit -m "feat: add rich event renderer for the CLI"
```

---

### Task M5.2: CLI 入口（一次性 + 交互式）

**Files:**
- Create: `mini_pi/cli/app.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败测试 `tests/test_cli.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app, create_llm
from mini_pi.errors import MiniPiError

runner = CliRunner()


def test_create_llm_rejects_unknown_provider() -> None:
    import typer

    with pytest.raises(typer.BadParameter, match="unsupported provider"):
        create_llm("ollama", None)


def test_missing_api_key_exits_with_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = runner.invoke(app, ["hello", "--cwd", str(tmp_path)])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_help_lists_options() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--max-steps" in result.output


def test_missing_api_key_raises_minipi_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MiniPiError):
        create_llm("deepseek", None)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL，`No module named 'mini_pi.cli.app'`

- [ ] **Step 3: 写 `mini_pi/cli/app.py`**

```python
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from mini_pi.agent.agent import Agent
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.errors import MiniPiError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

app = typer.Typer(add_completion=False, help="mini-pi: a lightweight Python coding agent")


def create_llm(provider: str, model: str | None) -> LLMClient:
    if provider == "openai":
        return OpenAIClient(model=model or "gpt-4o-mini")
    if provider == "deepseek":
        return DeepSeekClient(model=model or "deepseek-chat")
    raise typer.BadParameter(f"unsupported provider: {provider!r} (expected 'openai' or 'deepseek')")


@app.command()
def cli(
    prompt: str | None = typer.Argument(None, help="Task to run once; omit to start an interactive session."),
    provider: str = typer.Option("openai", "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    max_steps: int = typer.Option(50, "--max-steps", min=1),
) -> None:
    try:
        llm = create_llm(provider, model)
    except MiniPiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    workspace = Workspace(cwd)
    renderer = ConsoleRenderer()
    agent = Agent(
        llm=llm,
        registry=build_default_registry(workspace),
        workspace=workspace,
        max_steps=max_steps,
        on_event=renderer.handle,
    )
    if prompt is not None:
        agent.run(prompt)
        return
    console = Console()
    console.print("mini-pi interactive mode. Commands: /reset, /exit")
    while True:
        try:
            line = input("mini-pi> ")
        except (EOFError, KeyboardInterrupt):
            break
        stripped = line.strip()
        if stripped in {"/exit", "/quit"}:
            break
        if stripped == "/reset":
            agent.reset()
            console.print("context cleared")
            continue
        if not stripped:
            continue
        try:
            agent.run(stripped)
        except KeyboardInterrupt:
            console.print("interrupted", style="yellow")


def main() -> None:
    app()
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: `4 passed`

- [ ] **Step 5: 验证控制台脚本已安装**

Run: `uv sync && uv run mini-pi --help`
Expected: 输出帮助信息，包含 `--provider` / `--cwd` / `--max-steps`

- [ ] **Step 6: Commit**

```bash
git add mini_pi/cli/app.py tests/test_cli.py
git commit -m "feat: add one-shot and interactive CLI"
```

---

### Task M5.3: 样例项目与真实闭环验收

**Files:**
- Create: `tests/fixtures/sample_project/calculator.py`
- Create: `tests/fixtures/sample_project/test_calculator.py`
- Create: `tests/test_integration_agent.py`

- [ ] **Step 1: 写样例项目（故意留下失败用例）**

`tests/fixtures/sample_project/calculator.py`：

```python
def add(a: float, b: float) -> float:
    return a - b
```

`tests/fixtures/sample_project/test_calculator.py`：

```python
from calculator import add


def test_add() -> None:
    assert add(2, 3) == 5
```

Run: `uv run pytest tests/fixtures/sample_project -q`
Expected: `1 failed`（`assert -1 == 5`）

- [ ] **Step 2: 写集成测试 `tests/test_integration_agent.py`**

```python
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_pi.agent.agent import Agent
from mini_pi.cli.app import create_llm
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


def _credentials_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _credentials_available(), reason="no LLM API key configured"),
]


def test_agent_fixes_failing_test(tmp_path: Path) -> None:
    target = tmp_path / "sample_project"
    shutil.copytree(FIXTURE, target)
    workspace = Workspace(target)
    provider = os.environ.get("MINI_PI_PROVIDER", "openai")
    agent = Agent(
        llm=create_llm(provider, None),
        registry=build_default_registry(workspace),
        workspace=workspace,
        max_steps=30,
    )
    result = agent.run(
        "Run pytest, find the failing test, fix the code, then run pytest again to verify."
    )
    assert result.stop_reason in {"stop", "tool_calls"}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace.root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "calculator.py" in agent.state.modified_files
```

- [ ] **Step 3: 运行集成测试（需要 Key）**

Run: `MINI_PI_PROVIDER=deepseek uv run pytest tests/test_integration_agent.py -m integration -v`
Expected: `PASSED`；若模型未完成则根据失败信息调整 system prompt（`mini_pi/agent/prompt.py`），不要放宽断言。

- [ ] **Step 4: 人工验收（与 README 第 9 节一致，在副本上执行，避免污染仓库）**

```bash
rm -rf /tmp/mini-pi-demo && cp -R tests/fixtures/sample_project /tmp/mini-pi-demo
uv run mini-pi --provider deepseek --cwd /tmp/mini-pi-demo \
  "运行 pytest，定位失败原因并修复，修复后再次运行 pytest 验证"
```

观察输出应包含：`run_command`（pytest 失败）→ `read_file` / `search_code` → `edit_file` → `run_command`（pytest 通过）→ 最终总结。

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures tests/test_integration_agent.py
git commit -m "test: add sample project and end-to-end agent acceptance test"
```

---

## M6 pytest 完善与收尾

### Task M6.1: 边界用例补全

**Files:**
- Modify: `tests/test_workspace.py` / `tests/test_edit.py` / `tests/test_bash.py` / `tests/test_search.py` / `tests/test_loop.py`

- [ ] **Step 1: 追加边界测试（逐个文件）**

`tests/test_workspace.py`：

```python
def test_write_through_symlinked_parent_is_rejected(workspace: Workspace) -> None:
    outside = workspace.root.parent / "outside_dir"
    outside.mkdir()
    (workspace.root / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceViolationError):
        workspace.write_text("linkdir/new.txt", "x")


def test_expanduser_is_not_a_shortcut(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolationError):
        workspace.resolve("~/secret.txt")
```

`tests/test_edit.py`：

```python
def test_edits_still_apply_when_first_edit_changes_length(workspace: Workspace) -> None:
    path = workspace.root / "grow.txt"
    path.write_text("abc def\n", encoding="utf-8")
    tool = EditFileTool(workspace)
    tool.execute(
        path="grow.txt",
        edits=[
            EditSpec(old_text="abc", new_text="abcdefghij"),
            EditSpec(old_text="def", new_text="DEF"),
        ],
    )
    assert path.read_text(encoding="utf-8") == "abcdefghij DEF\n"
```

`tests/test_bash.py`：

```python
def test_stdout_truncation_marks_details(tool: RunCommandTool) -> None:
    command = f'"{sys.executable}" -c "print(\'x\' * 60000)"'
    result = tool.execute(command=command)
    assert result.details is not None
    assert result.details["stdout_truncated"] is True
    assert "[output truncated]" in result.content
```

`tests/test_search.py`：

```python
def test_binary_content_in_text_extension_is_skipped(tool: SearchCodeTool, tmp_path: Path) -> None:
    (tmp_path / "fake.txt").write_bytes(b"\xff\xfe\x00add")
    result = tool.execute(pattern="add")
    assert "fake.txt" not in result.content
```

`tests/test_loop.py`：

```python
def test_max_steps_one_with_tool_call_reports_step_limit(echo_registry) -> None:
    state = AgentState(messages=[UserMessage(content="once")])
    llm = FakeLLMClient([assistant(tool_calls=[tool_call("c1", "echo", {"text": "x"})])])
    result = run_loop(state, llm, echo_registry, max_steps=1)
    assert result.tool_calls
    assert state.step_count == 1
```

- [ ] **Step 2: 运行新增用例并修复发现的问题**

Run: `uv run pytest -v`
Expected: 全部通过。若暴露实现缺陷，先修实现再提交，不允许绕过测试。

- [ ] **Step 3: Commit**

```bash
git add tests
git commit -m "test: cover workspace, edit, truncation and step-limit edge cases"
```

---

### Task M6.2: 全量回归与文档同步

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/phase1-core-runtime.md`
- Modify: `AGENTS.md`（仅当设计发生偏差时）

- [ ] **Step 1: 全量测试（含集成测试可选）**

Run: `uv run pytest -q`
Expected: 全部通过，无 skipped 异常、无网络访问

Run: `uv run pytest -m integration -q`（可选）
Expected: 有 Key 时通过；无 Key 时全部 skipped

- [ ] **Step 2: 逐项勾选本计划 M1-M6 的复选框，并更新 README 路线图状态**

README 状态改为：

```text
M1 已完成 / M2 已完成 / ... / M6 已完成
```

- [ ] **Step 3: 对照 Phase 1 验收清单**

```text
[ ] Agent 能自主搜索、读取、修改、运行测试并修复失败
[ ] 所有文件操作被限制在 workspace 内
[ ] 工具错误作为 observation 回传，Agent 能自我纠正
[ ] LLM 错误、step limit、length 截断有明确终止行为
[ ] 无真实 API Key 时测试套件仍然全绿
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/plans/phase1-core-runtime.md AGENTS.md
git commit -m "docs: mark phase 1 milestones complete"
```

---

## Phase 1 完成标准（M1-M6）

- `uv run pytest` 全绿，且不依赖网络 / 真实 API
- `uv run pytest -m integration` 在配置 API Key 后通过
- 在 `tests/fixtures/sample_project` 上由真实模型完成“运行测试 → 定位 → 修改 → 验证”闭环
- `README.md` 路线图表、本计划复选框与实际状态一致
- 已知限制记录在 README（如：edit 仅精确匹配、无 Session、无并行工具执行）

---

## 计划自检

**Spec coverage（对照 AGENTS.md 第 5、6、7、8、9、10、11、12、19、20、21 节）：**

| 要求 | 覆盖任务 |
| --- | --- |
| LLM Client（OpenAI/DeepSeek、流式、重试、reasoning 回放） | M1.2-M1.6 |
| Tool Calling / Registry 校验与错误分类 | M2.1-M2.5 |
| Agent Loop（事件、max_steps、错误、length 截断） | M2.4、M3.1-M3.2 |
| Agent 封装与 system prompt | M3.3 |
| Workspace 路径逃逸（`../`、绝对路径、symlink）、原子写 | M4.1、M6.1 |
| read_file / write_file / edit_file / search_code / run_command / git_diff | M4.4-M4.10 |
| Shell Tool（cwd、timeout 杀进程组、stdout/stderr 分离、exit code、双限截断） | M4.3、M4.8 |
| CLI（一次性 + 交互式 + 事件渲染） | M5.1-M5.2 |
| pytest（FakeLLM、tmp_path、integration 排除、无网络） | 全计划 |
| Phase 1 闭环验收 | M5.3、M6.2 |

**Placeholder scan：** 无 TODO / TBD；所有代码步骤均给出可执行代码。

**Type consistency 检查：**

- `run_loop(state, llm, registry, *, max_steps, on_event)`：M2.4 定义，M3.1/M3.2 原地修改，M3.3 Agent 调用一致。
- `ToolResult(content, details)`：M2.1 定义，M2.4、M4.x、M5.1 使用一致。
- `AgentEvent` 判别字段 `type`、`reason ∈ {completed, step_limit, error}`：M2.3 定义，M3/M5 使用一致。
- `ToolError` 子类：M1.3 定义（`errors.py`），M2.2 registry、M2.4 loop、M4.x tools 使用一致。
- `Workspace.resolve/relative/read_text/write_text`：M4.1 定义，M4.4-M4.10、M3.3 使用一致。

