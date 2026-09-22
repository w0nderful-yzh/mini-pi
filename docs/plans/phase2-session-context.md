# Phase 2: Session / Context 设计与实施计划

> 状态：实施中；M7.1-M7.4 已完成，下一任务为 M7.C1（CLI 展示与上下文边界），随后继续 M7.5a。

**目标：** 在不扩大 Agent Core 的前提下，为 Phase 1 MVP 增加可恢复会话、项目指令加载、上下文压缩和可长期使用的 CLI，使长任务能够跨进程继续，并为后续 LSP / MCP、Task / Memory 提供稳定的数据底座。

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
- M7.2 已使项目 `AGENTS.md` 生效并支持跨 run 刷新；规则仍只在内存 transcript 中，退出后无法恢复。
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
- CLI 的思考状态、工具事件摘要、用量与上下文可观测性；交互输入与会话列表的后续优化。
- FakeLLM 单测、损坏文件与切点边界测试、真实长会话集成验收。

### 明确不做

- session tree 导航、fork、branch summary、标签、搜索 UI。
- steering / follow-up 队列、并行工具、asyncio。
- SQLite、数据库、云同步、多机恢复。
- skills、prompt templates、extension/plugin runtime。
- LSP、MCP、Task、Memory、Multi-Agent。
- embedding、RAG、Vector DB。
- 为“简单任务”写死读取次数、强制固定工具调用顺序，或为了展示而篡改模型事实。

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
│   ├── sections.py        # section diff、patch 应用与历史 replay
│   ├── projection.py      # entry path → 当前模型消息
│   └── compaction.py      # token、切点、摘要与压缩结果
└── agent/
    ├── agent.py           # 增加消息提交 hook，不持有 Session
    ├── prompt.py          # sections 构建与文本渲染
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
{"type":"session","version":1,"id":"uuid","timestamp":"ISO-8601","cwd":"/abs/workspace","provider":"deepseek","model":"deepseek-flash"}
```

后续 entry 只支持两种：

```text
message
  id / parentId / timestamp / provider / model / stepCount / message

compaction
  id / parentId / timestamp
  summary / firstKeptEntryId / tokensBefore
  systemMessage / usage / modifiedFiles
```

规则：

- entry append 时成为当前 leaf 的 child，然后推进 leaf。
- header 的 provider / model 是创建时默认值；每条 message entry 同时记录当时使用的 provider / model，resume 取路径上最后一个值，因此 `/connect` 后的后续消息不会让配置回退。
- 新建 AgentSession 的每条 message entry 记录提交时累计 `stepCount`；旧 M7.1 文件缺少该字段时，基础回放按完整 assistant 消息数推导。工具改动文件保存在 `ToolMessage.modified_files`，不另设旁路状态。
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

`SystemMessage` 的载荷严格三选一：Phase 1 的 opaque `content`、完整 `sections` 快照、或 `section_patch` 操作列表。Patch 中 `set` 必须携带文本，`delete` 不携带 content 并作为显式 tombstone；同一 patch 不允许重复 section id。Provider 请求前重放全部 system 消息，按固定 section 顺序渲染为请求首部唯一一条完整 system message；无 system 历史时不添加。

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
- 每次 `Agent.run()` 前重新加载 root → workspace 祖先链，不扫描整个仓库；本阶段不根据每次工具目标动态切换子目录规则。

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

### 4.9 CLI 展示与模型上下文边界

CLI 只消费现有 `AgentEvent` 和 Session / Context 的只读状态。终端渲染、状态图标、`--verbose` 与统计信息不写入 `AgentState.messages`、JSONL message 或 Provider 请求。Assistant 正文、tool call / result 仍按原协议提交；不能为了让界面简洁而删掉模型所需的 observation。长工具输出继续由 Tool 层有界截断，历史缩短交给 M7.5 / M7.6 compaction。

`thinking_delta` 与 `AssistantMessage.reasoning_content` 是不同边界：默认 CLI 不渲染原始思考内容；OpenAI wire 不回放它，DeepSeek 仍按现有协议在需要时回放。不得用 `I should` 等正文关键词猜测、删除普通 assistant 文本，也不得为隐藏终端内容改写已持久化的消息。若将来要取消持久化 reasoning，必须先证明 DeepSeek tool-call 回放和 resume 兼容，并单独修改协议与测试。

上下文占用以 M7.4g 的 `context_window - reserve_tokens` 策略作为唯一自动压缩/停止依据。来源方案中的 70% / 85% / 95% 可作 `/context` 提示区间，不另造三套触发规则；窗口未知时显示“未知”，不伪造百分比或自动压缩。状态统计应区分 Provider 返回的实际 usage 与本地估算，分类只针对当前投影，避免重复计数。

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

#### M7.2c：实现 Section Patch、Diff 与 Replay（已完成）

提交：本任务提交（`feat: 实现 Section Patch 与历史回放`）。

交付物：

- `SystemMessage` 支持 legacy content、完整 sections 快照、section patch 三种互斥载荷。
- `SectionPatch` 使用严格 `set/delete` 协议；delete 是显式 tombstone，未知操作、重复 id、空 patch 与非法 content Fail Fast。
- `context/sections.py` 提供稳定 diff、纯函数 patch 应用和顺序 replay；legacy content 保持 opaque 快照。
- 明确未做：Provider 折叠、Agent prompt refresh 与项目规则注入。

验收：专项 11 passed；LLM/Session 兼容回归 20 passed；全量 `205 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.2d：Provider 折叠为唯一 System Prompt（已完成）

