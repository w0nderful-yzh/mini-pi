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
6. 已完成的任务与里程碑在本文档中精简为「交付物 + 验收（含提交号）」摘要，删除完整代码块与步骤；未完成部分保留完整步骤与代码。
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

### Task M2.1: Tool 基类与 ToolResult（已完成）

提交：`645f875`

交付物：`mini_pi/tools/base.py` — `Tool`（声明 `name` / `description` / `args_model`，`schema()` 由 pydantic 模型生成 `ToolSchema`）与 `ToolResult(content, details)`。

验收：`tests/test_tool_base.py` 3 passed。

### Task M2.2: ToolRegistry（已完成）

提交：`8341a25`

交付物：`mini_pi/tools/registry.py` — `register`（重名抛 `ValueError`）、`schemas()`、`execute()`（未注册 → `ToolNotFoundError`；参数校验失败 → `ToolArgumentError`；通过后 `tool.execute(**validated)`）。

验收：`tests/test_registry.py` 6 passed；全量 33 passed。

---

### Task M2.3: AgentState 与 AgentEvent（已完成）

提交：`75b7a7d`

交付物：

- `mini_pi/agent/state.py`：`AgentState(messages, step_count, modified_files)`，`step_count` 为会话累计值
- `mini_pi/agent/events.py`：九类 `AgentEvent`（agent_start / turn_start / message_start / message_delta / message_end / tool_execution_start / tool_execution_end / turn_end / agent_end），以 `type` 为判别字段

验收：`tests/test_agent_events.py` 3 passed；全量 36 passed。

---

### Task M2.4: run_loop 与工具调用循环（已完成）

提交：与本文档压缩同提交（`feat: complete M2.4 agent loop and finalize M2 milestone`）

交付物：

- `mini_pi/agent/loop.py`：`run_loop(state, llm, registry, *, max_steps, on_event)` 纯函数；事件序列 agent_start → turn_start → message_* → tool_execution_* → turn_end → agent_end；`stop_reason=error` / `length` 处理与 Agent 封装在 M3 补充
- `tests/conftest.py`：`FakeLLMClient`（脚本化流式回放 + 上下文记录）、`assistant()` / `tool_call()` 构造器、Echo / Failing / Crash 工具与 fixtures
- `tests/test_loop.py`：9 个用例覆盖 completed、工具 observation、事件序列、ToolError / 未知工具 / 参数错误的 error observation、非预期异常冒泡、step_limit

验收：`uv run pytest` → 45 passed, 2 deselected。

---

## M3 Agent Loop 完善

### Task M3.1: LLM 错误处理（已完成）

提交：本次提交

交付物：`mini_pi/agent/loop.py` — `_stream_assistant` 将 `ErrorEvent` 聚合为 `stop_reason="error"` 的 assistant 消息并写入 transcript；`run_loop` 遇错时先补发 `turn_end`，再以 `agent_end(reason="error", error=...)` 结束，不向调用方抛异常。

验收：`tests/test_loop.py` 新增 2 个用例（error 终止事件、错误消息入 transcript）；全量 50 passed, 2 deselected。

---

### Task M3.2: length 截断保护（已完成）

提交：本次提交

交付物：`mini_pi/agent/loop.py` — `stop_reason="length"` 时不执行任何 tool call，全部转 error observation（提示模型用完整参数重发）后进入下一轮；该轮同样补发 `turn_end`。

验收：`tests/test_loop.py::test_length_truncated_tool_calls_are_not_executed`；全量 51 passed, 2 deselected。

---

### Task M3.3: Agent 封装与 system prompt（已完成）

提交：本次提交

交付物：

- `mini_pi/agent/prompt.py`：`build_system_prompt(*, cwd, tools)`（身份 / 环境 / 工作规则 / 工具清单）
- `mini_pi/agent/agent.py`：`Agent(llm, registry, cwd, max_steps, on_event)`；首次 `run()` 注入 system 消息，`reset()` 清空状态
- 依赖方向修正：Agent 只接收 `cwd: Path`，不依赖 Workspace（Workspace 仅属于 Tool 层）

