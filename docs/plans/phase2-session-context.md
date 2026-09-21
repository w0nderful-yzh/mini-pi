# Phase 2: Session / Context 设计与实施计划

> 状态：实施中；M7.1、M7.2a-M7.2b 已完成，下一任务为 M7.2c。

**目标：** 在不扩大 Agent Core 的前提下，为 Phase 1 MVP 增加可恢复会话、项目指令加载和上下文压缩，使长任务能够跨进程继续，并为后续 LSP / MCP、Task / Memory 提供稳定的数据底座。

**设计依据：** 当前 `mini_pi` 的真实实现与测试；Pi 的生产路径 `packages/agent/src/agent-loop.ts`、`packages/coding-agent/src/core/session-manager.ts`、`agent-session.ts`、`resource-loader.ts`、`system-prompt.ts`、`compaction/`。详细源码链路见 [`../design/pi-production-architecture.md`](../design/pi-production-architecture.md)。借鉴数据模型和边界，不复制 Pi 的扩展、分支导航、队列、并发与新 harness 复杂度。

**总原则：** Session 保存完整事实，Context 是可重建投影；Agent Loop 只提供 turn 边界扩展点，不感知 JSONL、AGENTS.md 或摘要格式。

---

## 1. 现状与问题

Phase 1 已稳定完成以下闭环：

```text
用户任务 → LLM → Tool → Observation → LLM → 修改 → 验证 → 总结
```

当前实际限制：

- `AgentState.messages` 只在内存中，进程退出后 transcript 丢失。
- system prompt 是启动时生成的一段固定字符串，没有加载项目 `AGENTS.md`。
- 上下文无限增长；当前 `Usage` 已记录 token，但没有阈值、切点与压缩。
- `Agent` 同时承担“持续持有 transcript”的隐含会话职责，CLI 无法恢复一次历史任务。
- `run_loop()` 内部没有 turn 边界 hook，Context 无法在工具结果后、下一次 LLM 请求前安全替换投影。

因此下一步优先完成 M7。此时直接做 LSP / MCP，会让新工具建立在不可恢复、不可控长度的上下文之上，返工概率高。

---

## 2. 范围与非目标

### 本阶段交付

- JSONL Session：创建、追加、校验、恢复、继续最近会话。
- `id` / `parentId` entry 链；本阶段只产生单链，但格式允许后续分支。
- 结构化 system prompt sections 与差量记录。
- 从 git 项目根到 workspace 的 `AGENTS.md` 加载。
- token 估算、安全切点、手动压缩与自动压缩。
- CLI 的 `--resume`、`--continue`、`--no-session`、`/new`、`/compact`。
- FakeLLM 单测、损坏文件与切点边界测试、真实长会话集成验收。

### 明确不做

- session tree 导航、fork、branch summary、标签、搜索 UI。
- steering / follow-up 队列、并行工具、asyncio。
- SQLite、数据库、云同步、多机恢复。
- skills、prompt templates、extension/plugin runtime。
- LSP、MCP、Task、Memory、Multi-Agent。
- embedding、RAG、Vector DB。

这些能力不为 M7 预建抽象；只保留 JSONL `parentId` 和清晰模块边界。

---

## 3. 目标架构

```text
CLI
 └── AgentSession                    # 一次可恢复会话的编排边界
      ├── Agent                      # 保留 Phase 1 运行时职责
      │    └── run_loop()
      │         ├── LLMClient
      │         └── ToolRegistry → Tool → Workspace
      ├── JsonlSession               # 完整、追加式事实记录
      └── Context
           ├── ProjectInstructions   # AGENTS.md
           ├── PromptSections        # 构建 / diff / replay / render
           └── Compaction            # 估算 / 切点 / 摘要 / 投影
```

依赖规则：

```text
CLI → AgentSession → Agent → run_loop
AgentSession → JsonlSession
AgentSession → Context
run_loop → LLMClient / ToolRegistry
Context → LLMClient（仅生成摘要时）

禁止：
Agent / run_loop → JsonlSession
Tool → Session / Context
CLI 直接拼 JSONL 或直接执行压缩
Context 直接执行文件修改工具
```

`Agent` 仍可脱离 Session 独立测试和使用；持久化是上层生命周期能力，不成为 Agent Core 的必选依赖。

### 3.1 建议目录