提交：本任务提交（`feat: 折叠 Provider System Prompt`）。

交付物：

- `context/sections.py` 统一渲染完整 section 状态；`agent/prompt.py` 保留原导出，避免 LLM → Agent 反向依赖。
- OpenAI 与 DeepSeek 共用 wire 转换：请求首部最多一条 system 消息，历史快照、patch 与删除均先 replay；其他角色保持原顺序。
- 旧版多条 `content` 取最后完整快照；非法 patch 在请求前失败；DeepSeek 的 `reasoning_content` 回放保持不变。
- 明确未做：`AGENTS.md` 注入、Agent prompt refresh、Session 持久化。

验收：Provider 专项 6 passed；`uv run pytest tests/llm -q` 10 passed；全量 `211 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.2e：Agent 注入并刷新项目规则（已完成）

提交：本任务提交（`feat: 注入并刷新项目规则`）。

交付物：

- `Agent.run()` 前经 Context/Workspace 重新发现规则；首次追加完整 sections 快照，后续只在变化时追加 `set/delete` patch。
- `project_context` 依 git root → workspace 顺序包裹各级规则与相对来源路径；文件内容原样保留，XML 路径属性安全转义。
- 规则修改、增加、删除与 reset 后重建均有测试；读取失败发生在追加本轮 user 消息和调用 LLM 之前。
- 明确未做：Session 持久化、resume、CLI Session 参数和 compaction。

验收：`uv run pytest tests/agent -q` 12 passed；Agent 回归 19 passed；全量 `219 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

M7.2 总验收（已完成）：同一段历史重放后 Provider 得到唯一、确定的 system prompt；项目规则来源路径可见。

### M7.3 AgentSession、持久化与 Resume（已完成）

#### M7.3a：完整消息提交 Hook（已完成）

提交：本任务提交（`feat: 增加完整消息提交 Hook`）。

交付物：

- `Agent` / `run_loop` 共用可选 `MessageCommit` 回调；system、user、assistant、tool 完整消息均先提交、成功后才加入内存历史。
- 流式 delta 不触发提交；工具执行完成后才提交 observation；回调失败直接冒泡，不追加失败消息、不继续模型循环。
- `ToolMessage.modified_files` 只记录本次工具明确声明的改动，Provider wire 忽略；内存改动集合在工具消息提交成功后更新。
- 明确未做：创建 Session、JSONL 接线、resume、CLI 参数与 compaction。

