# Pi 生产架构参考

> 用途：作为 mini-pi M7 及后续阶段的源码级设计参考，不是 mini-pi 的实施规范。具体取舍以 `docs/plans/phase2-session-context.md` 为准。

## 0. 基线与阅读范围

本文依据本地源码：

```text
仓库：/Users/yzh666/workspace/pi
版本：packages/coding-agent 0.86.0
commit：d1230ea2000d
日期：2026-09-20
```

本地 `main` 比远端少 6 个提交，因此本文描述的是已检查的本地代码，不声称代表远端最新实现。定位优先使用“文件 + 符号名”，不要依赖易漂移的行号。

### 生产路径

```text
packages/ai/src
packages/agent/src/agent.ts
packages/agent/src/agent-loop.ts
packages/agent/src/types.ts
packages/coding-agent/src/core/agent-session.ts
packages/coding-agent/src/core/session-manager.ts
packages/coding-agent/src/core/system-prompt.ts
packages/coding-agent/src/core/resource-loader.ts
packages/coding-agent/src/core/compaction/
packages/coding-agent/src/core/tools/
packages/coding-agent/src/core/sdk.ts
packages/coding-agent/src/main.ts
```

### 不要混入生产路径的代码

`packages/agent/src/harness/` 是另一套面向 durable runtime、lane、effect gate、reducer 的新运行时。它有自己的 Session、Context 和 Compaction，目前不是主 CLI 的 `AgentSession` 生产链路。

阅读或移植行为时：

- 当前 CLI 行为看 `Agent + AgentSession + SessionManager`。
- `packages/agent/src/harness/` 只作为远期设计研究材料。
- 两处同名 compaction 实现不能混用；生产实现位于 `packages/coding-agent/src/core/compaction/`。

---

## 1. Package 职责边界

| Package | 核心职责 | 不负责 |
| --- | --- | --- |
| `packages/ai` | 消息协议、Model、Provider stream、token usage、system/tool transcript replay、provider 兼容 | Agent Loop、工具执行、Session |
| `packages/agent` | 无状态 Loop、带状态 Agent、工具调度、事件协议、steering/follow-up 队列 | Coding 工具语义、JSONL、CLI |
| `packages/coding-agent` | AgentSession、SessionManager、内置工具、Prompt、Compaction、资源加载、CLI/SDK 装配 | Provider wire 协议 |
| `packages/tui` | 输入与渲染 | Agent 决策与持久化 |

依赖主干：

```text
CLI / SDK
  ↓
AgentSession                       packages/coding-agent
  ├── Agent                       packages/agent
  │    └── runAgentLoop
  │         ├── StreamFn          packages/ai provider runtime
  │         └── AgentTool
  ├── SessionManager              packages/coding-agent
  ├── ResourceLoader
  └── Compaction
```

最重要的边界是：

- `packages/ai` 不知道 Session。
- `runAgentLoop` 不知道 JSONL、AGENTS.md、CompactionEntry。
- `AgentSession` 负责把运行时事件、资源、Session 和 Context 串起来。
- UI 订阅事件，不参与决策和持久化。

---

## 2. 运行时装配

入口参考：

```text
packages/coding-agent/src/main.ts
  createSessionManager
  createRuntime

packages/coding-agent/src/core/sdk.ts
  createAgentSession
```

`createAgentSession()` 的主要顺序：

1. 解析 cwd、agentDir、SettingsManager。
2. 创建或接收 `SessionManager`。
3. `ResourceLoader.reload()`，收集 AGENTS.md、skills、prompt、扩展、主题。
4. `sessionManager.buildSessionContext()`，判断是否存在可恢复历史。
5. 从历史恢复 model / thinking level；失败再回退到配置默认值。
6. 确定默认活动工具：`read / bash / edit / write`。
7. 构造 `Agent`：注入 `streamFn`、`convertToLlm`、`transformContext`、队列模式等。
8. 将恢复后的 messages 放入 `agent.state.messages`。
9. 构造 `AgentSession`，安装事件处理、Prompt、Compaction 和工具装配。

