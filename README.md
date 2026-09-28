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
- Session 与 Context 已完成（M7）：JSONL 会话、项目 `AGENTS.md`、恢复、任务预算与 compaction
- 逐步加入 LSP、MCP、Task、Memory、Multi-Agent

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
| 循环结构 | 双层循环：内层 tool batch + steering，外层 follow-up 队列 | 当前是单层 tool batch 循环；`prepare_next_turn` 接收单次运行的 `RunContext` 并可替换投影。steering / follow-up 尚未实现，是否进入 M8.0 先由真实长任务基线验证 |
| 事件驱动 | `AgentEvent` 事件流驱动 TUI/print/RPC，UI 是纯消费者 | Loop 发 `AgentEvent`，CLI 只做渲染；M7.C7 的命令标题和失败提示不进入 ToolMessage，也不改变 Agent 决策 |
| 思考与终端展示 | thinking 事件可供 UI 展示，Provider 保留必要回放字段 | M7.C 默认只显示思考状态图标；CLI 元数据不进入消息历史，DeepSeek 的 `reasoning_content` 回放保持协议兼容 |
| 用量与上下文 | 模型请求返回 usage，compaction 缩短后续模型投影 | M7.C6 已区分单次任务累计 Provider 用量和当前上下文估算；M7.C8 提供默认关闭、显式启用的请求边界任务预算，窗口阈值只负责压缩安全 |
| 工具结果生命周期 | Session 保留完整消息，compaction 生成摘要投影 | 当前工具轮使用真实且有界的 observation；JSONL 原始消息不改写，后续投影只在安全切点压缩，展示摘要不替代 ToolMessage |
| LLM 流式 | provider 无关的 `AssistantMessageEvent` 事件流，错误编码进流 | 复刻：同步 SDK + `stream=True`，`ErrorEvent` 不裸抛给 Loop |
| Tool Call 拼装 | 按 `index` 聚合 SSE 增量，结束后解析 JSON | 复刻：`_AssistantAccumulator`，解析失败显式报错（不静默返回 `{}`） |
| 工具错误 | 所有异常转成 `isError` ToolResult 回传模型 | ToolError 转 `is_error` observation；非预期异常直接冒泡（Fail Fast） |
| 工具执行 | prepare 串行 + execute 并行 | 全部串行，未实现并行执行 |
| 工具定义 | Schema → Definition → AgentTool → Renderer 四层 | 简化为 `pydantic Args + Tool` 单层，渲染由 CLI 事件层承担 |
| 输出截断 | 行数 + 字节双限，附可操作续读提示 | 复刻（read / bash / search） |
| edit 语义 | 相对原文匹配、唯一匹配、多 edit 不重叠、支持 fuzzy | 只做精确唯一匹配，fuzzy 后置 |
| Workspace 沙箱 | 无沙箱，绝对路径与 `../` 均放行 | 自建 `Workspace.resolve()`：`..`、绝对路径逃逸、symlink 逃逸全部 Fail Fast |
| Shell 边界 | `bash` 是无沙箱本地 shell | 同样是无沙箱本地 shell：`bash` 只约束 `cwd`，可读写 workspace 之外、可联网；文件工具的边界不适用于它。接入不可信外部工具前重新评估执行权限 |
| 上下文估算 | usage 锚点 + 其后消息的字符估算 | M7.8.1 按当前 wire 消息与工具 schema 预测下一请求；M7.8.2 用字符类权重和一次性工具模式开销校准 DeepSeek 输入；M7.8.3 由运行时持有解析后的窗口策略，压缩决策与 CLI 展示共用。旧 `usage.total_tokens` 仅留在摘要切点与成本模型的消息区域估算中，历史 input usage 单独展示 |
| 原子写 | 普通 `writeFile` | `tempfile` + `os.replace` 原子写 |
| System Prompt | prompt sections 存在 transcript 的 system message 中，可 diff | M7.2 已实现快照/patch；每次 run 前读取祖先链 `AGENTS.md` 并只记录变化 |
| 持久化 | JSONL entry 树（`parentId` 链）+ compaction | M7.3-M7.5 已完成 CLI 新建/恢复/`/new`/同链 `/model` 切换、compaction 投影与手动 `/compact`；M7.6 接入窗口与成本触发；M7.D1 的 `/sessions` 与 `--continue` 严格加载全部候选；M7.7 已通过离线回归、真实 DeepSeek 与人工 CLI 验收 |
| 终端输入与取消 | TUI 提供多行编辑、历史与中断 | M7.D2 用可选 `prompt_toolkit` 提供历史/多行/`/` 补全/Ctrl+L，非 tty 或库缺失回退内建 `input()`（交互终端缺库会打印降级原因）；唯一匹配时 Enter 先补全再提交；Ctrl+C 在 Loop 的工具/流式边界转成 `cancelled`，已提交消息与文件改动保留，连续两次才退出 |