验收：专项 `11 passed`；全量 `230 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.3b：创建模式 `AgentSession`（已完成）

提交：本任务提交（`feat: 实现 AgentSession 创建模式`）。

交付物：

- `AgentSession.create/run` 装配 Agent 与 JsonlSession；创建时同步写 header，每条完整消息通过 M7.3a 回调同步追加 JSONL 后才进入内存。
- message entry 记录当前 provider、model 与累计 `stepCount`；旧 M7.1 entry 允许缺省该字段；工具改动仍由 `ToolMessage.modified_files` 记录。
- 纯文本双轮、工具轮、项目规则 patch、parent 链与写盘失败均有离线测试；失败消息不推进 Session leaf 或 Agent 消息历史。
- 明确未做：resume、CLI flags、`/new`、compaction。

验收：`uv run pytest tests/session/test_runtime_create.py -q` → 5 passed；全量 `235 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.3c：从活动分支恢复基础状态（已完成）

提交：本任务提交（`feat: 回放 Session 活动分支基础状态`）。

交付物：

- `JsonlSession.active_entries()` 从当前或指定 leaf 沿 parent 回溯，正序返回独立副本，不移动当前 leaf。
- `replay()` 从活动分支消息重建 messages、累计 step_count、去重 modified_files 和最后使用的 provider/model；空链使用 header 默认值，旧 entry 缺少 `stepCount` 时按 assistant 数推导。
- 活动路径含 compaction 时明确报需 M7.4 投影；线性链、非 leaf 分支、空链、重复文件、坏 leaf 与 compaction 拒绝均有测试。
- 明确未做：`AgentSession.resume()`、CLI 接线、compaction-aware 投影。

验收：`uv run pytest tests/session/test_replay.py -q` → 6 passed；全量 `241 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.3d：Resume 模式 `AgentSession`（已完成）

提交：本任务提交（`feat: 实现 AgentSession 恢复模式`）。

交付物：

- `AgentSession.resume()` 加载 M7.3c 的活动分支状态，恢复消息、累计步骤与改动文件，后续完整消息沿原 leaf 追加。
- 未覆盖时沿用活动路径最后的 provider/model，显式覆盖只影响新 entry；默认客户端只从环境变量或用户认证文件取 Key，Session 文件不保存凭据；离线测试可注入客户端构造函数。
- session cwd 丢失、显式 cwd 不符、缺失凭据及 compaction entry 均在运行前显式失败；跨实例工具轮、模型元数据与 leaf 连续性均有测试。
- 明确未做：CLI 参数、`/new`、compaction-aware resume。

验收：`uv run pytest tests/session/test_runtime_resume.py -q` → 7 passed；全量 `248 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.3e：CLI 默认 Session 与 `--no-session`（已完成）

提交：本任务提交（`feat: CLI 默认创建 Session 并支持纯内存模式`）。

交付物：

- CLI 默认通过 `AgentSession.create()` 新建 JSONL，Rich 展示存储目录、创建错误与最终路径；`--no-session` 保持原内存 Agent 行为。
- 首次缺 Key 时 `/connect` 可创建 Session；持久化模式在 M7.3g 前拒绝 `/reset` 和活动会话中的 `/connect`，避免内存历史与文件或模型元数据不一致。
- 离线测试覆盖默认创建、显式禁用、创建失败、退出后加载、首次连接及两种模式的 reset 边界；未做 CLI 恢复、`/new`、compaction。

验收：`uv run pytest tests/cli/test_session_create.py -q` → 7 passed；全量 `255 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.3f：CLI `--resume` 与 `--continue`（已完成）

提交：本任务提交（`feat: CLI 支持恢复与继续最近会话`）。

交付物：

- `--resume <path>` 沿指定文件原 leaf 恢复；`--continue` 严格加载当前 workspace 的所有候选，按已验证的最后 entry 时间选最近活动会话（空会话用 header 时间），不依据文件名或 mtime。
- 参数互斥、无候选、坏文件、cwd 不符和最新时间并列均在启动时显式非零退出；候选损坏不静默退回旧会话，`--no-session` 也不能与恢复选项混用。
- 恢复默认沿用会话中的最后 provider/model；显式覆盖只影响新 entry。未做 `/new`、活动会话 `/connect` 和自动压缩。

验收：`uv run pytest tests/cli/test_session_resume.py -q` → 13 passed；全量 `268 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.3g：`/new` 与 `/connect` 会话连续性（已完成）