```text
mini_pi/
├── session/
│   ├── models.py          # Header / MessageEntry / CompactionEntry
│   ├── jsonl.py           # 创建、严格加载、追加、当前 leaf
│   └── runtime.py         # AgentSession：run/resume/new/compact
├── context/
│   ├── project.py         # AGENTS.md 发现与加载
│   ├── prompt.py          # sections 构建、diff、replay、render
│   ├── projection.py      # entry path → 当前模型消息
│   └── compaction.py      # token、切点、摘要与压缩结果
└── agent/
    ├── agent.py           # 增加消息提交 hook，不持有 Session
    └── loop.py            # 增加 prepare_next_turn hook
```

不新增 `Manager` / `Service` / Repository 层。`JsonlSession` 直接负责这一种已确定的持久化格式。

---

## 4. 核心设计

### 4.1 JSONL 是事实源，AgentState 是运行投影

文件位置：

```text
~/.mini-pi/sessions/<sha256(resolved-cwd)[:16]>/<timestamp>_<session-id>.jsonl
```

第一行 header：

```json
{"type":"session","version":1,"id":"uuid","timestamp":"ISO-8601","cwd":"/abs/workspace","provider":"deepseek","model":"deepseek-chat"}
```

后续 entry 只支持两种：

```text
message
  id / parentId / timestamp / provider / model / message

compaction
  id / parentId / timestamp
  summary / firstKeptEntryId / tokensBefore
  systemMessage / usage / modifiedFiles
```

规则：

- entry append 时成为当前 leaf 的 child，然后推进 leaf。
- header 的 provider / model 是创建时默认值；每条 message entry 同时记录当时使用的 provider / model，resume 取路径上最后一个值，因此 `/connect` 后的后续消息不会让配置回退。
- 创建用独占写；每次 append 写一行、flush、`os.fsync()`。
- 加载时用 Pydantic 严格校验；未知版本、非法 JSON、重复 id、缺失 parent、cwd 不一致明确报错。
- 不跳过坏行，不猜测修复，不用空对象兜底。
- 原始 message entry 永不因压缩删除或重写。
- 本阶段没有 fork API，但保留 `parentId`，避免未来迁移线性格式。

### 4.2 消息提交 hook

持久化不通过“run 完成后整段 save”，否则工具已修改文件但进程中断时会丢失关键 observation。

`Agent` / `run_loop` 增加统一的可选提交 hook：

```python
MessageCommit = Callable[[Message], None]
```

system、user、assistant、tool 四类完整消息都走同一个提交函数。启用 Session 时先 append JSONL，成功后再加入 `AgentState.messages`；写入失败直接冒泡，禁止继续生成与磁盘不一致的内存历史。

`ToolMessage` 增加只用于本地状态恢复的 `modified_files: list[str]`，Provider wire 转换忽略该字段。恢复时由历史 tool message 重建 `AgentState.modified_files`。

### 4.3 System prompt sections

将固定字符串拆成稳定 section：

```text
preamble
environment
rules
tools
project_context
```

Session 中的 `SystemMessage` 保存 section patch：值为文本表示新增/替换，`null` 表示删除。发给 Provider 前重放所有 system patch，折叠为一条完整 system message。

收益：

- `AGENTS.md` 或工具集变化时只记录差量，历史可解释。
- resume 后可以比较“历史 prompt 状态”和“当前项目状态”。
- compaction entry 自带当时完整 system snapshot，压缩后无需回放被裁掉的旧 system entry。

不在 M7 引入自定义 section、skills 或扩展 hook。

### 4.4 AGENTS.md 加载语义

- 若 workspace 位于 git 仓库内，从 git root 到 workspace 逐级查找 `AGENTS.md`，按父 → 子顺序加载。
- 非 git workspace 只检查 workspace 根目录。
- 同一目录只认精确文件名 `AGENTS.md`；不兼容 `CLAUDE.md`、override 或大小写变体。
- 文件读取失败、非 UTF-8，或 symlink 逃出“git root → workspace”发现区间时显式报错。
- 内容包在带 workspace 相对来源路径的 `<project_instructions>` 中，便于模型区分层级。
- 启动时加载，不扫描整个仓库；本阶段不根据每次工具目标动态切换子目录规则。

### 4.5 Context 投影

没有 compaction 时：沿 leaf 的 parent 链还原全部 message。

存在 compaction 时，使用当前路径上最后一条 compaction：

```text
完整 system snapshot
+ conversation summary
+ firstKeptEntryId 起的保留消息
+ compaction entry 之后的新消息
```

summary 以明确标记的 user-level context 投影，不提升为 system 权限；摘要中的命令和引用仍按历史数据处理。

### 4.6 Token 估算与切点

估算优先级：