这说明 Pi 没有让 `Agent` 自己打开 Session 文件。恢复、资源加载和模型选择都发生在 composition root / AgentSession 层。

---

## 3. 一次用户输入的完整链路

核心符号：

```text
AgentSession.prompt
Agent.prompt
runAgentLoop
runLoop
streamAssistantResponse
executeToolCalls
Agent.processEvents
AgentSession._handleAgentEvent
```

时序：

```text
用户输入
  ↓
AgentSession.prompt
  ├── 扩展命令 / input hook
  ├── skill / prompt template 展开
  ├── streaming 时进入 steer 或 follow-up 队列
  ├── 校验 model / auth
  ├── 新 prompt 前检查 compaction
  ├── 构造 UserMessage
  └── 计算 prompt/tool diff
  ↓
Agent.prompt(messages)
  ↓
runAgentLoop
  ├── declareToolChanges
  ├── emit agent_start / turn_start
  └── 初始 system/user 消息 emit message_start/message_end
  ↓
runLoop
  ↓
streamAssistantResponse
  ├── transformContext
  ├── convertToLlm
  ├── normalizeContext
  ├── streamFn(model, context)
  └── partial 原地替换，final message_end
  ↓
若存在 tool calls
  ├── prepare
  ├── execute
  ├── finalize
  └── ToolResultMessage 回写
  ↓
turn_end
  ↓
prepareNextTurn
  ├── 按需 compaction
  ├── refresh prompt sections
  └── refresh model / thinking / tools
  ↓
下一轮或 agent_end
```

`Agent.processEvents()` 先更新 AgentState，再顺序 await listeners。`AgentSession` 是其中一个 listener，所以 `message_end` 到达 Session 层时，AgentState 里已经是定稿消息。

---

## 4. Agent Core

### 4.1 AgentState

定义：`packages/agent/src/types.ts::AgentState`。

主要状态：

```text
systemPrompt       从 transcript system messages 重放得到，只读
model              下一轮使用的模型
thinkingLevel      下一轮 reasoning 级别
tools              实际可执行工具
messages           当前运行投影
isStreaming        当前是否有活动 run
streamingMessage   当前 partial assistant
pendingToolCalls   正在执行的 tool call id
errorMessage       最近一轮错误
```

Pi 区分两种“工具状态”：

- `AgentState.tools`：运行时真正能执行的工具对象。
- transcript 中 `toolsAdded/toolsRemoved`：模型看到的工具声明历史。

每次请求前必须把二者对齐，不能假设它们天然一致。

### 4.2 Agent 是有状态包装，Loop 是无状态驱动

`Agent` 负责：

- 保存 state。
- 管理 active run、AbortController 和 idle 生命周期。
- 暴露 prompt / continue / reset / steer / followUp。
- 把 loop event reduce 进 state。
- 顺序等待订阅者。

`runAgentLoop` 负责：

- 使用传入的 context snapshot 驱动一次运行。
- 不打开文件，不加载配置，不持久化。
- 只通过 hooks 和 events 与上层交互。

### 4.3 Agent 的 settle 语义

`agent_end` 表示 Loop 不再产生新事件，但 Agent 还没有立即 idle。所有 `agent_end` listener 都 await 完成后，`finishRun()` 才清掉 active run，并让 `waitForIdle()` resolve。

这保证 Session 落盘、扩展回调和 UI 收尾都属于一次 run 的完成条件。

---

## 5. 双层 Agent Loop

核心：`packages/agent/src/agent-loop.ts::runLoop`。

伪代码：

```text
pending = poll steering

while true:                              # 外层：follow-up
    has_more_tool_calls = true

    while has_more_tool_calls or pending: # 内层：tool + steering
        if previous_turn:
            snapshot = prepareNextTurn(previous_turn)
            替换 context/model/thinking
            再 poll 一次 steering
            emit turn_start

        把 prepared + pending 消息提交到 transcript
        assistant = streamAssistantResponse()

        if error/aborted:
            emit turn_end / agent_end
            return

        if tool_calls:
            length stop → 全部转 error result，不执行
            否则 executeToolCalls

        emit turn_end
        shouldStopAfterTurn? → agent_end
        pending = poll steering

    followups = poll follow-up
    if followups: pending = followups; continue
    break

emit agent_end
```