验收：`tests/test_agent.py` 6 passed；全量 57 passed, 2 deselected。M3 里程碑全部完成。

---

## M4 文件 / Shell Tool

### Task M4.1: Workspace 安全边界（已完成）

提交：本次提交

交付物：`mini_pi/workspace/workspace.py` — `Workspace(root)`（root 必须存在并 resolve）；`resolve`（`~` 展开、绝对/相对路径统一处理、跟随 symlink 后 `is_relative_to` 校验，越界抛 `WorkspaceViolationError`）；`relative`（posix 相对路径）；`read_text`；`write_text`（自动建父目录 + 临时文件 `os.replace` 原子写，失败时清理）。

验收：`tests/test_workspace.py` 12 passed（`../`、绝对路径、symlink 文件/目录逃逸、原子写无残留）；全量 69 passed, 2 deselected。

---

### Task M4.2: 截断工具（已完成）

提交：本次提交

交付物：`mini_pi/tools/truncate.py` — `truncate_text(text, *, max_lines, max_bytes, keep="head"|"tail") -> (text, truncated)`；按整行丢弃，不切半行、不切多字节字符；非法上限抛 `ValueError`。

验收：`tests/test_truncate.py` 6 passed；全量 75 passed, 2 deselected。

---

### Task M4.3: 进程执行器（已完成）

提交：本次提交（有界捕获为后续加固）

交付物：`mini_pi/tools/process.py` — `ProcessResult(exit_code, stdout, stderr, timed_out, stdout_truncated, stderr_truncated)`；`run_process(argv)` 与 `run_shell(command)`；`start_new_session=True` + 超时 `os.killpg` 杀整个进程组；stdout / stderr 由读取线程持续排空，经 `_BoundedCapture` 有界缓冲（默认 1MB/流，`keep="head"|"tail"`），内存恒定，非 0 退出码原样返回。

验收：`tests/test_process.py` 8 passed（超时杀进程组、超大输出封顶、head/tail 保留方向、stderr 同样有界）；全量 83 passed, 2 deselected。

---

### Task M4.4: read

**Files:**
- Create: `mini_pi/tools/read.py`
- Test: `tests/test_read.py`

- [ ] **Step 1: 写失败测试 `tests/test_read.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import ToolError, WorkspaceViolationError
from mini_pi.tools.read import ReadTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path) -> ReadTool:
    (tmp_path / "notes.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return ReadTool(Workspace(tmp_path))


def test_reads_whole_file(tool: ReadTool) -> None:
    result = tool.execute(path="notes.txt")
    assert "line1\nline2\nline3" in result.content
    assert result.details == {"path": "notes.txt", "total_lines": 3}


def test_offset_and_limit(tool: ReadTool) -> None:
    result = tool.execute(path="notes.txt", offset=2, limit=1)
    assert result.content.splitlines()[0] == "line2"
    assert "Use offset=3" in result.content


def test_offset_beyond_end_is_error(tool: ReadTool) -> None:
    with pytest.raises(ToolError, match="beyond end of file"):
        tool.execute(path="notes.txt", offset=99)


def test_missing_file_is_error(tool: ReadTool) -> None:
    with pytest.raises(ToolError, match="not a file"):
        tool.execute(path="missing.txt")


def test_escape_is_rejected(tool: ReadTool) -> None:
    with pytest.raises(WorkspaceViolationError):
        tool.execute(path="../secret.txt")


def test_binary_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    tool = ReadTool(Workspace(tmp_path))
    with pytest.raises(ToolError, match="binary"):
        tool.execute(path="bin.dat")


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00\x41")
    tool = ReadTool(Workspace(tmp_path))
    with pytest.raises(ToolError):
        tool.execute(path="bad.txt")


def test_line_truncation_adds_hint(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("\n".join(f"l{i}" for i in range(5000)), encoding="utf-8")
    tool = ReadTool(Workspace(tmp_path))
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


class ReadArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    offset: int = Field(default=1, ge=1, description="1-based line number to start reading from.")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to read.")


class ReadTool(Tool):
    name = "read"
    description = "Read a UTF-8 text file inside the workspace. Supports offset/limit paging and reports a continuation hint when truncated."
    args_model = ReadArgs
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
                content=f"Selected line is too long to display (byte cap {self.max_bytes}). Use bash with sed or awk to inspect it.",
                details={"path": rel, "total_lines": total},
            )
        shown = body.count("\n") + 1 if body else 0
        last = offset + shown - 1
        if truncated or last < total:
            body += (
                f"\n[Showing lines {offset}-{last} of {total}. "
                f"Use offset={last + 1} to continue.]"
            )
        # 只读工具：details 仅用于展示，不声明 modified_files
        return ToolResult(content=body, details={"path": rel, "total_lines": total})
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_read.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/read.py tests/test_read.py
git commit -m "feat: add read tool"
```