1. 最近一条 assistant 的 `usage.total_tokens`。
2. 对其后的消息按 `ceil(chars / 4)` 追加估算。
3. 没有 usage 时对全部投影消息估算。

触发条件：

```text
estimated_tokens > context_window - reserve_tokens
```

切点从最新消息向前累计 `keep_recent_tokens`：

- 只能切在 user 或 assistant message。
- 永远不能让 tool result 脱离对应 assistant tool call。
- 优先 user turn 边界；单个 turn 超预算时才允许从 assistant 边界切。
- 找不到安全切点时压缩失败，不丢消息、不强行裁剪。

内置默认模型可以提供显式 `context_window` 配置；未知自定义模型未配置窗口时不偷偷猜值，自动压缩关闭并在启动信息中明确显示，手动 `/compact` 仍可使用。

### 4.7 压缩事务

固定摘要结构：Goal、Constraints、Progress、Key Decisions、Next Steps、Critical Context、read/modified files。

事务顺序：

```text
准备待摘要消息与安全切点
  ↓
调用 LLM（tools=None）生成摘要
  ↓ 成功
严格校验非空摘要
  ↓
append CompactionEntry
  ↓
从 JSONL 重新 build context
  ↓
一次性替换 AgentState.messages
```

任一步失败都不写半条 compaction，不删除旧消息，不改变当前投影。重复压缩将上一份 summary 作为历史输入，而不是重新发送所有已压缩原文。

### 4.8 turn 边界 hook

`run_loop()` 增加可选：

```python
prepare_next_turn: Callable[[AgentState], list[Message] | None]
```

只在本轮 assistant 与全部 tool results 已提交、下一次 LLM 请求尚未开始时调用。返回新投影时整体替换 `state.messages`；返回 `None` 保持原状。

Loop 不知道 hook 做的是压缩、prompt refresh 或其他 Context 工作。M7 不借此加入 steering / follow-up 队列。

---

## 5. M7 子里程碑

### M7.1 Session Schema 与 JSONL 基础（已完成）

提交：`fce8686`

交付物：

- `mini_pi/session/models.py`：严格 version=1 header、MessageEntry / CompactionEntry 判别联合、JSON alias 与时区/cwd/非空字段约束。
- `mini_pi/session/jsonl.py`：按 cwd 哈希定位，独占创建，durable-first append + fsync，load、leaf 推进与文件发现。
- 树结构校验：单根、id 唯一、parent 必须指向更早 entry、compaction 切点必须属于当前分支。
- Fail Fast：错误 header、未知版本、未知字段、空行、损坏/半行 JSON、孤儿 parent 与 cwd 冲突全部抛 `SessionError`。

验收：Session 专项 19 passed；`uv run pytest -q` → 177 passed, 3 deselected；compileall 与 `git diff --check` 通过。

### 审查批次规则

M7.2 之后不再按整个子里程碑一次实现，默认以一个任务编号作为一次审查批次：

1. 一次只执行一个编号任务，例如只做 `M7.2a`，不得顺带实现 `M7.2b`。
2. 每个任务只引入一个可观察行为，并同时补齐该行为的针对性测试。
3. 每个任务完成后执行针对性测试、全量离线测试与 `git diff --check`，然后停止并等待审查。
4. 每个任务只创建一个提交，代码、测试、README 与计划状态必须一起提交；提交格式遵循 `feat/fix/docs: 中文说明`。
5. 汇报必须列出：任务编号、行为变化、文件清单、测试命令、测试结果、提交号和明确未做项。
6. 未经确认不提前创建下一任务的接口、占位实现或兼容分支；后续任务需要的抽象到真正使用时再引入。

### M7.2 Project Context 与 Prompt Sections

#### M7.2a：发现项目级 `AGENTS.md`（已完成）

提交：`73730b7`

交付物：

- `context/project.py`：git root → workspace 父子顺序发现，非 git 根目录模式，workspace 相对来源路径。
- 所有候选读取经 Workspace；拒绝发现区间外 symlink，非 UTF-8 与读取错误 Fail Fast。
- 精确识别 `AGENTS.md`，在大小写不敏感文件系统上也不误读 `agents.md` / `CLAUDE.md`。
- 明确未做：prompt 拼装、Agent 注入、Provider 改动、JSONL 持久化。