---

## 3. 架构

```text
CLI（Typer 参数解析 / Rich 事件渲染 / 可选 prompt_toolkit 输入）
 │
 │  AgentSession.run(task)        --no-session 时直接 Agent.run(task)
 ▼
AgentSession
 ├── JsonlSession：完整、追加式事实（message / compaction entry，durable-first 提交）
 ├── Context：AGENTS.md sections / 活动链投影 / token 估算 / 压缩事务
 │
 │  Agent.run(task)
 ▼
Agent（AgentState：messages / step_count / modified_files）
 │
 │  run_loop()
 ▼
LLM Client（Protocol：OpenAIClient / DeepSeekClient，stream() -> StreamEvent）
 │
 │  tool_calls
 ▼
ToolRegistry（schema 校验 / 调度 / 错误分类）
 │
 │  **kwargs
 ▼
Tool（read / write / edit / search / bash / git_diff / git_status）
 │
 │  path（只经 Workspace）
 ▼
Workspace（resolve / read / write / cwd，路径逃逸 Fail Fast）
```

依赖方向单向向下，禁止反向依赖：

```text
CLI → AgentSession → Agent → (LLM, ToolRegistry) → Tool → Workspace
            └── Context / JsonlSession 只由 Session 层驱动
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
prompt_toolkit（CLI 行编辑；非 tty 或缺失时回退内建 input）
ripgrep-bin（search 工具内置 rg）
pytest
ruff（静态检查；CI 使用 Python 3.12）
```

支持平台：macOS / Linux（依赖 POSIX 进程组与文件权限语义）；Windows 未支持。

后续阶段再引入：

```text
LSP / MCP
asyncio / 并行工具执行
```

不使用：

```text
LangGraph / AutoGen / CrewAI / Dify / Coze
```

Agent Runtime 自己实现。

---

## 5. 目录结构

当前实现范围（M1-M7.8 已完成，M8 未开始）：

```text
mini-pi/
├── pyproject.toml
├── README.md
├── AGENTS.md
│
├── docs/
│   ├── design/
│   │   └── pi-production-architecture.md  # Pi 生产架构参考
│   ├── plans/
│   │   ├── phase1-core-runtime.md         # M1-M6 实施计划
│   │   ├── phase2-session-context.md      # M7 设计与实施计划
│   │   └── phase3-runtime-hardening.md    # M7.8-M10 路线与实施计划
│   └── benchmarks/                    # M7 用量 / 成本 / 验收记录与离线 tty 驱动器
│
├── mini_pi/
│   ├── errors.py                      # Tool / LLM / Workspace / Session 错误
│   ├── auth.py                        # API Key 与上次连接的安全持久化
│   │
│   ├── cli/
│   │   ├── app.py                     # Typer 入口：一次性 / 交互式
│   │   ├── input.py                   # prompt_toolkit 行编辑与单行回退
│   │   ├── banner.py                  # 启动图案与紧凑元数据
│   │   ├── console.py                 # AgentEvent -> Rich 渲染
│   │   ├── sessions.py                # 严格 Session 列表展示
│   │   └── status.py                  # 状态、上下文与工具展示
│   │
│   ├── assets/
│   │   ├── banner.txt                 # 启动图案资源
│   │   └── thinking.txt               # 思考状态图案资源
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
│   │   ├── process.py                 # subprocess 执行、超时/中断杀进程组
│   │   ├── read.py
│   │   ├── write.py
│   │   ├── edit.py
│   │   ├── search.py
│   │   ├── bash.py
│   │   ├── git.py
│   │   └── git_status.py
│   │
│   ├── session/
│   │   ├── models.py                 # Header / MessageEntry / CompactionEntry
│   │   ├── jsonl.py                  # create / load / append / leaf / 路径发现
│   │   ├── runtime.py                # AgentSession 创建 / 恢复 / 新会话 / 模型切换
│   │   └── usage.py                  # 从活动链重建最近任务用量
│   │
│   ├── context/
│   │   ├── project.py                # 项目 AGENTS.md 发现与读取
│   │   ├── sections.py               # section diff / patch / replay / render
│   │   ├── projection.py             # Session 活动路径的消息投影
│   │   ├── tokens.py                 # token 估算
│   │   ├── compaction.py             # 安全切点与压缩输入 plan
│   │   ├── serializer.py             # 摘要输入的确定性序列化
│   │   ├── summarizer.py             # 固定摘要协议与单次摘要调用
│   │   ├── policy.py                 # context window 与阈值策略
│   │   ├── cost.py                   # 旧工具结果提前压缩的成本模型
│   │   └── stats.py                  # 当前投影分类估算
│   │
│   └── workspace/
│       └── workspace.py               # 路径解析与安全边界
│
└── tests/
    ├── conftest.py                    # FakeLLMClient / workspace fixtures
    ├── integration/                   # 离线端到端（真实工具 + 真实 JSONL + FakeLLM）
    ├── test_integration_*.py          # 真实 API 用例（integration marker，默认排除）
    └── ...
```