提交：本任务提交（`feat: 完成会话新建与模型切换`）。

交付物：

- `AgentSession.new()` 用当前运行依赖创建独立 JSONL；CLI `/new` 切换到新文件、保留旧文件可回放，创建失败仍留在旧会话。纯内存模式继续使用 `/reset`。
- 活动会话的 `/connect` 验证 Key 后只替换运行时 LLM；后续 message entry 记录新 provider/model，旧 entry 与 header 不改写。Key 仅保存于用户认证文件，不进入 Session 或 CLI 输出。
- 从 Session 恢复后 `/connect` 沿用活动链实际模型；验证失败保持原客户端和历史。未做 `/compact` 或压缩感知恢复。

验收：`uv run pytest tests/cli/test_session_commands.py -q` → 8 passed；全量 `276 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

M7.3 总验收（已完成）：离线执行“运行一轮 → 退出进程 → resume → 继续提问”；第二轮 FakeLLM 收到第一轮历史，新增 JSONL entry 沿原 leaf 追加，原文件未另建副本。

### M7.4 Context 投影、Token 与安全切点（已完成）

#### M7.4a：活动 Entry 路径投影（已完成）

提交：本任务提交（`feat: 增加活动 Entry 路径投影`）。

交付物：

- `context/projection.py`：`project_entry_path(entries, leaf_id=...)` 纯函数，沿 parent 链返回 root → leaf 有序路径，不改写也不复制入参。
- 未知 leaf、孤儿 parent、parent 环、重复 id 均按损坏状态抛出 `SessionError`；`leaf_id=None`（空会话）返回空路径。
- `JsonlSession.active_entries()` 改为复用投影并保留深拷贝，与 M7.3c 读取逻辑去重，Agent resume 行为不变。
- 明确未做：compaction 解释、token 估算、切点。

验收：专项 `uv run pytest tests/context/test_projection_path.py -q` → 8 passed；全量 `288 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4b：Message Entry 投影（已完成）

提交：本任务提交（`feat: 增加 Message Entry 投影与工具配对校验`）。

交付物：

- `context/projection.py` 新增 `project_messages(entries)` → `MessageProjection(messages, system_prompt)`：按路径还原四种 role，并回放结构化 system section 快照/patch（legacy content 作为完整快照）。
- 校验 assistant tool_calls 与 tool result：批次必须完整、call id 全局唯一、结果不得孤儿，不满足即 `SessionError`（覆盖连续工具轮与多工具批次）。
- 路径含 compaction entry 时明确指向 M7.4c，不在本任务解释切点。
- 明确未做：compaction 投影、token、运行时接线。

验收：专项 `uv run pytest tests/context/test_projection_messages.py -q` → 11 passed；全量 `299 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4c：Compaction Entry 投影（已完成）

提交：本任务提交（`feat: 增加 Compaction Entry 投影`）。

交付物：

- `context/projection.py` 新增 `project_compaction(entries)` → `CompactionProjection | None`：取活动路径最新 compaction，产出 `system_prompt` 快照 + user 级 `<compacted-conversation-summary>` 摘要 + `kept_messages`，`messages` 属性按 system → 摘要 → 保留消息组装。
- 重复压缩只认最新一条；更早快照/摘要被吸收，兄弟分支 compaction 不参与。
- 切点校验：必须在活动路径、必须指向 message entry、必须严格位于 compaction 之前；保留区间再做一次工具配对校验。
- compaction 之后的 system patch 续接快照，摘要不提升为 system 权限。
- 明确未做：生成摘要、自动触发、运行时接线。

验收：专项 `uv run pytest tests/context/test_projection_compaction.py -q` → 9 passed；全量 `308 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4d：Token 估算（已完成）

提交：本任务提交（`feat: 增加 Token 估算`）。

交付物：