---

### Task M4.5: write

**Files:**
- Create: `mini_pi/tools/write.py`
- Test: `tests/test_write.py`

- [ ] **Step 1: 写失败测试 `tests/test_write.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import WorkspaceViolationError
from mini_pi.tools.write import WriteTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_writes_new_file_and_creates_parents(workspace: Workspace) -> None:
    tool = WriteTool(workspace)
    result = tool.execute(path="pkg/mod.py", content="x = 1\n")
    assert (workspace.root / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert result.content == "Wrote 6 bytes to pkg/mod.py."
    assert result.details == {"path": "pkg/mod.py"}


def test_overwrites_existing_file(workspace: Workspace) -> None:
    target = workspace.root / "a.txt"
    target.write_text("old", encoding="utf-8")
    WriteTool(workspace).execute(path="a.txt", content="new")
    assert target.read_text(encoding="utf-8") == "new"


def test_escape_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolationError):
        WriteTool(workspace).execute(path="../out.txt", content="x")
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


class WriteArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    content: str = Field(description="Full file content to write.")


class WriteTool(Tool):
    name = "write"
    description = "Create or fully rewrite a file inside the workspace. Parent directories are created automatically and writes are atomic."
    args_model = WriteArgs

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str, content: str) -> ToolResult:
        rel = self._workspace.relative(path)
        self._workspace.write_text(path, content)
        size = len(content.encode("utf-8"))
        return ToolResult(
            content=f"Wrote {size} bytes to {rel}.",
            details={"path": rel},
            modified_files=[rel],
        )
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_write.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/write.py tests/test_write.py
git commit -m "feat: add atomic write tool"
```

---

### Task M4.6: edit

**Files:**
- Create: `mini_pi/tools/edit.py`
- Test: `tests/test_edit.py`

- [ ] **Step 1: 写失败测试 `tests/test_edit.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.edit import EditTool, EditSpec
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_unique_replacement(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    result = tool.execute(path="app.py", edits=[EditSpec(old_text="a - b", new_text="a + b")])
    assert (workspace.root / "app.py").read_text(encoding="utf-8").endswith("return a + b\n")
    assert result.content == "Replaced 1 block(s) in app.py."
    assert result.details is not None
    assert "-    return a - b" in result.details["diff"]
    assert "+    return a + b" in result.details["diff"]


def test_multiple_edits_use_original_offsets(workspace: Workspace) -> None:
    path = workspace.root / "multi.txt"
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    tool = EditTool(workspace)
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
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="not found"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="nope", new_text="x")])


def test_ambiguous_old_text_is_error(workspace: Workspace) -> None:
    path = workspace.root / "dup.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="matches 2 times"):
        tool.execute(path="dup.txt", edits=[EditSpec(old_text="same", new_text="other")])


def test_overlapping_edits_are_error(workspace: Workspace) -> None:
    path = workspace.root / "overlap.txt"
    path.write_text("abcdef\n", encoding="utf-8")
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="overlap"):
        tool.execute(
            path="overlap.txt",
            edits=[
                EditSpec(old_text="abcd", new_text="x"),
                EditSpec(old_text="cdef", new_text="y"),
            ],
        )


def test_empty_old_text_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="must not be empty"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="", new_text="x")])


def test_no_change_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
    with pytest.raises(ToolArgumentError, match="no change"):
        tool.execute(path="app.py", edits=[EditSpec(old_text="a - b", new_text="a - b")])


def test_missing_file_is_error(workspace: Workspace) -> None:
    tool = EditTool(workspace)
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


class EditArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    edits: list[EditSpec] = Field(min_length=1, description="Non-overlapping replacements applied to the original file.")


class EditTool(Tool):
    name = "edit"
    description = "Apply exact, unique text replacements to an existing file. All edits match the original content and must not overlap. Returns a unified diff."
    args_model = EditArgs

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
            modified_files=[rel],
        )
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_edit.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/edit.py tests/test_edit.py
git commit -m "feat: add exact-match edit tool"
```