`tests/integration/` 是离线端到端目录（CLI → Session → Loop → Tool → Context 全链路，FakeLLM 驱动），与 `@pytest.mark.integration`（真实 API）无关；后者默认被 `addopts` 排除。

后续 Task / Memory / MCP / LSP 目录在对应里程碑前不创建。

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
- M7.6a 起可选 `prepare_next_turn` 钩子在完整工具批次提交后、下一次请求前调用，供 Session 层替换压缩投影；截断轮不触发。M7.6c 起 `AgentSession` 把该钩子接到与 prompt 前检查同一套策略判定和压缩事务上；M7.6d 起钩子的可预期失败（`MiniPiError`）在 Loop 内转成 `agent_end(error)`，不再向模型发出越界的下一次请求，其他异常仍直接冒泡。M7.8.4 起钩子接收每次 `run_loop` 新建的 `RunContext`，返回投影由 Loop 安装，返回 `None` 则保持原投影；成本压缩尝试标志按 run 隔离，任务预算仍由 Loop 计数
- 终止条件：无 tool call（`completed`）/ LLM error / 达到 `max_steps`（`step_limit`）/ 显式任务预算阻止下一请求（`budget_limit`）/ 用户中断（`cancelled`）。只有 `completed` 算成功，一次性模式按原因返回不同退出码（见 6.5）
- `stop_reason == "length"` 时**不执行**任何 tool call，全部转 error observation 让模型重发
- M7.D2 起用户中断（KeyboardInterrupt）是可预期终止：未提交的流式 assistant 不补写，工具轮为被中断及未执行的调用补 cancelled observation 保持 call/result 配对，以 `agent_end(reason="cancelled")` 结束，已提交消息与文件改动不回滚
- 不做 `read → edit → test` 固定流程，下一步由模型根据 Observation 自主决定

### 6.3 Tool System

当前工具：

```text
read       读文件（offset/limit、二进制识别、截断续读）
write      原子写（自动建父目录）
edit       精确唯一匹配替换（多 edit、不重叠、输出 diff）
search     搜索代码（依赖自带 rg，无可用 rg 时用 Python 扫描）
bash       执行命令（cwd=workspace、timeout、stdout/stderr 分离、exit code）
git_diff   查看内容差异（支持 staged）
git_status 观察工作区状态（分支、已暂存/未暂存/未跟踪/冲突，只读）
```

约定：