- 新增 `context/tokens.py`：`estimate_tokens(messages)` → `TokenEstimate(tokens, source)`，`source ∈ {usage, estimated}`。
- 优先级：最近一条 assistant 的 `usage.total_tokens` 全额采用，其后消息按 `ceil(chars / 4)` 追加；无 usage 时对全部消息估算。掺入任何估算就不得标记为 `usage`。
- 统计范围覆盖 user/assistant/system/tool：assistant 正文与 `reasoning_content`、工具名与参数（稳定序列化）、工具结果；空片段不贡献 token。
- 明确未做：阈值决策、压缩、切点。

验收：专项 `uv run pytest tests/context/test_tokens.py -q` → 10 passed；全量 `318 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4e：基础安全切点（已完成）

提交：本任务提交（`feat: 增加基础安全切点`）。

交付物：

- 新增 `context/compaction.py`：`find_cut_point(messages, keep_recent_tokens=...)` → `CutPoint(start_index, boundary, kept_tokens) | None`。
- 消息先切成原子段（assistant tool_calls 与紧随 tool results 永不拆散），从最新往前累计 token 到预算边界。
- 优先 user 边界；该 user turn 自身已超预算时才允许从完整 assistant 边界切。全部在预算内、空历史或切点落到 0 时返回 None，不强行截断。
- 非 user/assistant 起始段（如 system）不作为切点。`keep_recent_tokens <= 0` 直接报错。
- 明确未做：split-turn 特例（单条超长 turn 内部切分）、阈值决策、摘要生成。

验收：专项 `uv run pytest tests/context/test_cut_points.py -q` → 10 passed；全量 `328 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4f：工具轮 Split-Turn 切点（已完成）

提交：本任务提交（`feat: 支持工具轮 split-turn 切点`）。

交付物：

- `find_cut_point` 扩展原子段模型：assistant 多工具调用消费紧随的 tool results 并校验 call id 覆盖，多工具作为不可分割 batch。
- 完整工具轮可作为超预算 turn 内的 split-turn 切点；结果缺失的工具轮不可作为切点，只能连同所在 turn 整体保留或整体进入摘要。
- 单条超长 tool result 不拆分：切点回到该工具轮起点，整批保留。
- 明确未做：摘要模型调用、阈值决策。

验收：专项 `uv run pytest tests/context/test_cut_points_tools.py -q` → 6 passed；M7.4e 既有切点测试仍全绿；全量 `334 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4g：Context Window 配置与阈值策略（已完成）

提交：本任务提交（`feat: 增加上下文窗口与阈值策略`）。

交付物：

- 新增 `context/policy.py`：`ContextPolicy(context_window, reserve_tokens)`（含 `threshold_tokens`），窗口/reserve 非法直接报错。
- `KNOWN_CONTEXT_WINDOWS` 内置显式表（deepseek-flash / deepseek-v4-pro / gpt-6-astra / gpt-5.6-sol|terra|luna，来源 2026-09 官方文档，均为 1M 级）；`resolve_policy()` 用户显式窗口优先，未知模型返回 None（关闭自动压缩）。
- `evaluate_compaction(estimate, policy=...)` 纯函数返回 `CompactionDecision(status ∈ {not_needed, needed, unknown}, reason, source)`；严格大于阈值才触发，决策携带估算来源。
- 明确未做：自动调用 compact、CLI 接线、启动信息渲染。

验收：专项 `uv run pytest tests/context/test_policy.py -q` → 14 passed；全量 `348 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

#### M7.4h：Resume 使用统一 Context 投影（已完成）

提交：本任务提交（`feat: Resume 使用统一 Context 投影`）。

交付物：

- `JsonlSession.replay()` 改为 M7.4a-c 统一投影：有 compaction 走 M7.4c（system 快照 + 摘要 + 保留消息），否则走 M7.4b；移除 M7.3c 对 compaction 的临时拒绝。
- 元数据（stepCount / modifiedFiles / provider / model）仍按活动路径回放，摘要不回滚历史累计值。
- `latest_session_path()` 的候选校验随之接受压缩会话，不再误判损坏。
- 明确未做：创建新 compaction entry、自动触发。

验收：专项 `uv run pytest tests/session/test_runtime_resume.py tests/context -q` → 98 passed；全量 `356 passed, 3 deselected`；compileall 与 `git diff --check` 通过。