验收：专项 8 passed；全量 `185 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.2b：建立 Prompt Section 数据模型（已完成）

提交：本任务提交（`feat: 建立 Prompt Section 数据模型`）。

交付物：

- `PromptSection` 不可变模型，以及固定顺序的 `build_sections()`、统一标题边界的 `render_sections()`。
- section id 固定为 `preamble / environment / rules / tools`；空 tools section 仍保留标题。
- `build_system_prompt()` 改为 sections 构建与渲染，Phase 1 接口及 prompt 文本保持字节级兼容。
- 特殊字符按模型原文保留；明确未做 diff/replay、`SystemMessage` 改造、项目规则注入和 Provider 改动。

验收：专项 4 passed；Agent 回归 11 passed；全量 `189 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.2c：实现 Section Patch、Diff 与 Replay

- [ ] 扩展 `SystemMessage`，支持完整快照与 section patch 两种互斥载荷。
- 实现纯函数：当前 section 集合 diff、patch 校验、按历史 replay。
- 删除 section 使用显式 tombstone；未知操作、重复 id、非法 patch Fail Fast。
- 针对性测试覆盖：新增、修改、删除、无变化、重复 replay、legacy content 与非法 patch。
- 不做：Provider 折叠、Agent 自动刷新。

验收命令：`uv run pytest tests/llm/test_messages.py tests/context/test_sections.py -q`。

#### M7.2d：Provider 折叠为唯一 System Prompt

- [ ] OpenAI / DeepSeek wire 层在请求前 replay 所有 `SystemMessage`，只发送一个完整 system prompt。
- Provider-specific 差异仍留在各自 Client；Agent 与 Context 不感知请求体差异。
- 针对性测试断言：快照、patch、删除混合历史最终只产生一个确定 system message。
- 不做：加载 `AGENTS.md`、写 Session。

验收命令：`uv run pytest tests/llm -q`。

#### M7.2e：Agent 注入并刷新项目规则

- [ ] Agent 每次 `run()` 前构建目标 sections：首次追加完整快照，内容变化时仅追加 patch，无变化时不追加消息。
- 项目规则 section 展示来源路径，并按 M7.2a 的父子顺序渲染。
- prompt refresh 只改变 `AgentState.messages`，持久化留给 M7.3。
- 针对性测试覆盖：首次运行、规则未变化、文件修改、文件删除和连续两次运行。
- 不做：resume、CLI Session 参数、compaction。

验收命令：`uv run pytest tests/agent -q`。

M7.2 总验收：同一段历史重放后 Provider 得到唯一、确定的 system prompt；项目规则来源路径可见。

### M7.3 AgentSession、持久化与 Resume

#### M7.3a：完整消息提交 Hook

- [ ] 为 Agent / Loop 增加单一消息提交回调，覆盖 system、user、assistant、tool 四种完整消息。
- 回调只在消息完成后触发；流式 delta 不落盘，tool result 必须在执行完成后提交。
- 持久化模式采用 durable-first：写入失败时消息不得进入内存历史，循环立即失败。
- `ToolMessage` 显式携带本次工具的 `modified_files`，供 Session entry 保存。
- 不做：创建 Session、resume、CLI 接线。

验收命令：`uv run pytest tests/agent/test_agent.py tests/agent/test_loop.py -q`。

#### M7.3b：创建模式 `AgentSession`

- [ ] 新增 `session/runtime.py::AgentSession`，只实现 create 与 run。
- 创建时写 header；运行时把 M7.3a 的提交回调接入 `JsonlSession.append()`。
- 运行中的 provider、model、step_count 与 modified_files 写入既定 entry 字段，不新增旁路状态文件。
- 针对性测试覆盖：纯文本轮、工具轮、写盘失败和 parent 链推进。
- 不做：resume、CLI flags、`/new`。

验收命令：`uv run pytest tests/session/test_runtime_create.py -q`。

#### M7.3c：从活动分支恢复基础状态

- [ ] 为 `JsonlSession` 提供按 leaf 沿 parent 回溯并正序返回活动分支的只读能力。
- 仅从 `message` entry 恢复 messages、累计 step_count、modified_files 与最后使用的 provider / model。
- 本任务暂不解释 compaction entry；遇到 compaction 明确报“需要 M7.4 投影”，禁止静默跳过。
- 针对性测试覆盖：线性链、非 leaf 分支、空消息链、重复修改文件和含 compaction 的拒绝路径。
- 不做：真正运行 resumed Agent、CLI。

验收命令：`uv run pytest tests/session/test_replay.py -q`。

#### M7.3d：Resume 模式 `AgentSession`