- 参数用 pydantic 模型声明，registry 统一校验
- 可预期失败抛 `ToolError`，Loop 转成 `is_error` observation 回传模型
- 非预期异常直接冒泡（Fail Fast）
- 文件操作必须经过 `Workspace`，禁止工具自行 `open()`
- `modified_files` 只列工具确知改动的路径：只读工具（`read` / `search` / `git_diff` / `git_status`）与 `bash` 都留空，shell 改了哪些文件无法可靠推断。需要改动事实时用 `git_status` 观察状态、`git_diff` 查看内容，不靠模型总结补
- `git_status` 只观察工作区：输出按暂存 / 未暂存 / 未跟踪 / 冲突分组，路径相对 workspace；查询限定在 workspace 内，非 git workspace 明确报错，不返回空的“干净”结果

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
mini-pi                         # 交互式 REPL，默认创建 JSONL Session
mini-pi --cwd <dir>             # 指定 workspace（默认当前目录）
mini-pi --provider deepseek --model deepseek-flash
mini-pi --max-steps 80          # 单次任务最大循环步数（默认 50）
mini-pi --no-session            # 保留纯内存模式（/model /reset /exit）
mini-pi --resume <session.jsonl> # 恢复指定会话
mini-pi --continue              # 继续当前 workspace 最近的会话
mini-pi --no-banner             # 交互启动时不打印 ASCII Banner
mini-pi --verbose               # 显示工具参数与有界日志
mini-pi --max-run-input-tokens 100000  # 显式启用每次任务的累计输入预算
# REPL 支持 /status /context /tools /sessions /help；持久化模式另有 /new、/compact，纯内存模式支持 /reset
```

- 新建会话时的模型选择顺序：显式 `--provider/--model` > 上次连接（其 provider 有可用 Key 时）> 第一个已配 Key 的 provider > 内置默认值；恢复时默认使用会话活动路径最后的 provider/model，显式参数仅覆盖后续新消息
- API Key 解析顺序：环境变量（`OPENAI_API_KEY` / `DEEPSEEK_API_KEY`）> `~/.mini-pi/auth.json`（目录 0700、文件 0600）
- 启动时若已保存 Key 直接复用；当前 provider 缺 Key 时优先切到已配 Key 的 provider，交互式（tty）缺 Key 则直接隐藏输入并单次验证后原子保存，无需先记住命令
- `/model [provider] [model]` 切换 provider/model：已有 Key 直接复用、不重复落盘，仅缺 Key 时输入并验证；`/connect` 为兼容别名
- 一次性模式缺少 Key 时明确报错，并提示环境变量与 `/model`（`/connect`）两种方式
- 默认在 `~/.mini-pi/sessions/` 下按 workspace 保存 JSONL；交互启动页只显示短会话 id，完整 cwd 与当前 Session 路径由 `/status full` 展示；一次性任务结束仍显示可恢复文件路径；创建失败不会静默回退到内存模式
- `--resume <path>` 严格加载指定会话；`--continue` 严格校验当前 workspace 的所有候选，按最后 entry 的活动时间选最新（空会话用 header 时间）。候选损坏、cwd 不匹配或最新时间并列会报错，不静默退回旧会话；两者不可并用，也不可与 `--no-session` 并用
- `--no-session` 不创建持久化文件，保留原有 `/reset` 与 `/model` 行为；持久化模式用 `/new` 开启独立会话，`/model` 在当前链切换模型且仅让后续 entry 使用新配置；`/reset` 在持久化模式下提示改用 `/new`
- 交互启动显示 ASCII Banner 与标语（`mini_pi/assets/banner.txt` 原样输出），随后只列版本、模型、项目名、短会话 id 和 `/help`；终端宽度不足或非 tty 时 Banner 降为单行、启动元数据按字段分行；`--no-banner` 可关闭
- `/help` 列出可用命令；未知 `/命令` 只提示且不会作为任务发给模型
- `/sessions` 严格加载当前 workspace 的全部候选，按活动时间显示短 id、活动模型、摘要状态和当前标记；任何候选损坏都会整体报错，不跳过后展示不完整列表。该命令只读本地 JSONL，不调用模型；恢复仍使用 `--continue` 或 `--resume <session.jsonl>`
- `/status` 分开显示当前投影估算与最近任务的请求数、累计 Provider 用量、工具数和耗时；Session 恢复后从完整活动链重建统计，`/status full` 才显示完整路径。`/context` 分解当前投影，单列最近请求输入与任务累计用量，并显示当前模型的窗口压缩阈值与成本触发状态；`/tools` 列出工具
- `--context-window INT` 可显式覆盖当前模型的窗口，`--reserve-tokens INT` 调整预留量（默认 8192，必须小于已配置窗口）。持久化会话的 prompt 前检查、工具轮检查、`/status` 与 `/context` 使用同一策略；`/model` 切换时显式窗口继续优先，否则重查新模型内置窗口，`/new` 继承本次配置。`--resume` 用本次 CLI 参数重新解析，不把窗口写入 JSONL。未知模型未配置窗口时自动压缩仍关闭；`--no-session` 只展示解析后的窗口，不执行需要 JSONL 的自动压缩
- `/compact [instructions]` 手动压缩持久化会话：只在安全切点前生成摘要检查点，摘要请求不带工具，`instructions` 仅进入本次请求；输出摘要消息数、保留 entry 数、切点边界、压缩前后当前上下文估算与摘要调用的实测 usage。原始 message entry 一条不删，失败时不改 JSONL 与内存投影；`--no-session` 明确拒绝，不隐式建 JSONL
- 同一套判定也会自动运行：M7.6b 起每次 `run()` 提交新 user 消息前按窗口策略（当前投影估算 > `context_window - reserve`）检查，需要时先压缩再追加这一条消息；M7.6c 起每个完整工具批次提交后、下一次请求前也用同一判定检查，压缩成功后同一次任务继续，已执行的工具不重放。窗口未知的模型不启用自动压缩，未超阈值不产生任何写入。M7.6d 起自动压缩失败（无安全切点、摘要或写盘失败、压缩后仍超阈值）以 agent error 结束这次任务：不追加假 assistant、不改投影、不再发出越界的下一次请求；已提交的消息与工具结果保留，一次性 CLI 调用返回非零码。M7.6f 起工具轮之间还有第二个触发：窗口内但旧工具结果占被摘要区域一半以上、且按 3 次后续请求算得摘要成本小于预计节省时提前压缩（旧输出按 2000 字符截断进入摘要请求，因此摘要成本与日志长度无关）；每次任务最多尝试一次，摘要阶段失败只放弃这次优化、不终止任务，写盘或重建失败仍以 agent error 终止；仅离线估算验证过收益（[记录](docs/benchmarks/m7-6f-cost-aware-compaction.md)），没有真实计费结论
- 流式打印模型正文，默认工具事件根据已知命令形态显示操作标题、真实退出码、超时/截断与简短 stderr；未知或含凭据的 shell 命令采用保守标题，不推断任务成败。`--verbose` 展示参数及 Tool 层已截断日志并脱敏已知凭据。任务结束只打印一行请求、用量覆盖率、工具数和耗时；缺失 usage 明确标为不可用或部分实测。耗时在恢复后是 JSONL 消息时间的近似跨度
- `--max-steps` 控制单次任务的最大循环步数（默认 50）。用尽步数仍未给出最终回答时以 `step_limit` 结束：终端明确提示上限值与「任务未完成」，不会把最后一条 assistant 正文当成完成态；交互模式下只结束本次任务，下一条提问重新计数，也可用 `--max-steps` 提高上限
- 一次性模式的退出码与终止原因一一对应，脚本可据此判断任务是否真的完成：`0` completed、`1` error、`2` budget_limit、`3` step_limit、`130` cancelled（128+SIGINT）。只有 `completed` 返回 0；其余情况不回滚已提交的消息、工具结果与文件改动，持久化会话保留，可用 `--resume` 继续；拿不到 `agent_end` 时按失败（1）处理，不因缺少终止事件报成功
- 交互输入默认使用 `prompt_toolkit`：输入 `/` 时命令菜单自动出现在输入行下方，Tab（或 `→`）采纳高亮项；只剩**唯一匹配**时 Enter 会先补全、再按一次 Enter 才提交，其他情况 Enter 直接提交原文；Ctrl+J 或 Alt+Enter 换行、Ctrl+L 清屏。输入历史保存在 `~/.mini-pi/history`（目录 0700、文件 0600，不写入项目目录，Key 输入走独立的隐藏提示因此不进入历史）。stdin/stdout 不是 tty 时静默回退内建 `input()` 的单行 REPL（按行读取、每行一次提交）；交互终端但输入库缺失时同样回退，并在 REPL 顶部打印一行降级原因，避免静默失去补全与历史
- Ctrl+C 取消当前任务：任务中的中断由 Loop 在流式与工具边界转成 `cancelled`（不是 `completed`），已提交的消息、工具结果与文件改动保留；工具轮里被中断和未执行的调用会补 cancelled observation 保持 call/result 配对，`bash` 的独立进程组会被整组杀掉。空闲时第一次 Ctrl+C 只提示、连续第二次退出；刚取消任务后的下一次空闲 Ctrl+C 直接退出。一次性模式被中断返回退出码 130。tty 人工记录见 [`docs/benchmarks/m7-d2-tty-input-cancel.md`](docs/benchmarks/m7-d2-tty-input-cancel.md)
- `--max-run-input-tokens` 显式启用每次 `run()` 的累计输入预算，默认关闭。Loop 在下一次模型请求前用 Provider 已报告 input 加当前投影估算检查；接近上限时只提示模型收敛一次，预计超限则以 `budget_limit` 停止。它是请求边界控制，单次请求仍可能超过预测；已提交的消息、工具结果和文件改动保留，交互模式下一条任务获得新预算

---

## 7. 快速开始

```bash
uv sync
export OPENAI_API_KEY=sk-...        # 或 DEEPSEEK_API_KEY
# 也可以先 `uv run mini-pi`，按提示输入 Key 或用 /model 切换 provider/model