M7.4 总验收：`tests/context/test_m7_4_projection_pipeline.py` 表驱动覆盖无 usage、tail 追加估算、连续工具调用、单 turn 超长、全部在预算内、重复 compaction，共 6 场景通过。

### M7.C CLI 与对话健康度（M7.4 后，M7.5 前）

依据用户提供的 `mini-pi-cli-optimization-plan.md` 安排；该文件是需求素材，以下边界以本计划和现有协议为准。M7.C1-C5 按顺序完成，不改动 M7.5 / M7.6 的既有任务编号。每个任务分别做针对性离线验证、全量回归和中文提交；完成后按本文件与 README 同步状态。

#### M7.C1：隐藏 raw thinking，显示思考状态

- [ ] `ConsoleRenderer` 默认不打印 `MessageDeltaEvent(kind="thinking")` 的内容；普通 `text` 继续流式展示，错误与最终答复可见。禁止按英文短语过滤正文。
- [ ] 交互式 tty 在思考阶段显示用户指定的小牛图标；图案作为 `mini_pi/assets/thinking.txt` 资源，启动仍使用现有 `banner.txt`。图标只代表运行状态，不包含或暗示模型思考内容。
- [ ] 思考、正文、工具执行与终止事件切换时原地刷新/清理状态，避免重复刷屏；窄终端、非 tty、`--no-banner` 下提供简洁文本状态或静默降级。图标及 spinner 不进入 Session 或模型上下文。
- [ ] 使用 FakeLLM 事件与 Rich 捕获验证：thinking 增量不泄漏，正文不丢，状态结束后无残留；DeepSeek `reasoning_content` 的既有回放测试保持通过。

参考图案（资源文件保留等宽布局；终端可按宽度降级）：

```text
      db         db
    d88           88
   888            888
  d88             888b
  888             d88P
  Y888b  /``````\8888
,----Y888        Y88P`````\
|        ,'`\_/``\ |,,    |
 \,,,,-| | o | o / |  ```'
       |  """ """  |
      /             \
     |               \
     |  ,,,,----'''```|
     |``   @    @     |
      \,,    ___    ,,/
        \__|   |__/
            | | |
            \_|_/