- [ ] `AgentSession.resume()` 加载 M7.3c 的状态并继续沿原 leaf 追加。
- session cwd 不存在、显式 cwd 与 header 冲突时 Fail Fast。
- 未显式覆盖 provider / model 时沿用最后记录值；凭据仍只从认证配置读取。
- 针对性测试覆盖：跨实例恢复继续提问、leaf 连续、cwd 冲突与缺失凭据。
- 不做：CLI 参数、compaction-aware resume。

验收命令：`uv run pytest tests/session/test_runtime_resume.py -q`。

#### M7.3e：CLI 默认 Session 与 `--no-session`

- [ ] CLI 默认创建 Session；`--no-session` 保持当前纯内存行为。
- 默认存储目录、创建失败提示和最终 session path 使用 Rich 展示，但 CLI 不参与消息决策。
- 针对性测试覆盖：默认创建、显式禁用、启动失败和退出后文件可加载。
- 不做：`--resume`、`--continue`、`/new`。

验收命令：`uv run pytest tests/cli/test_session_create.py -q`。

#### M7.3f：CLI `--resume` 与 `--continue`

- [ ] `--resume <path>` 恢复指定文件；`--continue` 选择当前 cwd 最近的合法 Session。
- 两参数互斥；不存在、损坏、cwd 不匹配时给出明确错误并退出非零。
- “最近”只依据已验证 Session 的时间字段，不根据模糊文件名猜测。
- 针对性测试覆盖：成功恢复、互斥参数、无候选、多候选排序和损坏候选。
- 不做：交互命令 `/new`、自动压缩。

验收命令：`uv run pytest tests/cli/test_session_resume.py -q`。

#### M7.3g：`/new` 与 `/connect` 会话连续性

- [ ] `/new` 结束当前 Session 并创建新 Session；旧文件保持可恢复。
- `/connect` 只替换运行时 LLM，并在下一条 message entry 记录新的 provider / model。
- API Key 不进入消息、details、Session header 或 entry。
- 针对性测试覆盖：新会话 id/parent 重置、旧会话不变、切换模型后继续同一链和敏感信息不落盘。
- 不做：`/compact`。

验收命令：`uv run pytest tests/cli/test_session_commands.py -q`。

M7.3 总验收：运行一轮 → 退出进程 → resume → 继续提问；第二轮 LLM 收到第一轮历史，JSONL parent 链连续。

### M7.4 Context 投影、Token 与安全切点

#### M7.4a：活动 Entry 路径投影

- [ ] 新增 `context/projection.py`，将 Session 活动分支投影为有序 entry path。
- 投影输入不可变；孤儿、循环和非法 leaf 明确报错。
- 与 M7.3c 的 Session 读取逻辑去重，但本任务不改变 Agent resume 行为。
- 不做：compaction 解释、token、切点。

验收命令：`uv run pytest tests/context/test_projection_path.py -q`。

#### M7.4b：Message Entry 投影

- [ ] 将 message entry path 还原为模型 messages，并恢复结构化 system section 状态。
- 校验 assistant tool call 与 tool result 的 id 配对；不完整配对 Fail Fast。
- 针对性测试覆盖四种 role、连续工具调用、legacy system message 和坏配对。
- 不做：compaction entry、运行时接线。

验收命令：`uv run pytest tests/context/test_projection_messages.py -q`。

#### M7.4c：Compaction Entry 投影

- [ ] 选择活动路径上最新 compaction，投影为 system snapshot + summary + kept messages。
- 重复 compaction 只使用最新有效投影；切点必须属于该 compaction 的祖先路径。
- 针对性测试覆盖：无 compaction、一次、连续两次、旧分支 compaction 与非法切点。
- 不做：生成摘要、自动触发。

验收命令：`uv run pytest tests/context/test_projection_compaction.py -q`。

#### M7.4d：Token 估算

- [ ] 新增独立 token estimator：有 provider usage 时优先使用，无 usage 时按统一字符规则保守估算。
- 返回结果标记来源，禁止把估算值伪装为 provider 精确值。
- 针对性测试覆盖：usage、中文、英文、空内容、tool arguments/result 和极长消息。
- 不做：阈值决策、压缩。

验收命令：`uv run pytest tests/context/test_tokens.py -q`。

#### M7.4e：基础安全切点

- [ ] 只允许在完整 user turn 或完整 assistant turn 边界切分。
- 至少保留最近一轮可用对话；无安全切点时返回显式结果而非强行截断。
- 针对性测试覆盖：尾随 user、尾随 assistant、空历史、单 turn 超长和刚好命中边界。
- 不做：split-turn tool 配对特例。