---

### Task M4.7: search

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
from mini_pi.tools.search import SearchTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SearchTool:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("value = 42\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("add documentation here\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("add ignored\n", encoding="utf-8")
    return SearchTool(Workspace(tmp_path))


def test_literal_search(tool: SearchTool) -> None:
    result = tool.execute(pattern="add")
    lines = result.content.splitlines()
    assert "src/app.py:1:def add(a, b):" in lines
    assert "notes.md:1:add documentation here" in lines
    assert all(".venv" not in line for line in lines)
    assert result.details is not None
    assert result.details["engine"] == "python"


def test_regex_search(tool: SearchTool) -> None:
    result = tool.execute(pattern=r"return a \+ b", is_regex=True)
    assert "src/app.py:2:    return a + b" in result.content


def test_glob_filter(tool: SearchTool) -> None:
    result = tool.execute(pattern="add", glob="*.py")
    assert "notes.md" not in result.content


def test_no_matches(tool: SearchTool) -> None:
    result = tool.execute(pattern="does-not-exist")
    assert result.content == "No matches found"


def test_limit_adds_truncation_hint(tool: SearchTool) -> None:
    result = tool.execute(pattern="add", limit=1)
    assert "[Truncated at 1 matches" in result.content


def test_invalid_regex_is_argument_error(tool: SearchTool) -> None:
    with pytest.raises(ToolArgumentError, match="invalid regex"):
        tool.execute(pattern="[", is_regex=True)


def test_search_single_file(tool: SearchTool) -> None:
    result = tool.execute(pattern="add", path="src/app.py")
    assert "src/app.py:1" in result.content
    assert "notes.md" not in result.content


def test_binary_files_are_skipped(tool: SearchTool, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00add\x00")
    result = tool.execute(pattern="add")
    assert "blob.bin" not in result.content


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_rg_engine(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    tool = SearchTool(Workspace(tmp_path))
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


class SearchArgs(BaseModel):
    pattern: str = Field(description="Literal text or regular expression to search for.")
    path: str = Field(default=".", description="File or directory to search, relative to the workspace root.")
    glob: str | None = Field(default=None, description="Only search files matching this glob, e.g. '*.py'.")
    is_regex: bool = Field(default=False, description="Treat pattern as a regular expression.")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of matching lines to return.")


class SearchTool(Tool):
    name = "search"
    description = "Search text or regex across workspace files and return file:line:text matches. Skips .git/.venv/node_modules and binary files."
    args_model = SearchArgs
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
git commit -m "feat: add search tool with rg and python engines"
```

---

### Task M4.8: bash

**Files:**
- Create: `mini_pi/tools/bash.py`
- Test: `tests/test_bash.py`

- [ ] **Step 1: 写失败测试 `tests/test_bash.py`**

```python
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_pi.tools.bash import BashTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path) -> BashTool:
    return BashTool(Workspace(tmp_path))


def test_successful_command(tool: BashTool) -> None:
    result = tool.execute(command="echo hello")
    assert "exit_code: 0" in result.content
    assert "hello" in result.content
    assert result.details is not None
    assert result.details["exit_code"] == 0


def test_non_zero_exit_is_a_normal_result(tool: BashTool) -> None:
    result = tool.execute(command="exit 3")
    assert "exit_code: 3" in result.content


def test_stderr_is_preserved(tool: BashTool) -> None:
    result = tool.execute(command="echo oops 1>&2")
    assert "oops" in result.content
    assert "stderr:" in result.content


def test_cwd_is_workspace_root(tool: BashTool, tmp_path: Path) -> None:
    tool.execute(command="pwd > cwd.txt")
    recorded = Path((tmp_path / "cwd.txt").read_text(encoding="utf-8").strip()).resolve()
    assert recorded == tmp_path.resolve()


def test_timeout_is_reported(tool: BashTool) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    result = tool.execute(command=command, timeout=1)
    assert "timed out" in result.content
    assert result.details is not None
    assert result.details["timed_out"] is True


def test_invalid_timeout_is_rejected_by_schema(tool: BashTool) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BashTool.args_model.model_validate({"command": "echo hi", "timeout": 0})
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


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to run with cwd set to the workspace root.")
    timeout: int = Field(default=120, ge=1, le=600, description="Timeout in seconds.")


class BashTool(Tool):
    name = "bash"
    description = "Run a shell command in the workspace root. Returns exit code, stdout and stderr. Non-zero exit codes are reported, not raised."
    args_model = BashArgs
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
                "stdout_truncated": stdout_truncated or result.stdout_truncated,
                "stderr_truncated": stderr_truncated or result.stderr_truncated,
            },
        )
