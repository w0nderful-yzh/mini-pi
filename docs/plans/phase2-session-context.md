# Phase 2: Session / Context 设计与实施计划

> 状态：设计完成，尚未开始实现。

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
- 内容包在带绝对来源路径的 `<project_instructions>` 中，便于模型区分层级。
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

### M7.1 Session Schema 与 JSONL 基础

- [ ] 新增 `session/models.py`：严格判别联合与 version=1 header。
- [ ] 新增 `session/jsonl.py`：create/load/append、leaf 推进、路径发现。
- [ ] 校验 parent 链、重复 id、错误 header、未知版本、损坏/半行 JSON。
- [ ] 测试 fsync append 后可由新进程对象完整恢复。

验收：纯 Session 测试不依赖 Agent 或网络；合法文件 round-trip 相等，所有损坏样例明确失败。

### M7.2 Project Context 与 Prompt Sections

- [ ] 新增 `context/project.py`，实现 git root → workspace 的 `AGENTS.md` 加载顺序。
- [ ] 将 `agent/prompt.py` 迁移为 sections 构建，补充 diff / replay / render。
- [ ] 扩展 `SystemMessage`，OpenAI / DeepSeek wire 层只接收折叠后的完整 prompt。
- [ ] 覆盖父子规则、内容变化、section 删除、legacy content 与非法 patch。

验收：同一段历史重放后 Provider 得到唯一、确定的 system prompt；项目规则来源路径可见。

### M7.3 AgentSession、持久化与 Resume

- [ ] 新增消息提交 hook，保证每条完整消息追加式落盘。
- [ ] 新增 `session/runtime.py::AgentSession`，负责 create/resume/run/new。
- [ ] resume 从 entry path 重建 messages、step_count、modified_files 与最后使用的 provider / model。
- [ ] CLI 增加 `--resume <path>`、`--continue`、`--no-session` 与 `/new`。
- [ ] resume 时 cwd 不存在或显式 cwd 冲突直接报错。
- [ ] `/connect` 替换 LLM 时保留同一 Session，不把 API Key 写入 Session。

验收：运行一轮 → 退出进程 → resume → 继续提问；第二轮 LLM 收到第一轮历史，JSONL parent 链连续。

### M7.4 Context 投影、Token 与安全切点

- [ ] 新增 `context/projection.py`，实现 parent path 与 latest compaction 投影。
- [ ] 新增 token 估算，优先 usage，字符估算兜底。
- [ ] 实现 user / assistant 安全切点及 tool_call / tool_result 配对约束。
- [ ] 增加未知模型 context window 的显式配置与提示。

验收：表驱动测试覆盖无 usage、尾随消息、连续工具调用、单 turn 超长、找不到安全切点、重复 compaction。

### M7.5 手动 Compaction

- [ ] 新增固定摘要模板与 transcript serializer；单条 tool result 摘要输入限长。
- [ ] 实现 `/compact [instructions]`，摘要调用禁止携带 tools。
- [ ] compaction entry 保存 firstKeptEntryId、tokensBefore、usage、system snapshot 与 modifiedFiles。
- [ ] 摘要失败、空摘要、写盘失败时保持原 Session 与 AgentState 不变。

验收：FakeLLM 生成摘要后，模型上下文缩短，JSONL 原始 message 数量不变，resume 得到相同投影。

### M7.6 自动 Compaction 与 turn hook

- [ ] 为 `run_loop` 增加 `prepare_next_turn`，默认 None 时保持 Phase 1 行为与事件顺序。
- [ ] 在新 prompt 前，以及 tool batch 完成后、下一次 LLM 前检查阈值。
- [ ] 自动压缩成功后在同一次 run 中继续，不重放已执行工具。
- [ ] 压缩失败编码为清晰的 agent error；禁止带着超限 context 盲目重试。

验收：FakeLLM 构造跨阈值工具轮，断言调用序列为 assistant → tool → compact → assistant，工具只执行一次。

### M7.7 回归、真实验收与文档同步

- [ ] `uv run pytest` 全量通过，默认测试无网络。
- [ ] 新增长会话 integration marker，使用真实模型验证自动压缩后能继续完成任务。
- [ ] 人工验证 create → exit → resume → compact → continue。
- [ ] 更新 README 路线图、目录、CLI、已知限制与测试结果。
- [ ] 将本计划已完成任务压缩成“交付物 + 验收 + 提交号”。

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
M7.1 JSONL
  ↓
M7.2 prompt / AGENTS.md
  ↓
M7.3 resume 闭环          ← 首个用户可见增量
  ↓
M7.4 token / 切点
  ↓
M7.5 手动 compact         ← 先把事务做对
  ↓
M7.6 自动 compact         ← 再接 turn hook
  ↓
M7.7 真实验收与文档收尾
```

不要把 M7.3 与 M7.6 合并成一次大改：先证明完整历史可靠持久化与恢复，再让 Context 改写运行投影。