验收命令：`uv run pytest tests/context/test_cut_points.py -q`。

#### M7.4f：工具轮 Split-Turn 切点

- [ ] 扩展切点算法，保证 assistant tool calls 与对应 tool results 永不被拆散。
- 多工具调用必须作为一个不可分割 batch；缺少结果时只能整体保留。
- 针对性测试覆盖：单工具、多工具、部分结果、连续工具轮和超长单结果。
- 不做：调用摘要模型。

验收命令：`uv run pytest tests/context/test_cut_points_tools.py -q`。

#### M7.4g：Context Window 配置与阈值策略

- [ ] 定义模型 context window、reserve 与 auto-compaction threshold 配置。
- 已知模型可使用显式内置表；未知模型必须由用户配置，否则关闭自动压缩并给出明确提示。
- 纯函数返回“无需压缩 / 应压缩 / 无法判断”，不直接修改状态。
- 针对性测试覆盖：已知模型、未知模型、非法 reserve、临界值和估算来源。
- 不做：自动调用 compact。

验收命令：`uv run pytest tests/context/test_policy.py -q`。

#### M7.4h：Resume 使用统一 Context 投影

- [ ] `AgentSession.resume()` 切换为 M7.4a-c 的统一投影，解除 M7.3c 对 compaction 的临时拒绝。
- 恢复后的 state 与直接对同一 Session 投影的结果完全一致。
- 针对性测试覆盖：无压缩、一轮压缩、重复压缩和含 prompt patch 的恢复。
- 不做：创建新的 compaction entry。

验收命令：`uv run pytest tests/session/test_runtime_resume.py tests/context -q`。

M7.4 总验收：表驱动测试覆盖无 usage、尾随消息、连续工具调用、单 turn 超长、找不到安全切点、重复 compaction。

### M7.5 手动 Compaction

#### M7.5a：Transcript Serializer

- [ ] 新增供摘要模型读取的确定性 transcript serializer。
- role、tool name、call id、arguments、result 与错误状态显式可见；单条 tool result 输入按固定上限截断并标记。
- 序列化不得修改原始 message，也不得把 UI details 当作模型事实。
- 不做：摘要 prompt、LLM 调用。

验收命令：`uv run pytest tests/context/test_serializer.py -q`。

#### M7.5b：固定摘要协议与调用器

- [ ] 定义摘要 system/user 模板与结构要求，单次调用必须 `tools=None`。
- 摘要为空、只有空白、LLM error 或意外 tool call 均视为失败。
- FakeLLM 测试断言请求内容和调用参数，不调用真实网络。
- 不做：选择切点、写 Session。

验收命令：`uv run pytest tests/context/test_summarizer.py -q`。

#### M7.5c：准备 Compaction 输入

- [ ] 组合投影、token estimator 与安全切点，生成不可变的 compaction plan。
- plan 明确列出：待摘要 entries、保留 entries、firstKeptEntryId、tokensBefore、system snapshot、modifiedFiles。
- 找不到安全切点时返回可解释错误，不调用摘要器。
- 不做：LLM 调用与 append。

验收命令：`uv run pytest tests/context/test_compaction_plan.py -q`。

#### M7.5d：生成 Compaction Result

- [ ] 使用 M7.5b 为 M7.5c 的 plan 生成摘要与 usage 元数据。
- 重复压缩时把上一份 summary 作为历史摘要输入，不重新发送已压缩原文。
- split-turn 场景必须在摘要中保留未完成工具轮的必要上下文。
- 不做：写盘、替换 AgentState。

验收命令：`uv run pytest tests/context/test_compaction_result.py -q`。

#### M7.5e：Compaction 事务提交

- [ ] 仅在摘要成功并完成字段校验后 append `CompactionEntry`。
- append 成功后从 JSONL 重新 build 投影，再一次性替换 `AgentState.messages`。
- 摘要失败、空摘要、写盘失败或重建失败时，不写半条 entry、不删除旧消息、不改变内存投影。
- 不做：CLI 命令、自动触发。

验收命令：`uv run pytest tests/session/test_compaction_transaction.py -q`。

#### M7.5f：CLI `/compact [instructions]`

- [ ] 接入手动压缩命令；可选 instructions 仅进入本次摘要请求，不持久化为全局 prompt。
- 命令输出压缩前后 token、保留切点与 session path，不展示内部敏感配置。
- `--no-session` 模式明确拒绝该命令，不创建隐式 Session。
- 不做：自动压缩。

验收命令：`uv run pytest tests/cli/test_compact_command.py -q`。