两个队列的区别：

- steering：在当前 assistant + tools 完成后、下一次 assistant 请求前注入。
- follow-up：Agent 原本准备结束时才注入。

外层循环的存在是为了覆盖“模型已经没有 tool call，但用户此刻排入 follow-up”的情况。

### 5.1 prepareNextTurn 是统一 turn 边界

`AgentLoopConfig.prepareNextTurn` 可以同时替换：

```text
context
messages
model
thinkingLevel
```

Pi 把 Compaction、Prompt refresh、动态工具和模型切换都挂在这里。Loop 不需要出现：

```text
if should_compact ...
if prompt_changed ...
if tools_changed ...
```

mini-pi M7 最应该借鉴的是这个边界，而不是双队列本身。

---

## 6. 流式消息与事件

### 6.1 Provider 事件

`packages/ai/src/types.ts::AssistantMessageEvent`：

```text
start
text_start / text_delta / text_end
thinking_start / thinking_delta / thinking_end
toolcall_start / toolcall_delta / toolcall_end
done
error
```

错误不以异常替代终止事件；最终会形成 `stopReason=error|aborted` 的 AssistantMessage。

### 6.2 Agent 事件

`packages/agent/src/types.ts::AgentEvent`：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / tool_execution_update / tool_execution_end
```

一次普通工具轮：

```text
turn_start
  message_start(user)
  message_end(user)
  message_start(assistant partial)
  message_update × N
  message_end(assistant final)
  tool_execution_start
  tool_execution_update × N
  tool_execution_end
  message_start(toolResult)
  message_end(toolResult)
turn_end
```

### 6.3 partial 状态

`streamAssistantResponse()` 在 `start` 时把 partial push 到 `context.messages`；delta 到来时替换数组最后一项；done/error 时用 final message 替换。

后果：

- 流式期间 messages 最后一项可能是临时值。
- 持久化只能绑定 `message_end`。
- UI 可通过 `message_update` 渲染，不需要另一套 buffer。

### 6.4 并行工具的两种顺序

Pi 的并行模式：

- `tool_execution_end` 按实际完成顺序 emit，让 UI 尽快更新。
- ToolResultMessage 按 assistant 原始 tool call 顺序写入 transcript。

后者保证 prompt history 稳定，避免调度时序改变上下文和 prompt cache 前缀。

---

## 7. Tool 架构

### 7.1 四层抽象

```text
Tool                         packages/ai
  name / description / parameters
        ↓ extends
AgentTool                    packages/agent
  label / prepareArguments / execute / executionMode / replay
        ↓ coding 层定义
ToolDefinition               packages/coding-agent
  promptSnippet / guidelines / renderer / shell renderer
        ↓ wrapToolDefinition
