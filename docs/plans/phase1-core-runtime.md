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

### Task M4.4: read（已完成）

提交：本次提交

交付物：

- `mini_pi/workspace/workspace.py`：新增 `read_bytes`，保证文件读取全部经过 Workspace
- `mini_pi/tools/read.py`：`ReadTool`（name=`read`）— 参数 `{path, offset, limit}`；NUL 二进制嗅探、UTF-8 显式校验、offset 越界报错、2000 行 / 50KB 截断并附 `[Showing lines X-Y of N. Use offset=Z to continue.]`；只读工具不声明 modified_files

验收：`tests/test_read.py` 8 passed；全量 92 passed, 2 deselected。

---

### Task M4.5: write（已完成）

提交：本次提交

交付物：`mini_pi/tools/write.py` — `WriteTool`（name=`write`）— 参数 `{path, content}`；整文件写、自动建父目录（复用 Workspace 原子写）、按 UTF-8 字节数回报、显式声明 `modified_files=[rel]`；路径逃逸由 Workspace 拦截。

验收：`tests/test_write.py` 4 passed；全量 96 passed, 2 deselected。

---

### Task M4.6: edit（已完成）

提交：本次提交

交付物：`mini_pi/tools/edit.py` — `EditTool`（name=`edit`）+ `EditSpec{old_text, new_text}`；参数 `{path, edits[]}`；所有 edit 相对原文定位、唯一匹配、区间不重叠、从后往前应用；空 old_text / 未命中 / 多匹配 / 重叠 / 无变化全部 `ToolArgumentError`；成功返回 unified diff 与 `modified_files=[rel]`。

验收：`tests/test_edit.py` 8 passed；全量 104 passed, 2 deselected。

---

### Task M4.7: search（已完成）

提交：本次提交

交付物：`mini_pi/tools/search.py` — `SearchTool`（name=`search`）— 参数 `{pattern, path, glob, is_regex, limit<=1000}`；`_find_rg` 优先取当前环境 bin 下的 rg（`ripgrep-bin` 依赖自带，不依赖 PATH），无 rg 时用内置 Python 扫描（确定性排序、跳过 `.git/.venv/node_modules` 等目录、跳过 >1MB 与含 NUL 的二进制文件、单行截 500 字符）；非法正则 → `ToolArgumentError`；结果格式 `file:line:text` 并附截断提示；只读工具。

验收：`tests/test_search.py` 10 passed（含 rg 引擎与环境内 rg 优先级用例）；全量 120 passed, 2 deselected。

---

### Task M4.8: bash（已完成）

提交：本次提交

交付物：`mini_pi/tools/bash.py` — `BashTool`（name=`bash`）— 参数 `{command, timeout∈[1,600]，默认 120}`；cwd=workspace.root，复用进程层（超时杀进程组、有界捕获）；stdout / stderr 各自 2000 行 / 50KB tail 截断并附 `[output truncated]`；非 0 退出码正常返回并如实展示；超时在 header 标注；无法可靠推断改动的文件，故不声明 modified_files。

验收：`tests/test_bash.py` 6 passed；全量 119 passed, 2 deselected。

---

### Task M4.9: git_diff（已完成）

提交：本次提交

交付物：`mini_pi/tools/git.py` — `GitDiffTool`（name=`git_diff`）— 参数 `{path?, staged=false}`；执行 `git diff --no-color [--cached] [-- path]`，路径先经 Workspace 校验；进程层 `keep="head"`，再按 2000 行 / 50KB 截断并附提示；无改动返回 `No changes.`；非 git 仓库等非 0 退出转 `ToolError`；只读工具。

验收：`tests/test_git.py` 5 passed；全量 125 passed, 2 deselected。

---

### Task M4.10: 默认工具注册（已完成）

提交：本次提交

交付物：`mini_pi/tools/__init__.py` — `build_default_registry(workspace)` 按固定顺序装配 read / write / edit / search / bash / git_diff；顺序即 system prompt 与工具 schema 的展示顺序。

验收：`tests/test_registry_defaults.py` 1 passed；全量 126 passed, 2 deselected。M4 里程碑全部完成。

---

## M5 真实代码修改闭环

### Task M5.1: CLI 渲染器（已完成）

提交：本次提交

交付物：`mini_pi/cli/console.py` — `ConsoleRenderer(console=None)` 消费 `AgentEvent`：正文 / 思考增量逐段打印（thinking 用暗色）、`message_end` 收尾换行、工具调用 `→ name {args}` 与结果首行（错误红 / 正常绿）、`agent_end` 的 step_limit / error 提示；只做渲染，不参与决策。

验收：`tests/test_console.py` 4 passed；全量 130 passed, 2 deselected。

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