M7.5 总验收：FakeLLM 生成摘要后，模型上下文缩短，JSONL 原始 message 数量不变，resume 得到相同投影。

### M7.6 自动 Compaction 与 turn hook

#### M7.6a：`prepare_next_turn` Hook

- [ ] 为 `run_loop` 增加可选 hook，只在完整 tool batch 已提交、下一次 LLM 请求前调用。
- hook 返回 messages 时整体替换投影，返回 `None` 时保持原状。
- 默认 `None` 必须保持 Phase 1 行为、事件顺序和测试完全不变。
- 不做：阈值判断、实际压缩。

验收命令：`uv run pytest tests/agent/test_loop.py -q`。

#### M7.6b：新用户 Prompt 前阈值检查

- [ ] `AgentSession.run()` 在追加新 user message 前检查当前投影是否达到阈值。
- 达到阈值时复用 M7.5 事务；未达到或策略无法判断时按 M7.4g 的明确结果处理。
- 压缩成功后只追加一次新 user message。
- 不做：工具轮中自动检查。

验收命令：`uv run pytest tests/session/test_auto_compact_before_prompt.py -q`。

#### M7.6c：工具轮之间自动压缩

- [ ] 把 M7.5 事务封装进 M7.6a hook，在 tool batch 后按阈值决定是否压缩。
- 成功后同一次 `run()` 继续下一次 LLM 请求；已执行工具绝不重放。
- 不做：LLM context overflow 后的自动 retry。

验收命令：`uv run pytest tests/session/test_auto_compact_tool_turn.py -q`。

#### M7.6d：自动压缩错误语义

- [ ] 摘要失败、无安全切点和写盘失败统一转为明确的 agent error 终止原因与事件。
- 错误后 Session 与 AgentState 仍指向压缩前的有效投影；禁止带超限 context 继续请求。
- 未开始输出前的 Provider 重试规则保持原样，压缩层不额外重试。
- 不做：overflow recovery、降级删除历史。

验收命令：`uv run pytest tests/session/test_auto_compact_errors.py -q`。

#### M7.6e：自动压缩端到端离线验收

- [ ] 用 FakeLLM 构造跨阈值工具轮，验证完整事件与持久化序列。
- 断言调用顺序为 assistant → tool → compact → assistant，工具仅执行一次。
- 退出并 resume 后再次投影，结果必须与压缩后的内存状态一致。
- 不做：真实模型网络测试，留给 M7.7。

验收命令：`uv run pytest tests/integration/test_auto_compaction.py -q`。

M7.6 总验收：自动压缩后在同一次 run 中继续，工具不重复，失败时不破坏原 Session。

### M7.7 回归、真实验收与文档同步

#### M7.7a：离线全量回归与不变量补强

- [ ] 运行全部非 integration 测试，并针对发现的覆盖缺口只补 Session / Context 不变量测试。
- 明确验证：默认无网络、`--no-session` Phase 1 兼容、JSONL append-only、tool pair 不拆分。
- 不做：真实模型请求、文档状态更新。

验收命令：`uv run pytest -q`、`uv run python -m compileall mini_pi tests`、`git diff --check`。

#### M7.7b：真实模型长会话 Integration

- [ ] 新增 `integration` marker 测试，使用用户已配置 Provider 验证长会话自动压缩后继续完成任务。
- 测试缺少凭据时 skip，不影响默认离线套件；日志不得输出 API Key。
- OpenAI 与 DeepSeek 至少完成一个 Provider 的真实验收，另一个未测状态必须如实记录。
- 不做：为通过测试增加新 Provider 或网络 fallback。

验收命令：`uv run pytest -m integration <目标测试> -q`。

#### M7.7c：人工 CLI 场景验收

- [ ] 按脚本人工验证 create → 工具调用 → exit → resume → `/compact` → continue → `/new`。
- 保存可复核的命令、Session 路径与非敏感结果摘要，不提交真实 Session 文件。
- 每个失败点先记录实际行为，再决定是否创建修复任务，禁止在验收任务里顺带大改。
- 不做：文档宣称未执行的场景通过。

验收：人工验收清单逐项签名，敏感信息扫描无命中。

#### M7.7d：文档收尾

- [ ] 更新 README 路线图、目录、CLI、设计取舍、已知限制与最新测试结果。
- 将本计划已完成任务压缩成“交付物 + 验收 + 提交号”，保留未完成任务的细分清单。
- 核对 README、AGENTS.md、CLI `--help` 与真实行为一致。
- 不做：任何运行时代码变化；发现代码问题另开任务。