```

- [ ] **Step 4: 运行测试通过**

Run: `uv run pytest tests/test_bash.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add mini_pi/tools/bash.py tests/test_bash.py
git commit -m "feat: add bash tool with timeout and output truncation"
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
        result = run_process(argv, cwd=self._workspace.root, timeout_s=30, keep="head")
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
        "read",
        "write",
        "edit",
        "search",
        "bash",
        "git_diff",
    ]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_registry_defaults.py -v`
Expected: FAIL，`ImportError: cannot import name 'build_default_registry'`

- [ ] **Step 3: 写 `mini_pi/tools/__init__.py`**

```python
from __future__ import annotations

from mini_pi.tools.bash import BashTool
from mini_pi.tools.edit import EditTool
from mini_pi.tools.git import GitDiffTool
from mini_pi.tools.read import ReadTool
from mini_pi.tools.registry import ToolRegistry
from mini_pi.tools.search import SearchTool
from mini_pi.tools.write import WriteTool
from mini_pi.workspace.workspace import Workspace

__all__ = ["build_default_registry"]


def build_default_registry(workspace: Workspace) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadTool(workspace))
    registry.register(WriteTool(workspace))
    registry.register(EditTool(workspace))
    registry.register(SearchTool(workspace))
    registry.register(BashTool(workspace))
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
    call = ToolCall(id="c1", name="bash", arguments={"command": "pytest"})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    renderer.handle(
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="exit_code: 0"), is_error=False)
    )
    output = stream.getvalue()
    assert "bash" in output
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
        cwd=workspace.root,
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
        cwd=workspace.root,
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

观察输出应包含：`bash`（pytest 失败）→ `read` / `search` → `edit` → `bash`（pytest 通过）→ 最终总结。

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
    tool = EditTool(workspace)
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
def test_stdout_truncation_marks_details(tool: BashTool) -> None:
    command = f'"{sys.executable}" -c "print(\'x\' * 60000)"'
    result = tool.execute(command=command)
    assert result.details is not None
    assert result.details["stdout_truncated"] is True
    assert "[output truncated]" in result.content
```

`tests/test_search.py`：

```python
def test_binary_content_in_text_extension_is_skipped(tool: SearchTool, tmp_path: Path) -> None:
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
| read / write / edit / search / bash / git_diff | M4.4-M4.10 |
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