AgentTool
```

核心 Loop 只依赖 AgentTool；coding-agent 的 Prompt/UI 元信息不会污染 Loop。

mini-pi 第一阶段将其压缩成 `Pydantic args + Tool` 是合理的。只有当 M8 动态工具确实需要 prompt snippet、UI renderer 等独立生命周期时，再考虑增加 definition 层。

### 7.2 prepare / execute / finalize

`executeToolCalls()` 将一次调用拆为三段：

1. `prepareToolCall`
   - 查找工具。
   - `prepareArguments` 做兼容归一化。
   - schema 校验。
   - `beforeToolCall` 可 block。
2. `executePreparedToolCall`
   - 调用 `tool.execute()`。
   - 接收 partial update。
   - 捕获执行错误并转 error result。
3. `finalizeExecutedToolCall`
   - `afterToolCall` 可覆盖 content/details/isError/usage/terminate。

这样可以区分：调用不成立、执行失败、结果后处理失败。

### 7.3 批级别约束

- 全局 `toolExecution` 支持 sequential / parallel。
- 批次中任一工具声明 `executionMode=sequential`，整批退化为串行。
- `stopReason=length` 时整批 tool call 不执行，因为参数可能“合法但不完整”。
- 只有批内每个结果都 `terminate=true` 时才提前终止，避免留下无 ToolResult 的调用。

### 7.4 工具错误策略与 mini-pi 差异

Pi 将 prepare/execute/finalize 的绝大多数错误编码为 error ToolResult，让模型继续纠正。mini-pi 当前选择：

```text
ToolError → observation
其他异常 → 冒泡
```

这是有意差异。M7 不应为了接近 Pi 而改变 mini-pi 的 Fail Fast 分类。

---

## 8. Transcript 是 Prompt 和工具状态的日志

核心：

```text
packages/ai/src/types.ts::SystemMessage
packages/ai/src/utils/transcript.ts
```

SystemMessage 不只是一个开头字符串：

```text
content         追加指令
sections        按 name 替换；null 删除
toolsAdded      增加/替换工具声明
toolsRemoved    删除工具声明
```

顺序重放所有 system message 可以得到某一时刻的完整 prompt 与工具集。

### 8.1 核心 replay 函数

- `getCurrentTools(messages)`：按序应用 toolsRemoved，再应用 toolsAdded。
- `getCurrentSystemMessage(messages)`：合并 content、sections 和当前 tools。
- `getCurrentSystemPrompt(messages)`：渲染当前完整 prompt。
- `collapseSystemMessages(context)`：对不支持中途 system message 的 Provider，折叠成一条 leading system。
- `resolveTranscript(context, supportsMidConvoSystemMessages)`：按 Provider 能力选择保留差量或折叠。

### 8.2 为什么用 transcript delta

- Prompt 变化可重放，可随 Session 恢复。
- Provider 支持时能保持历史前缀，提升 prompt cache 命中。
- 工具定义的变化与当时上下文绑定，不依赖一份外置“当前工具配置”。
- Compaction 可以保存完整 system snapshot，切掉旧 system delta 后仍能重建。

### 8.3 declareToolChanges

`runAgentLoop` 在请求前调用 `declareToolChanges()`：

1. 从已提交 transcript 重放当前工具声明。
2. 将 `context.tools` 转为纯声明，移除 execute/UI 字段。
3. 计算 added / removed / same-name changed。
4. 合并进当前待提交 system message，或插入新的 system patch。

模型所见工具集最终以“实际 executable tools”为准，而不是相信调用方携带的旧 patch。

---

## 9. System Prompt 与项目资源

### 9.1 Prompt sections

`packages/coding-agent/src/core/system-prompt.ts::buildSystemPromptSections` 生成：

```text
preamble
tools
rules
docs
addendum
project_context
skills
cwd
自定义 sections
```

除 preamble 外，各段使用同名 XML 标签包裹。`diffSystemPromptSections()` 返回：

```text
section: new text    新增或替换
section: null        删除
```

`buildRules()` 根据实际活动工具生成规则并去重，因此工具变化不只是 schema 变化，rules/tools 两段也要一起 refresh。

### 9.2 ResourceLoader

`resource-loader.ts::loadProjectContextFiles`：

- 全局 agentDir 可提供一份 context file。
- 从 cwd 向上逐级发现项目 context。
- 每个目录候选优先级：`AGENTS.override.md`、`AGENTS.md`、`AGENTS.MD`、`CLAUDE.md`、`CLAUDE.MD`。
- 最终按祖先 → 当前目录顺序注入。
- 对嵌套 worktree 处理 main repo context shadow，避免同一逻辑 scope 重复加载。

mini-pi M7 只采用 git root → workspace 的 `AGENTS.md`，不复制全局规则、CLAUDE 兼容和 worktree shadow。

### 9.3 每轮 refresh

`AgentSession._installAgentNextTurnRefresh()`：

1. 检查并执行 turn 间 compaction。
2. 调用之前已有的 prepare hook，保持 hook 链。
3. 从当前活动工具重建 prompt options。
4. `_preparePromptAndToolLoadout()` 计算差量 system message。
5. 返回新的 context / messages / model / thinking。

这样模型、Prompt、工具和 Context 在同一个 turn 边界对齐。

---

## 10. AgentSession：生产链路的编排层

`AgentSession` 不是简单的数据类，它承担：

- 用户输入预处理。
- Prompt/Skill/Extension 展开。
- Agent 事件订阅与 Session 落盘。
- ToolDefinition → AgentTool 装配。
- model / thinking / active tools 管理。
- Compaction 触发与状态替换。
- retry / overflow recovery。
- steering / follow-up 队列的 UI 状态。

这也是 Pi 最重的类之一。mini-pi 不应整体照搬，而应只提取 M7 所需的生命周期编排。

### 10.1 prompt preflight

`AgentSession.prompt()` 在 Agent 开始前：

- compaction 正在运行时拒绝新 prompt。
- streaming 中必须显式指定 steer 或 followUp。
- 校验 model 与 auth。
- 检查上一次 assistant 是否需要 compaction。
- 构造 user/custom/system patch messages。
- 交给 `_runAgentPrompt()`。

### 10.2 事件收敛与落盘

`AgentSession._handleAgentEvent()` 顺序：

1. 处理 queue 显示状态。
2. 发扩展事件。
3. 通知 AgentSession 外部 listeners。
4. `message_end` 按 role 落盘。
5. assistant message 更新 retry/overflow 状态。
6. `turn_end` flush 不可插入 tool call/result 中间的 custom messages。

持久化绑定 message_end，而不是 agent_end。进程在长任务中崩溃时，已经定稿的消息仍在 JSONL。

### 10.3 mini-pi 应保持更小

M7 的 AgentSession 只需：

```text
create / resume / run / new / compact
message commit
prompt refresh
context projection
```

不要同步引入 extension、skills、queue、retry UI、branch navigation。

---

## 11. SessionManager 与 JSONL entry 树

核心：`packages/coding-agent/src/core/session-manager.ts`。

### 11.1 文件结构

第一行：

```json
{"type":"session","version":3,"id":"...","timestamp":"...","cwd":"..."}
```

后续 entry 公共字段：

```text
type
id
parentId
timestamp
```

Pi 0.86.0 的主要 entry：

```text
message
thinking_level_change
model_change
usage
compaction
branch_summary
custom
custom_message
label
session_info
```

### 11.2 追加与 leaf

`_appendEntry()`：

```text
fileEntries.push(entry)
byId.set(entry.id, entry)
leafId = entry.id
_persist(entry)
```

普通 append 总是成为当前 leaf 的 child。`branch(id)` 只移动 leaf；下一次 append 自然形成新分支，不修改旧 entry。

### 11.3 延迟创建文件

Pi 在第一个 assistant 出现前不会真正 flush 新 session 文件。这样只打开 CLI、输入被取消或没有模型回复时不会留下空会话。第一次落盘用独占创建并写入当前全部 entry，之后逐行 append。

mini-pi M7 当前计划采用每条消息 durable-first，是更严格但 I/O 更多的取舍。实现时不要误以为这是 Pi 原样行为。

### 11.4 Context 投影三件套

```text
buildSessionPath
buildContextEntries
sessionEntryToContextMessages
```

流程：

1. 从 leaf 沿 parentId 回到 root，再 reverse。
2. 沿路径恢复最后 model / thinking。
3. 找最后一条 compaction。
4. 构造 compaction-aware entries。
5. 将参与上下文的 entry 投成 AgentMessage；label/usage/custom state 等返回空数组。

这说明 JSONL 不是“模型下一次直接读取的 messages 数组”，而是可投影的事实记录。

### 11.5 Compaction 投影

若当前路径存在 compaction：

```text
[latest compaction]
+ [compaction 之前从 firstKeptEntryId 开始的非 system entry]
+ [compaction 之后的 entry]
```

CompactionEntry 投影为：

```text
systemMessage snapshot（若有）
compactionSummary
```

旧 system message 被过滤，避免在 snapshot 后重复回放。

### 11.6 版本迁移

- v1 → v2：线性历史补 id/parentId，compaction index 改为 entry id。
- v2 → v3：`hookMessage` role 政名为 `custom`。

mini-pi 从 version=1 开始即可，不需要预写不存在的 migration framework；等格式真的升级再加入迁移。

### 11.7 宽松解析与 mini-pi 差异

Pi 的 `parseSessionEntries()` 会跳过非法 JSON 行，兼容手工编辑或局部损坏。mini-pi 的项目约束偏好 Fail Fast，因此 M7 计划是发现坏行立即报错。开发时要明确这是有意分叉，不要无意复制 Pi 的宽松解析。

---

## 12. Compaction

生产实现：`packages/coding-agent/src/core/compaction/compaction.ts`。

### 12.1 默认参数

```text
enabled = true
reserveTokens = 16384
keepRecentTokens = 20000
```

触发：

```text
contextTokens > contextWindow - reserveTokens
```

这些数值是 Pi 默认值，不自动成为 mini-pi 默认值。mini-pi 需要结合 OpenAI/DeepSeek 模型配置决定。

### 12.2 Token 估算

`estimateContextTokens()`：

1. 找最后一个有效 assistant usage。
2. 以 usage totalTokens 作为已知前缀成本。
3. 对其后的消息按字符估算。
4. 没有 usage 时全部按字符估算。

error、aborted、全零 usage 不作为可信基线。

### 12.3 安全切点

`findCutPoint()` 从最新 entry 向前累计 `keepRecentTokens`：

- user / assistant / custom / summary 等可作为切点。
- toolResult 不能作为切点。
- 优先完整 turn。
- 单 turn 太大时允许从 assistant 开始保留，并单独摘要该 turn 的前缀。

允许从 assistant 切是为了处理一个超长 Agent turn；保留 assistant 后面的 tool results，就不会拆散 tool call/result。

### 12.4 prepareCompaction

它只准备数据，不调用模型：

```text
firstKeptEntryId
messagesToSummarize
turnPrefixMessages
isSplitTurn
tokensBefore
previousSummary
fileOps
settings
```

重复压缩时从上一条 compaction 的 `firstKeptEntryId` 开始，带入 `previousSummary` 做增量更新，而不是从 session 开头重新总结。

### 12.5 摘要请求

摘要不是把旧消息原样作为多轮对话发给模型：

1. `convertToLlm()` 先处理 custom roles。
2. `serializeConversation()` 序列化成 `[User] / [Assistant] / [Tool result]` 文本。
3. tool result 每条最多保留 2000 字符。
4. 用独立 summarization system prompt 强调“只总结，不继续对话”。
5. 不提供 tools；若模型仍返回 tool call，视为失败。
6. error 或 length stop 都不能持久化为有效摘要。

固定摘要结构包含 Goal、Constraints、Progress、Key Decisions、Next Steps、Critical Context。

### 12.6 split turn

若一个 turn 本身超过保留预算：

- 历史完整 turn 生成 history summary。
- 当前超长 turn 的早期部分生成 turn-prefix summary。
- 两份摘要合并。
- 最近 assistant/tool suffix 原样保留。

这比强行只在 user 边界切更稳，因为后者可能无法释放足够上下文。

### 12.7 文件操作累计

Pi 从 tool call、旧 compaction details、branch summary 中累计：

```text
readFiles
modifiedFiles
```

它们既进入结构化 details，也追加到摘要文本，保证多次压缩后仍知道读写过哪些文件。

### 12.8 提交压缩结果

`AgentSession._runAutoCompaction()`：

```text
prepare
  ↓
