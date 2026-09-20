# mini-pi

基于 Python 从零实现的轻量级 Coding Agent Harness，参考 [Pi](https://github.com/earendil-works/pi) / Claude Code 一类终端编程助手的架构思想。

项目目标不是“聊天机器人套 Shell”，而是一个真正具备自主代码操作能力的本地 AI 编程助手：

```text
用户任务
  ↓
CLI
  ↓
Agent Runtime
  ↓
LLM
  ↓
Tool Calling
  ↓
读取 / 搜索 / 修改代码 / 执行命令
  ↓
Observation
  ↓
LLM 继续决策
  ↓
直到完成 / 阻塞 / 达到 Step Limit
```

只支持两类模型 Provider：

- OpenAI / OpenAI-Compatible API
- DeepSeek

---

## 1. 项目定位

`mini-pi` 是一个 Python 编写的本地 Coding Agent。

核心目标：

- 自己实现 Agent Loop，不依赖 Agent 框架
- 支持 DeepSeek / OpenAI Compatible API
- 支持模型原生 Tool Calling
- 支持本地代码读取、搜索、修改和命令执行
- Workspace 安全边界：所有文件操作限制在工作区内，路径逃逸直接报错
- 任务过程中持续观察、修复、验证
- 逐步加入 Session、Context、LSP、MCP、Task、Memory、Multi-Agent

最终形态：

```text
Python Coding Agent Harness
├── Model Layer
├── Agent Runtime
├── Context System
├── Tool System
├── Workspace
├── Session
├── Task System
├── Memory
├── MCP
├── LSP
└── Multi-Agent
```

---

## 2. 从 pi 学到的设计取舍

阅读 pi 源码（`packages/agent`、`packages/coding-agent`、`packages/ai` 生产路径）后，mini-pi 明确以下取舍：

| 设计点 | pi 的做法 | mini-pi 的选择 |
| --- | --- | --- |
| 循环结构 | 双层循环：内层 tool batch + steering，外层 follow-up 队列 | 第一阶段单层循环，只处理 tool batch；队列、steering 留到 Session 阶段 |
| 事件驱动 | `AgentEvent` 事件流驱动 TUI/print/RPC，UI 是纯消费者 | 复刻：Loop 发 `AgentEvent`，CLI 只做渲染，不做决策 |
| LLM 流式 | provider 无关的 `AssistantMessageEvent` 事件流，错误编码进流 | 复刻：同步 SDK + `stream=True`，`ErrorEvent` 不裸抛给 Loop |
| Tool Call 拼装 | 按 `index` 聚合 SSE 增量，结束后解析 JSON | 复刻：`_AssistantAccumulator`，解析失败显式报错（不静默返回 `{}`） |
| 工具错误 | 所有异常转成 `isError` ToolResult 回传模型 | ToolError 转 `is_error` observation；非预期异常直接冒泡（Fail Fast） |
| 工具执行 | prepare 串行 + execute 并行 | 第一阶段全部串行，预留 `execution_mode` |
| 工具定义 | Schema → Definition → AgentTool → Renderer 四层 | 简化为 `pydantic Args + Tool` 单层，渲染由 CLI 事件层承担 |
| 输出截断 | 行数 + 字节双限，附可操作续读提示 | 复刻（read / run_command / search） |
| edit 语义 | 相对原文匹配、唯一匹配、多 edit 不重叠、支持 fuzzy | 第一阶段只做精确唯一匹配，fuzzy 后置 |
| Workspace 沙箱 | 无沙箱，绝对路径与 `../` 均放行 | 自建 `Workspace.resolve()`：`..`、绝对路径逃逸、symlink 逃逸全部 Fail Fast |
| 原子写 | 普通 `writeFile` | `tempfile` + `os.replace` 原子写 |
| System Prompt | prompt sections 存在 transcript 的 system message 中，可 diff | 第一阶段固定字符串；Session 阶段升级为 sections + diff |
| 持久化 | JSONL entry 树（`parentId` 链）+ compaction | 第二阶段复刻最小子集：`message + compaction` entry |

---

## 3. 架构

```text
┌────────────────────────────────────┐
│               CLI                  │
│    Typer 参数解析 / Rich 渲染       │
│    用户输入 / 流式输出 / 事件消费     │
└────────────────┬───────────────────┘
                 ↓  Agent.run(task)
┌────────────────────────────────────┐
│              Agent                 │
│  AgentState: messages / step_count │
│              modified_files        │
└────────────────┬───────────────────┘
                 ↓  run_loop()
┌────────────────────────────────────┐
│          LLM Client (Protocol)      │
│  OpenAIClient / DeepSeekClient      │
│  stream() -> StreamEvent            │
└────────────────┬───────────────────┘
                 ↓  tool_calls
┌────────────────────────────────────┐
│           ToolRegistry             │
│  schema 校验 / execute / 错误分类    │
└────────────────┬───────────────────┘
                 ↓  **kwargs
┌────────────────────────────────────┐
│               Tool                 │
│ read / write / edit / search       │
│ run_command / git_diff             │
└────────────────┬───────────────────┘
                 ↓  path
┌────────────────────────────────────┐
│             Workspace              │
│  resolve / read / write / cwd      │
│  路径逃逸 Fail Fast                 │
└────────────────────────────────────┘
```

依赖方向单向向下，禁止反向依赖：

```text
CLI → Agent → (LLM, ToolRegistry) → Tool → Workspace
```

---

## 4. 技术栈

```text
Python 3.12+
uv
Pydantic v2
openai SDK（同步 client）
Typer
Rich
pytest
```

后续阶段再引入：

```text
asyncio / 并行工具执行
JSONL Session
LSP / MCP
```

不使用：

```text
LangGraph / AutoGen / CrewAI / Dify / Coze
```

Agent Runtime 自己实现。

---

## 5. 目录结构

第一阶段实现范围：

```text
mini-pi/
├── pyproject.toml
├── README.md
├── AGENTS.md
│
├── docs/
│   └── plans/
│       └── phase1-core-runtime.md     # M1-M6 实施计划
│
├── mini_pi/
│   ├── errors.py                      # ToolError / LLMError / WorkspaceViolationError
│   │
│   ├── cli/
│   │   ├── app.py                     # Typer 入口：一次性 / 交互式
│   │   └── console.py                 # AgentEvent -> Rich 渲染
│   │
│   ├── agent/
│   │   ├── agent.py                   # Agent：状态 + run / reset
│   │   ├── loop.py                    # run_loop()：纯函数，可单测
│   │   ├── events.py                  # AgentEvent
│   │   ├── state.py                   # AgentState
│   │   └── prompt.py                  # system prompt 组装
│   │
│   ├── llm/
│   │   ├── types.py                   # Message / ToolCall / StreamEvent
│   │   ├── base.py                    # LLMClient Protocol + 重试
│   │   ├── openai_client.py
│   │   └── deepseek_client.py
│   │
│   ├── tools/
│   │   ├── base.py                    # Tool / ToolResult
│   │   ├── registry.py                # 注册、schema 校验、调度
│   │   ├── truncate.py                # 行/字节双限截断
│   │   ├── process.py                 # subprocess 执行与超时杀进程组
│   │   ├── read.py
│   │   ├── write.py
│   │   ├── edit.py
│   │   ├── search.py
│   │   ├── bash.py
│   │   └── git.py
│   │
│   └── workspace/
│       └── workspace.py               # 路径解析与安全边界
│
└── tests/
    ├── conftest.py                    # FakeLLMClient / workspace fixtures
    └── ...
```

后续阶段扩展目录（Session / Context / Task / Memory / MCP / LSP）在第一阶段不创建。

---

## 6. 核心模块

### 6.1 LLM Layer

```text
BaseLLMClient
├── OpenAIClient
└── DeepSeekClient
```

- 同步 client + `stream=True`，对外暴露统一 `StreamEvent` 事件流
- 事件：`start` / `text_delta` / `thinking_delta` / `tool_call_start` / `tool_call_delta` / `tool_call_end` / `done` / `error`
- tool call 增量按 `index` 聚合，结束时解析 JSON；非法 JSON 显式报错
- 429 / 5xx / 网络错误有限重试（指数退避），400 / 401 / 403 不重试
- 重试耗尽后编码为 `ErrorEvent`，不把异常裸抛给 Agent Loop
- DeepSeek 差异：`base_url`、`DEEPSEEK_API_KEY`、`reasoning_content` 回放，仅此三项

### 6.2 Agent Loop

```text
LLM → Tool Call → Tool → Observation → LLM → ...
```

- `run_loop()` 是纯函数：输入 `AgentState + LLMClient + ToolRegistry`，输出最终 `AssistantMessage`
- 每轮通过 `on_event` 回调发出 `AgentEvent`，CLI 是纯消费者
- 终止条件：无 tool call / LLM error / 达到 max_steps / `length` 截断后的修复轮结束
- `stop_reason == "length"` 时**不执行**任何 tool call，全部转 error observation 让模型重发
- 不做 `read → edit → test` 固定流程，下一步由模型根据 Observation 自主决定

### 6.3 Tool System

第一阶段工具：

```text
read_file    读文件（offset/limit、二进制识别、截断续读）
write_file   原子写（自动建父目录）
edit_file    精确唯一匹配替换（多 edit、不重叠、输出 diff）
search_code  搜索代码（优先 rg，无 rg 用 Python 扫描）
run_command  执行命令（cwd=workspace、timeout、stdout/stderr 分离、exit code）
git_diff     查看改动（支持 staged）
```

约定：

- 参数用 pydantic 模型声明，registry 统一校验
- 可预期失败抛 `ToolError`，Loop 转成 `is_error` observation 回传模型
- 非预期异常直接冒泡（Fail Fast）
- 文件操作必须经过 `Workspace`，禁止工具自行 `open()`

### 6.4 Workspace

```python
workspace.resolve("src/app.py")     # -> Path，越界抛 WorkspaceViolationError
workspace.read_text("src/app.py")
workspace.write_text("src/app.py", content)  # 原子写
workspace.root
```

安全规则：

```text
../ 逃逸            → 报错
workspace 外绝对路径 → 报错
symlink 指向外部     → 报错
不自动修正路径，不静默 fallback
```

### 6.5 CLI

```bash
mini-pi "修复某个 bug"          # 一次性执行
mini-pi                         # 交互式 REPL（/reset /exit）
mini-pi --provider deepseek --model deepseek-chat
```

- Provider / Key：`--provider` + `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`，缺失时直接报错退出
- 流式打印模型正文，工具调用与结果以简洁格式展示
- `--max-steps` 控制单次任务的最大循环步数（默认 50）

---

## 7. 快速开始

```bash
uv sync
export OPENAI_API_KEY=sk-...        # 或 DEEPSEEK_API_KEY

uv run mini-pi "介绍一下这个仓库"
uv run mini-pi --provider deepseek "运行 pytest 并修复失败用例"
```

测试：

```bash
uv run pytest                       # 默认排除真实 API 测试
uv run pytest -m integration        # 需要 API Key
```

---

## 8. 开发路线图

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M1 | LLM 调通：消息模型、OpenAI/DeepSeek 流式 client、重试 | 已完成 |
| M2 | Tool Calling：Tool/Registry、事件模型、run_loop、FakeLLM 测试 | 未开始 |
| M3 | Agent Loop 完善：max_steps、错误处理、Agent 封装、system prompt | 未开始 |
| M4 | 文件 / Shell Tool：Workspace、read/write/edit/search/run_command/git_diff | 未开始 |
| M5 | 真实代码修改闭环：CLI、样例项目、真实 API 验收 | 未开始 |
| M6 | pytest 完善：边界用例、超时、路径逃逸、完整回归 | 未开始 |
| M7 | Session / Context：JSONL entry 树、AGENTS.md 加载、compaction | 未开始 |
| M8 | LSP / MCP | 未开始 |
| M9 | Task / Memory | 未开始 |
| M10 | Multi-Agent | 未开始 |

M1-M6 的详细任务拆解见 [`docs/plans/phase1-core-runtime.md`](docs/plans/phase1-core-runtime.md)。

不要跨阶段同时开太多功能。每完成一个里程碑：

1. 运行 `uv run pytest`
2. 更新本表状态
3. 勾选计划文档中对应任务

---

## 9. Phase 1 完成标准

以下流程稳定跑通：

```text
用户提出代码任务
  ↓
Agent 搜索代码
  ↓
读取相关文件
  ↓
定位问题
  ↓
修改代码
  ↓
执行测试 / 构建
  ↓
观察失败
  ↓
继续修复
  ↓
验证成功
  ↓
输出最终总结
```

验收方式：在 `tests/fixtures/sample_project`（内置失败用例）上执行

```bash
uv run mini-pi --cwd tests/fixtures/sample_project \
  "运行 pytest，定位失败原因并修复，修复后再次运行 pytest 验证"
```

要求：Agent 自主完成定位 → 修改 → 验证，且修改仅发生在 workspace 内。

如果只是 `User → LLM → Answer`，或只是固定 Workflow，都不是本项目目标。

目标是：

> 由模型在 Agent Loop 中根据当前上下文和 Tool Result 自主决定下一步行动。

---

## 10. 测试

- 核心链路（Loop / Registry / Workspace / Tool / 截断 / 超时）全部用 pytest 覆盖
- Agent 测试使用 `FakeLLMClient`（脚本化事件流），不调用真实 API
- 真实 API 测试标记 `@pytest.mark.integration`，默认排除
- 文件工具测试使用 `tmp_path`，不触碰真实项目文件

详见 [`docs/plans/phase1-core-runtime.md`](docs/plans/phase1-core-runtime.md) 的测试步骤。

---

## 11. 开发原则

```text
Keep Core Small
Fail Fast
Explicit > Magic
Tool First
Agent Decides
Workspace Isolated
Event Driven
Test Core Runtime
```

---

## 12. 参考

- Pi 源码：`/Users/yzh666/workspace/pi`（本地）
- Pi 源码学习指南：`/Users/yzh666/workspace/pi/AGENT-LEARNING-GUIDE.md`
- 项目约束：[`AGENTS.md`](AGENTS.md)
- Phase 1 计划：[`docs/plans/phase1-core-runtime.md`](docs/plans/phase1-core-runtime.md)