```

#### M7.C2：展示元数据与模型消息隔离

- [ ] 审计 CLI 渲染、Session 提交、Provider wire 三条路径；用测试确认 banner、状态图、token 文案、session 路径、`ToolResult.details` 不进入模型消息或 JSONL message。
- [ ] 将当前插在 `MessageEndEvent` 后的 `tokens: in ... / out ...` 移到任务结束摘要或 `/status`；只标记实际 usage，不把多轮累计误写成“本轮新增上下文”。
- [ ] 保持 tool result 的模型 observation、`is_error` 和 `modified_files` 完整语义；输出规模通过现有工具截断与后续 compaction 控制，不在 Renderer 中偷偷缩短模型上下文。

#### M7.C3：`/status` 与 `/context`

- [ ] `/status` 显示 provider/model、简短 cwd、session id（纯内存模式明确标识）、最近一次 run 的工具次数和 context 占用；详细路径仅显式请求时显示。
- [ ] `/context` 按当前投影列出 system / AGENTS、conversation、tool results、summary、total 与 window 使用率；各分类只给估算值，Provider 返回的整体 usage 单独标示，不伪装成分类实测值，也不重复计数。
- [ ] 未配置 context window 时显示未知及自动压缩关闭；压缩前后、resume 后从当前投影重算，不读旧 Session 全量原文冒充当前上下文。
- [ ] `/tools` 列出当前 Registry 的工具名与简述；`/help` 同步命令可用条件，`/reset` 与 `/new` 原有模式约束保持不变。

#### M7.C4：工具事件摘要与 `--verbose`

- [ ] 默认将 tool start/end 渲染成可读的操作、完成/失败摘要；保留非零 exit code、错误与改动文件数量等关键结果，不输出整段 JSON 参数或长日志。
- [ ] `--verbose` 显示完整可见命令/参数和 Tool 层已截断的 stdout/stderr；不得声称获得了进程层已丢弃的原始日志，且不得输出凭据。
- [ ] tty 工具执行时刷新状态，不重复打印思考图；非 tty 输出稳定的一行事件，便于重定向与测试。

#### M7.C5：减少无效探索的软约束

- [ ] 在现有 system prompt 工作规则中加入“每次 observation 后判断是否已有充分证据，足够时直接回答”的短指引；简单只读问题建议少量关键读取，但不把 3-5 次设为硬上限。
- [ ] `max_steps` 继续作为单次 run 的唯一硬步数上限；修复、测试或未知问题可继续搜索，不增加固定 workflow 或新的 Agent 状态机。
- [ ] FakeLLM 验证 prompt 与 `max_steps` 行为，并用真实任务验收对比无效工具调用数；不能仅凭提示词宣称已降低调用量。

M7.C1-C5 验收：默认终端不泄漏 thinking；图标只作状态提示；`/status`、`/context` 和工具摘要可用；上下文投影及 DeepSeek 回放行为保持正确。

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

### M7.D 会话导航与输入体验（M7.6 后）

#### M7.D1：会话列表与恢复入口

- [ ] `/sessions` 列出当前 workspace 的会话 id、最近活动时间、模型和短摘要；摘要只取已有 compaction summary 或有限长度的历史用户任务，不为列表额外请求模型。
- [ ] 提供从列表恢复指定会话的入口；复用 `--resume` / `--continue` 的严格校验，损坏文件、cwd 不符或并列最新时间仍明确报错。
- [ ] 启动页只显示版本、模型、项目名和短 session id；完整存储路径移到 `/status` 的详细视图或明确的诊断输出。保留 `--no-banner` 与非 tty 降级。

#### M7.D2：交互输入与中断

- [ ] 评估并接入 `prompt_toolkit`，支持历史命令、上下键浏览和多行输入；输入内容只在用户提交后进入 Session。
- [ ] 先定义并验证取消语义，再支持 Ctrl+C 取消当前任务、连续两次 Ctrl+C 退出；已提交的工具结果与 JSONL entry 不回滚，不能在工具仍运行时假装已取消。
- [ ] 输入库不可用或非 tty 时保留当前单行 REPL 路径；交互增强不改变 `AgentSession` / `run_loop` 的消息协议。

M7.D 验收：会话列表与恢复可用，输入历史和中断行为有 tty 人工验收记录；离线测试覆盖非 tty 与恢复错误边界。

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

- [ ] 按脚本人工验证 create → 工具调用 → exit → resume → `/compact` → continue → `/new`，以及 thinking 图标、`/status`、`/context`、`/tools`、`/sessions`、`--verbose`、窄终端/非 tty、输入历史与 Ctrl+C。
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
+ CLI 默认隐藏 raw thinking，状态与上下文可观察
+ 会话列表、恢复和交互输入经人工验收
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
| 隐藏 thinking 破坏 DeepSeek 回放 | 只改 CLI 渲染；Provider wire 与 resume 回归单测 |
| CLI 统计与实际请求不一致 | 按当前投影分组，标记估算与实际 usage；未知窗口不报百分比 |
| 中断导致工具与 Session 分叉 | 先定义取消边界，保留已提交消息；真实 tty 与进程组场景验收 |
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
  ↓ 已完成；先处理用户可见的 CLI 与上下文边界
M7.C1 → C2 → C3 → C4 → C5
隐藏思考/状态图   展示隔离   状态/上下文   工具摘要   停止软约束
  ↓
M7.5a → 5b → 5c → 5d → 5e → 5f
序列化   摘要调用   压缩计划   生成结果   事务提交   手动命令
  ↓ 先把手动事务做对
M7.6a → 6b → 6c → 6d → 6e
Turn Hook   Prompt 前检查   工具轮检查   错误语义   离线端到端
  ↓ 再扩展会话导航与终端输入
M7.D1 → D2
会话列表/恢复   历史输入/中断
  ↓
M7.7a → 7b → 7c → 7d
离线回归   真实模型   人工 CLI   文档收尾
```

当前唯一允许开始的下一任务是 `M7.C1`。不要把相邻编号合并成一次改动；先证明当前编号的行为与不变量，再进入下一编号。