LLM summary
  ↓
appendCompaction
  ↓
sessionManager.buildSessionContext
  ↓
agent.state.messages = rebuilt messages
```

原始 messages 仍在 JSONL；改变的是 AgentState 的运行投影。

### 12.9 两类触发位置

1. turn 内：`_compactBeforeNextAssistantResponse()`，工具结果完成后、下一次请求前。
2. run 间/异常后：`_checkCompaction()`，处理阈值、context overflow 和 recoverable length。

overflow recovery 最多 compact-and-retry 一次，防止无限恢复循环。失败/truncated assistant 虽然已经落盘，可以从 AgentState 临时移除后 compact，再 continue；事实历史与本次重试投影因此可以不同。

---

## 13. LLM / Provider 层

### 13.1 StreamFn 契约

`packages/agent/src/types.ts::StreamFn` 接收：

```text
model
normalized TranscriptContext
SimpleStreamOptions
```

Provider/请求失败应编码进 AssistantMessageEventStream，而不是用 rejected promise 代替协议结束。

### 13.2 统一消息到 Provider 消息

请求前固定链路：

```text
AgentMessage[]
  ↓ transformContext
AgentMessage[]
  ↓ convertToLlm
Message[]
  ↓ normalizeContext
TranscriptContext
  ↓ provider