uv run mini-pi "介绍一下这个仓库"
uv run mini-pi --provider deepseek "运行 pytest 并修复失败用例"
```

> 用 `uv tool install` 安装过 `mini-pi` 的话，新增依赖后要重新同步，否则独立环境会缺少 `prompt_toolkit`（CLI 会打印降级原因、补全与历史不可用）：`uv tool upgrade mini-pi`。

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
| M2 | Tool Calling：Tool/Registry、事件模型、run_loop、FakeLLM 测试 | 已完成 |
| M3 | Agent Loop 完善：max_steps、错误处理、Agent 封装、system prompt | 已完成 |
| M4 | 文件 / Shell Tool：Workspace、read/write/edit/search/bash/git_diff | 已完成 |
| M5 | 真实代码修改闭环：CLI、样例项目、真实 API 验收 | 已完成 |
| M6 | pytest 完善：边界用例、超时、路径逃逸、完整回归 | 已完成 |
| M7 | Session / Context 与 CLI：JSONL、AGENTS.md、resume、任务成本控制、compaction、可观测性 | 已完成（M7.1-M7.6、M7.C1-C8、M7.D1-D2、M7.7 验收；验收记录见 `docs/benchmarks/`） |
| M7.8 | Runtime Hardening：统一请求口径、CJK 安全估算、窗口配置化、RunContext 与 turn 边界、CI | 已完成（M7.8.0–M7.8.6；[最终验收](docs/benchmarks/m7-8-final-acceptance.md)：566 passed、5 deselected，DeepSeek 四组实测；[远端 CI](https://github.com/w0nderful-yzh/mini-pi/actions/runs/36373856072) 已通过） |
| M7.9 | 完成语义、工作区状态、真实任务基线与 CLI 视觉整理 | 进行中（M7.9.1–M7.9.2 已完成） |
| M8 | 长任务交互、只读 LSP、按需 MCP、活动工具集与恢复 | 未开始；每项以真实任务收益或明确服务需求为准 |
| M9 | Task / Memory | 未开始 |
| M10 | Multi-Agent | 未开始 |

M1-M6 的详细任务拆解见 [`docs/plans/phase1-core-runtime.md`](docs/plans/phase1-core-runtime.md)。
M7 的架构设计、子里程碑与验收见 [`docs/plans/phase2-session-context.md`](docs/plans/phase2-session-context.md)。
M7.8 的交付记录及 M7.9-M10 的任务拆解、准入与验收见 [`docs/plans/phase3-runtime-hardening.md`](docs/plans/phase3-runtime-hardening.md)。

MVP 后的实施顺序保持为：先让会话可恢复、上下文可控，再扩展外部能力。

```text
M7 Session / Context（已完成）
  ↓