验收命令：`uv run mini-pi --help`、`git diff --check`。

M7 完成标准：

```text
会话可恢复
+ 项目 AGENTS.md 生效
+ context 超阈值可在 turn 边界压缩
+ 原始历史不丢失
+ 压缩后任务继续执行且工具不重复
```

---

## 6. M7 之后的路线图

以下仅定义边界与准入条件，不在 M7 实现时顺带开发。

### M8 LSP / MCP

准入：M7 全部完成，Session 能持久化动态工具声明与长上下文。

1. **M8.1 LSP 只读能力**：definition、references、document symbols、diagnostics；LSP 作为 Tool adapter，只依赖 Workspace，不进入 Agent。
2. **M8.2 MCP stdio client**：initialize、list_tools、call_tool、进程生命周期、schema → ToolRegistry 命名空间映射。
3. **M8.3 动态工具集**：工具增删触发 prompt section diff 并写入 Session；resume 后重建同一可见工具集。
4. **M8.4 验收**：语言服务/MCP server 崩溃转可预期 ToolError；Agent Core 与 Provider 层无协议分支。

不做 MCP server、远程 OAuth、HTTP transport、LSP 自动改代码，除非出现明确需求。

### M9 Task / Memory

准入：有真实跨 Session 工作流和可归纳的重复信息，不从 transcript 直接堆“自动记忆”。

1. Task：目标、状态、验收、关联 session id；先 JSON 文件，不上数据库。
2. Memory：从已完成 Task 显式提炼事实与决策，记录来源；默认人工确认后写入。
3. 注入：按项目和明确 key 检索，不做 embedding / Vector DB。
4. 验收：未知/过期信息有来源和状态，不把摘要当事实静默注入。

### M10 Multi-Agent

准入：单 Agent 的 Session、Context、Task、工具权限已经稳定，并出现可并行且需隔离上下文的真实任务。

1. 每个子 Agent 独立 AgentState / Session，不共享可变 transcript。
2. 父 Agent 只通过结构化 task/result 协议协作。
3. 文件写入先隔离 workspace/worktree，再讨论并发；禁止多个 Agent 无协调写同一目录。
4. 先做一个 worker + 一个 reviewer 的确定场景，再考虑调度器。

---

## 7. 关键风险与验证策略

| 风险 | 约束与验证 |
| --- | --- |
| JSONL 与内存分叉 | durable-first 消息提交；写盘失败立即停止 |
| resume 注入过期 prompt | sections replay 后与当前 sections 做 diff |
| 压缩切断 tool 配对 | 切点单测 + projection invariant |
| 摘要丢失关键文件状态 | 固定 summary schema + modifiedFiles 结构字段 |
| 压缩后重复执行工具 | hook 仅位于已提交 tool batch 与下一请求之间；端到端断言一次执行 |
| 自定义模型窗口未知 | 不猜测；显式配置或明确关闭 auto-compaction |
| Pi 复杂度外溢 | M7 非目标清单作为 code review gate |

每个子里程碑遵循：失败测试 → 最小实现 → 针对性测试 → 全量回归 → 中文提交。未执行真实命令不得记录“通过”。

---

## 8. 推荐实施顺序

```text
M7.1 JSONL（已完成）
  ↓
M7.2a → 2b → 2c → 2d → 2e
项目规则发现   Section 模型   Patch/Replay   Provider 折叠   Agent 接入
  ↓ 每个编号审查通过后再继续
M7.3a → 3b → 3c → 3d → 3e → 3f → 3g
提交 Hook   创建运行时   基础恢复   Resume   默认 Session   CLI 恢复   交互命令
  ↓ 首个完整用户可见闭环
M7.4a → 4b → 4c → 4d → 4e → 4f → 4g → 4h
路径投影   消息投影   压缩投影   Token   基础切点   工具切点   阈值策略   Resume 接入
  ↓
M7.5a → 5b → 5c → 5d → 5e → 5f
序列化   摘要调用   压缩计划   生成结果   事务提交   手动命令
  ↓ 先把手动事务做对
M7.6a → 6b → 6c → 6d → 6e
Turn Hook   Prompt 前检查   工具轮检查   错误语义   离线端到端
  ↓ 再启用自动压缩
M7.7a → 7b → 7c → 7d
离线回归   真实模型   人工 CLI   文档收尾
```

当前唯一允许开始的下一任务是 `M7.2c`。不要把相邻编号合并成一次改动；先证明当前编号的行为与不变量，再进入下一编号。