```

自定义消息在 `convertToLlm` 被转换或过滤。Provider 不需要理解 coding-agent 的 custom role。

### 13.3 Provider 能力差异

不同 Provider 对以下能力不同：

- 中途 system message。
- tool addition/removal。
- reasoning/thinking 回放。
- tool result 格式。
- prompt cache。

这些差异由 `packages/ai` 的 transcript replay 和各 provider adapter 处理，Agent Loop 不写 provider if/else。

mini-pi 只支持 OpenAI / DeepSeek，因此无需复制 Pi 的完整 provider compatibility surface，但应保持“差异只在 LLM Client”这一方向。

---

## 14. 错误、Abort 与恢复

### 14.1 LLM 错误

- 正常协议终点是 done 或 error event。
- Agent 会产生 terminal AssistantMessage，保存 errorMessage。
- `runWithLifecycle()` 捕获 Loop 的意外异常时，也补齐 message_start/message_end/turn_end/agent_end，保持事件序列完整。

### 14.2 Tool 错误

- 工具不存在、参数错误、策略 block、execute throw、after hook throw 最终都能形成 error ToolResultMessage。
- AbortSignal 贯穿 provider 与工具。
- `pendingToolCalls` 由 tool execution start/end 维护。

### 14.3 Context overflow

- 判断失败消息是否与当前 model 相同，避免模型切换后误触发。
- 防止旧 compaction 前的 usage 再次触发。
- recovery 只尝试一次。
- compact 失败发明确事件，不静默删历史。

### 14.4 持久化边界

- Assistant partial 不落盘。
- message_end 才落盘。
- Compaction/BranchSummary 用独立 entry API，不能伪装成普通 message。
- custom context message 等到 turn_end 插入，避免落在 tool call/result 中间。

---

## 15. 对 mini-pi 的映射

### 15.1 M7.1 JSONL

参考：

```text
SessionHeader / SessionEntryBase
SessionManager._appendEntry
SessionManager.getBranch
buildSessionPath
```

采用：append-only、id/parentId、leaf、Context 投影。

不采用：全部 entry 类型、分支 UI、宽松坏行解析、预置 migration 框架。

### 15.2 M7.2 Prompt / AGENTS.md

参考：

```text
buildSystemPromptSections
diffSystemPromptSections
getCurrentSystemMessage
collapseSystemMessages
loadProjectContextFiles
```

采用：named sections、null 删除、replay 后 Provider 投影。

不采用：skills、custom sections、CLAUDE 兼容、global agentDir、worktree shadow。

### 15.3 M7.3 Resume

参考：

```text
createAgentSession
SessionManager.open / continueRecent
AgentSession._handleAgentEvent
Agent.processEvents
```

采用：Session 在 Agent 之外恢复 state；完整消息级持久化。

有意不同：mini-pi 计划 durable-first；Pi 是 AgentState 先更新，AgentSession listener 再 append。

### 15.4 M7.4 Token / 切点

参考：

```text
estimateContextTokens
findCutPoint
prepareCompaction
```

采用：usage 优先、字符兜底、tool pairing invariant、split turn。

### 15.5 M7.5 手动压缩

参考：

```text
serializeConversation
generateSummaryWithUsage
compact
appendCompaction
buildSessionContext
```

采用：固定摘要 schema、独立无工具调用、成功后才 append、原始 entry 不删除。

### 15.6 M7.6 自动压缩

参考：

```text
prepareNextTurn
AgentSession._compactBeforeNextAssistantResponse
AgentSession._checkCompaction
AgentSession._runAutoCompaction
```

采用：turn 边界 hook、阈值压缩、重建投影。

第一版不采用：overflow 自动 retry、扩展拦截、队列续跑；先把阈值路径做稳。

---

## 16. 采用 / 延后 / 不采用

| Pi 设计 | mini-pi 决策 | 原因 |
| --- | --- | --- |
| Loop 与 AgentSession 分层 | M7 采用 | 防止 Session/Context 污染 Agent Core |
| prepareNextTurn hook | M7 采用 | turn 边界唯一、安全 |
| JSONL entry tree | M7 采用最小子集 | 为 resume/compaction 提供事实源 |
| Prompt sections/diff | M7 采用 | AGENTS/tool 变化可重放 |
| CompactionEntry 不删原文 | M7 采用 | 可逆、可验证 |
| usage + trailing estimate | M7 采用 | 比全量字符估算可靠 |
| split turn | M7 纳入设计 | 避免单个超长 turn 无法压缩 |
| message_end 持久化 | 采用语义，提交顺序不同 | mini-pi 保持 Fail Fast/durable-first |
| steering/follow-up 双队列 | 延后 | 当前同步 CLI 无真实需求 |
| parallel tools | 延后 | Phase 1 明确串行 |
| ToolDefinition 四层 | 延后 | 当前 Tool 单层足够 |
| branch/tree/summary | 延后 | M7 只需线性 leaf |
| extensions/skills/templates | 延后 | 非 MVP 后首要问题 |
| provider 大兼容层 | 不采用 | 只支持 OpenAI/DeepSeek |
| 宽松跳过损坏 JSONL | 不采用 | mini-pi 要求 Fail Fast |
| durable harness/lane/effect gate | 不采用当前 M7 | 非生产主线且复杂度过高 |

---

## 17. 推荐源码阅读顺序

开发 M7 时按以下顺序定点阅读：

1. `packages/agent/src/types.ts`
   - `AgentState`
   - `AgentLoopConfig`
   - `AgentEvent`
2. `packages/agent/src/agent-loop.ts`
   - `runAgentLoop`
   - `runLoop`
   - `streamAssistantResponse`
   - `executeToolCalls`
3. `packages/agent/src/agent.ts`
   - `Agent.prompt`
   - `createLoopConfig`
   - `processEvents`
4. `packages/ai/src/types.ts` 与 `utils/transcript.ts`
   - `SystemMessage`
   - `getCurrentSystemMessage`
   - `getCurrentTools`
5. `packages/coding-agent/src/core/session-manager.ts`
   - entry types
   - `getBranch`
   - `buildContextEntries`
   - `buildSessionContext`
   - `appendMessage`
   - `appendCompaction`
6. `packages/coding-agent/src/core/system-prompt.ts`
   - `buildSystemPromptSections`
   - `diffSystemPromptSections`
7. `packages/coding-agent/src/core/resource-loader.ts`
   - `loadProjectContextFiles`
8. `packages/coding-agent/src/core/compaction/compaction.ts`
   - `estimateContextTokens`
   - `findCutPoint`
   - `prepareCompaction`
   - `compact`
9. `packages/coding-agent/src/core/agent-session.ts`
   - `_handleAgentEvent`
   - `_installAgentNextTurnRefresh`
   - `prompt`
   - `compact`
   - `_checkCompaction`
   - `_runAutoCompaction`
10. `packages/coding-agent/src/core/sdk.ts::createAgentSession`

先理解数据如何流动，再看 TUI、扩展和远程协议。否则容易把表现层或实验运行时误当成 Agent Core。

---

## 18. 开发时的核对问题

每实现一个 M7 子里程碑，至少回答：

1. JSONL 记录的是完整事实，还是被压缩后的投影？
2. 进程在 assistant/tool message 定稿后立即崩溃，resume 能否看到它？
3. Prompt/工具变化是否能仅凭 transcript 重放？
4. Context 投影是否可能把 tool result 与 tool call 拆开？
5. Compaction 失败时是否完全不改变 Session 和 AgentState？
6. Compaction 后是否可能再次执行已经完成的工具？
7. 当前实现是否把 Pi 的队列、扩展或分支复杂度提前带入了 mini-pi？
8. Provider 差异是否仍只存在于 LLM 层？

其中任何一项答案不明确，都不应进入下一个子里程碑。