M7.8 Runtime Hardening（已完成）
  ↓
M7.9 完成语义、真实任务基线与 CLI 视觉整理
  ↓
M8 长任务交互 / LSP / 按需 MCP
  ↓
M9 Task / Memory
  ↓
M10 Multi-Agent
```

不要跨阶段同时开太多功能。每完成一个里程碑：

1. 运行 `uv run pytest`
2. 更新本表状态
3. 更新计划中的交付、验收与提交号

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

要求：Agent 自主完成定位 → 修改 → 验证；文件工具的修改受 workspace 边界约束，`bash` 的边界见下方已知限制。

如果只是 `User → LLM → Answer`，或只是固定 Workflow，都不是本项目目标。

目标是：

> 由模型在 Agent Loop 中根据当前上下文和 Tool Result 自主决定下一步行动。

### 已知限制（当前实现）

- `git_diff` 只显示已跟踪内容的差异，未跟踪文件需要 `git_status` 定位后用 `read` 查看
- 长任务中不能追加 steering / follow-up 输入；当前只能等待或取消，M8.0 以前先用真实任务验证需求
- `read` 分页输出前仍会读取整个文件；多候选 `/sessions` 与 `--continue` 会逐个完整加载会话，规模表现待 M7.9.3 测量。损坏候选会严格阻断列表和自动恢复，不静默跳过
- `edit` 仅支持精确唯一匹配，无 fuzzy 匹配（缩进/智能引号差异会失败）
- 工具串行执行，无并行
- **`bash` 不是沙箱**：文件工具的 Workspace 边界只约束 read/write/edit/search/git_diff，`bash` 是 `cwd = workspace root` 的无沙箱本地 shell（可以读写 workspace 之外、可以联网），也没有危险命令确认机制
- 当前请求按 wire 消息与工具 schema 估算，M7.8.2 已用真实 DeepSeek input usage 校准字符类权重及工具模式固定开销（[四组固定样本记录](docs/benchmarks/m7-8-token-estimation.md)）；OpenAI、长真实会话与大量工具的偏差尚未量化。摘要切点与成本模型继续使用独立的消息区域估算，预测始终标为 estimated
- 手动 `/compact`、prompt 前与工具轮之间的窗口触发，以及旧工具结果的成本感知提前压缩都已可用；摘要成本收益只有离线估算记录，未做真实计费对照
- 已知模型的窗口都是 1M 级；自定义小窗口的自动压缩只经离线夹具验证，尚无真实长任务样本。M7.7b 只用 DeepSeek 验证了手动 `/compact` 事务后的继续与 resume，OpenAI 因未配置 Key 未测
- `prompt_toolkit` 是可降级能力：非 tty 或未安装时回退单行 REPL；交互终端缺依赖会在 REPL 顶部打印降级原因（补全/历史/多行编辑不可用）
- `search` 的 `.gitignore` 规则仅在 rg 引擎下生效，Python 兜底使用固定忽略目录
- 支持平台为 macOS / Linux；进程组与文件权限语义依赖 POSIX，Windows 未支持
- LSP / MCP / Task / Memory / Multi-Agent 属于后续阶段，路线见 [`docs/plans/phase3-runtime-hardening.md`](docs/plans/phase3-runtime-hardening.md)

---

## 10. 测试

- 核心链路（Loop / Registry / Workspace / Tool / 截断 / 超时 / 取消）全部用 pytest 覆盖
- Agent 测试使用 `FakeLLMClient`（脚本化事件流），不调用真实 API
- 真实 API 测试标记 `@pytest.mark.integration`，默认排除；当前有基础连通性与「压缩后继续 + resume」用例
- `tests/integration/` 是离线端到端目录：走生产装配路径（真实工具、真实 JSONL、FakeLLM），不加 `integration` marker
- 文件工具测试使用 `tmp_path`，不触碰真实项目文件

详见 [`docs/plans/phase1-core-runtime.md`](docs/plans/phase1-core-runtime.md) 与 [`docs/plans/phase2-session-context.md`](docs/plans/phase2-session-context.md) 的验收命令。

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
- Pi 生产架构参考：[`docs/design/pi-production-architecture.md`](docs/design/pi-production-architecture.md)
- Phase 1 计划：[`docs/plans/phase1-core-runtime.md`](docs/plans/phase1-core-runtime.md)
- Phase 2 计划：[`docs/plans/phase2-session-context.md`](docs/plans/phase2-session-context.md)
- Phase 3 计划（M7.8-M10）：[`docs/plans/phase3-runtime-hardening.md`](docs/plans/phase3-runtime-hardening.md)
- M7 成本与验收记录：[`docs/benchmarks/`](docs/benchmarks/)（用量基准、成本感知压缩、真实压缩验收、tty 输入与取消记录）
