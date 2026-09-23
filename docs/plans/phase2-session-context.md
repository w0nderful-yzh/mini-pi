# Phase 2：Session、Context 与 CLI 成本控制

> 状态：实施中。M7.1–M7.4、M7.C1–C8、M7.5、M7.6a–e 已完成；下一任务是 **M7.6f：旧 Tool Result 的成本感知压缩**。本文件先列待开发任务，已完成交付放在末尾。

**目标：** 在已有 Coding Agent 闭环上，控制单次任务的重复探索和累计模型输入，完成安全的手动/自动上下文压缩，并让终端清楚展示进度、失败和真实用量。只支持 OpenAI、DeepSeek；不引入 Agent 框架、额外规划模型或并行工具执行。

**依据：** 当前 `mini_pi` 实现与测试、2026-09-22 的真实会话记录、用户提供的 `mini-pi-cli-v2-optimization.md`，以及 [Pi 生产架构参考](../design/pi-production-architecture.md)。外部方案中的示例和阈值是需求素材；与本项目事实源、Provider 协议或实测数据冲突时，以本文件明确的取舍为准。

---

## 1. 当前基线与优先级

M7.1–M7.4 已交付 JSONL Session、项目 `AGENTS.md`、恢复、Context 投影、token 估算和安全切点。M7.C1–C8 已交付 thinking 状态图、展示边界、基础 `/status` `/context` `/tools`、语义化工具事件、软性停止提示、任务累计用量与当前上下文分离，以及默认关闭的请求边界任务预算。**尚未交付**：手动/自动 compaction、会话列表和增强输入。

### 1.1 真实成本样本

2026-09-22 一次“这个项目放到简历上怎么写”的只读任务，Session 活动链记录了：

| 指标 | 实测 | 含义 |
| --- | ---: | --- |
| 模型请求 | 11 次 | 一次用户任务内的多轮 LLM ↔ Tool 循环 |
| Tool 调用 | 19 次 `bash` | 基础工具摘要未让用户看出各次操作意图 |
| Provider 输入 | 157,007 tokens | **11 次请求的 input_tokens 之和**，是累计消耗 |
| Provider 输出 | 5,817 tokens | 11 次请求的 output_tokens 之和 |
| 最后一次请求输入 | 21,021 tokens | 最近一次请求的上下文规模，不能当作整轮成本 |
| 项目规则 / 工具结果 | 23,722 / 32,182 字符 | 大的固定前缀与逐轮增长的 observation |

原始 Session 仍保存在用户目录中，不加入仓库。这个样本说明：本轮反复发送了完整规则及不断增长的历史，且 C5 的软提示没有阻止过度探索。它**不**说明单次请求使用了 157k 上下文，也不能证明每一次工具调用都没有价值。

### 1.2 两种预算分别处理

- **任务累计成本**：本次 `run()` 中各次 Provider 实际 usage 相加；用于成本提示和任务预算。没有 Provider usage 时明确标注不可用或估算，不与实测相加后称作实测。
- **当前上下文占用**：下一次请求将携带的活动投影，按 M7.4 估算；用于模型窗口和 compaction 决策。`/context` 的分类总和只代表这个投影。
- `157k / 1M = 15.7%` 是把累计消耗误当窗口占用。上述样本最后一次输入约 21k，对 1M 窗口约 2.1%；自动压缩即使按 70% 触发，也无法解决这次的主要浪费。
- Provider usage 是请求用量，缓存命中与实际计费不在当前模型协议中；没有对应字段和验证前不显示“节省费用”。

**近期顺序：M7.5 → M7.6 → M7.D → M7.7。** 基准见 [M7.C6 用量记录](../benchmarks/m7-c6-usage-baseline.md)；任务预算与压缩保持独立。UI 小修不冒充成本下降。

---

## 2. 必须保持的设计边界

```text
CLI → AgentSession → Agent → run_loop → (LLM, ToolRegistry) → Tool → Workspace
          ├── JsonlSession：完整、追加式事实
          └── Context：从活动链构建的模型投影
```

- CLI 消费 `AgentEvent` 和只读统计，不决定工具、不拼 JSONL、不把图案、计时、token 文案或 session 路径喂给模型。
- Agent Loop 仍根据 observation 自主选择下一步；成本检查只能在完整 assistant/tool batch 后、下一次模型请求前进行，不写死 `read → edit → test` 或“3–5 次读取”。
- JSONL 保留原始 assistant、tool call、tool result 和 compaction entry。展示摘要不改 `ToolMessage.content`；压缩只切换可重建的模型投影，不删除历史。
- tool call 与 tool result 必须配对。写文件后的 `modified_files`、错误状态和实际 shell exit code 不因摘要或停止而丢失；失败写盘直接停止。
- OpenAI 不回放 `reasoning_content`，DeepSeek 仍按其协议回放。默认终端不展示 `thinking_delta`；不靠英文关键词删除普通 assistant 正文。
- 已知模型的窗口按显式配置解析；未知窗口不猜测百分比或启用自动压缩。M7.4 的 `context_window - reserve_tokens` 仍是**窗口安全阈值**，不是任务成本预算。

### 2.1 Tool Result 生命周期

用户方案提出 `ToolResult(raw, summary)`，本阶段不直接改现有工具协议：当前 `content` 已是有界且真实的模型 observation，`details` 仅供 UI。若立即把旧 `ToolMessage` 改写成短摘要，会让 JSONL、内存和已执行工具的事实不一致，还可能拆散调用与结果。

目标路径：本轮必要的完整、有界 observation → 安全切点 → M7.5 的 `CompactionEntry` 摘要 → 后续请求使用新投影。长日志先由 Tool 层的进程上限及行/字节双限控制。对旧工具结果的提前压缩须在 M7.6f 证明**摘要成本小于预计节省**，不能只因累计输入达到 157k 就额外调用摘要模型。

### 2.2 失败展示语义

`bash` 非零退出码是真实 observation，不自动等于整个 Agent 任务失败；`search` 无匹配也不自动等于程序故障。默认 UI 可显示命令类别、退出码、简短且脱敏的 stderr、超时和截断；只有 Agent 最终事件才能确定任务是否终止。不能仅凭 exit code 自称“non-critical, continuing”或“build passed”。

---

## 3. M7.6 自动 Compaction 与旧工具结果收敛

### M7.6c：工具轮之间自动压缩

- [x] 将 M7.5 事务接入 M7.6a hook，成功后同一次 run 继续；工具不能重放，不做 overflow 自动 retry。

验收：`uv run pytest tests/session/test_auto_compact_tool_turn.py -q`。

### M7.6d：自动压缩失败语义

- [x] 无安全切点、摘要失败或写盘失败时以明确 agent error 结束；保留压缩前有效状态，禁止携超限上下文继续请求。

验收：`uv run pytest tests/session/test_auto_compact_errors.py -q`。

### M7.6e：离线端到端

- [x] FakeLLM 验证 assistant → tool → compact → assistant，工具只执行一次；退出后 resume 投影与内存一致。

验收：`uv run pytest tests/integration/test_auto_compaction.py -q`。

### M7.6f：旧 Tool Result 的成本感知压缩

- [ ] 在已有 M7.5 事务上评估是否提前摘要旧工具输出，不新增 `ToolResult.raw/summary` 双写协议。当前工具轮所需 observation 保持原样；旧历史只在安全切点后进入 summary 投影。
- [ ] 单列摘要模型的 input/output 成本与预计后续请求节省；无法证明净收益、没有安全切点、存在未配对工具或摘要失败时不提前压缩。窗口阈值仍独立有效。
- [ ] 使用长 build/pytest 日志、git diff 和搜索结果样本验证关键信息（失败原因、文件、修改和后续动作）保留，resume 一致。不得把累计 157k 当成 15.7% 窗口占用。

验收：`tests/context/test_tool_result_lifecycle.py`、`tests/session/test_cost_aware_compaction.py`、全量回归和前后成本记录。

自动压缩阈值暂沿用 `estimated_context > context_window - reserve_tokens`。来源方案的 70%/85%/95% 是待测建议；在 1M 窗口、21k 活动上下文的样本上不会触发，不作为本次单任务成本修复的默认配置。

---

## 4. M7.D 交互体验与 M7.7 总验收

### M7.D1：会话列表和启动页

- [ ] `/sessions` 显示当前 workspace 的 id、活动时间、模型和已有摘要；不为列表请求 LLM。恢复入口复用 `--resume` / `--continue` 严格校验。
- [ ] 保持 `/new` 新建持久会话、`/reset` 清空纯内存会话的现有语义；若加入交互式 `/resume`，应复用严格加载逻辑，不把 `/reset` 当成删除历史的别名。
- [ ] 保留 `banner.txt` 与 `thinking.txt` 的 Rich Live 状态；启动页缩为版本、模型、项目名、短 id 和 `/help`。完整 cwd / Session 路径放 `/status full`，窄屏及非 tty 稳定降级。

验收：会话列表、指定恢复、损坏/错 cwd/并列最新时间错误均由离线测试覆盖；列表不会产生模型请求。

### M7.D2：输入与取消

- [ ] 评估 `prompt_toolkit` 的历史、多行、补全、Ctrl+L 和 `/` 提示；输入提交前不写 Session。
- [ ] 先定义取消边界，再实现 Ctrl+C 取消当前任务、连续两次退出。已提交消息/工具改动保留；非 tty 保持可用。中断不伪装成 `completed`。

验收：输入历史、多行、取消及进程组场景有 tty 人工记录；非 tty 回归通过，输入库不可用时仍可使用原单行 REPL。

### M7.7：回归、真实验收与文档同步

- [ ] **M7.7a 离线回归：** 全量 `uv run pytest -q`、compileall、`git diff --check`；验证 `--no-session`、append-only、tool pair、预算停止与 compaction 不变量。
- [ ] **M7.7b 真实模型：** 至少一个已配置 Provider 验证长任务压缩后续跑；缺 Key 时 integration skip，不输出凭据，未测 Provider 明确标注。
- [ ] **M7.7c 人工 CLI：** create → 工具调用 → exit → resume → `/compact` → continue → `/new`，加 `/status`、`/context`、`/sessions`、预算提示、语义化工具事件、窄屏/非 tty、输入历史与 Ctrl+C；只保存脱敏结果。
- [ ] **M7.7d 文档收尾：** README 路线图、CLI `--help`、AGENTS.md 与实现一致；已完成任务只保留交付物、验收、提交号摘要。

M7 完成标准：会话可恢复；项目规则生效；单任务累计成本与当前上下文不混淆，预算可控且中断可续；compaction 保留原始历史、安全配对与修改事实；默认 CLI 清晰展示真实进度和失败。

---

## 5. M8–M10 准入条件

- **M8 LSP / MCP：** M7 验收完成后，LSP 先实现只读 definition/references/symbols/diagnostics，MCP 先做 stdio client；动态工具变更经 prompt section diff 持久化，resume 可重建同一工具集。Adapter 仍经 ToolRegistry 和 Workspace，服务崩溃应转可预期 ToolError。不开发 MCP server、OAuth 或远程 transport。
- **M9 Task / Memory：** 出现真实跨 Session 工作流后，Task 先记录目标、状态、验收及关联 Session；Memory 仅从已完成工作提炼带来源、可核对的事实，默认人工确认后写入。按项目和明确 key 检索，不把 transcript 摘要当事实，不引入 RAG/Vector DB。
- **M10 Multi-Agent：** 单 Agent 的成本、Session、Context 和工具权限稳定后，且有需要隔离上下文的并行任务；子 Agent 独立状态与 Session，父子只交换结构化任务/结果。写入先隔离 workspace/worktree，再用一个 worker 和 reviewer 的确定场景验证，不提前建通用调度器。

---

## 6. 已完成交付与验证

已完成部分只保留可回查的交付物、验收和提交；历史细节由 Git 与测试保存。`本任务提交` 等旧占位记录在这里替换为实际提交号。

| 里程碑 | 交付物 | 验收与提交 |
| --- | --- | --- |
| M7.1 | 严格 JSONL header/entry、durable append、加载与树校验 | Session 专项 19 passed；全量 177 passed, 3 deselected；`fce8686` |
| M7.2a–e | 项目 AGENTS.md 发现、Prompt Sections/patch、Provider 唯一 system、Agent 跨 run 刷新 | M7.2e 全量 219 passed, 3 deselected；`73730b7`、`182a221`、`439bdee`、`4d5c645`、`0b7f12f` |
| M7.3a–g | 消息提交 Hook、AgentSession 创建/恢复、CLI 默认持久化、`--resume`/`--continue`/`/new`/模型切换 | M7.3g 全量 276 passed, 3 deselected；`4afd28b`、`f4e7e87`、`0824695`、`261d84c`、`97d213e`、`c54be2a`、`885ccf5` |
| M7.4a–h | 活动路径与 compaction 投影、tool pair 校验、token 估算、安全切点和窗口策略 | M7.4h 全量 356 passed, 3 deselected；`6926bd0`、`e078196`、`817fc47`、`c191d87`、`86a2839`、`3aab07a`、`0a2a566`、`10eee89` |
| M7.C1–C5 | 隐藏 raw thinking、小牛状态、展示/消息隔离、基础 status/context/tools、简洁工具事件与软提示 | C5 全量 387 passed, 3 deselected；`c8ec231`、`5da4848`、`5ebcc25`、`eb61481`、`1d28651`；真实简单只读 A/B 均为 1 Tool，不声称成本已下降 |
| M7.C6 | 最近任务请求/Tool/Provider usage 从活动链重建，CLI 分开显示累计用量与当前投影；[脱敏基准与 C8 决策](../benchmarks/m7-c6-usage-baseline.md) | 全量 392 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`b4cf0d0`；没有额外真实模型调用，复杂排障样本待补 |
| M7.C7 | CLI 为已知工具/命令生成确定性标题，展示实际退出码、超时、截断和有界 stderr；默认单行，详细模式脱敏，Session/ToolMessage 不变 | `tests/cli/test_tool_events.py` 20 passed；全量 412 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`50ce733` |
| M7.C8 | `--max-run-input-tokens` 显式启用每次任务累计输入预算；请求前预测、一次收敛提示及 `budget_limit` 安全停止保持 Session、tool pair 和文件修改，可在同会话用新预算继续 | 预算专项 10 passed；全量 422 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；真实 B1–B4 未运行，默认继续关闭；`8323300` |
| M7.5a | Transcript 序列化：role、工具名、call id、参数、结果与错误状态确定性输出；单条 tool result 2000 字符头部截断并标记省略量；system 快照不进入摘要输入 | `tests/context/test_serializer.py` 11 passed；全量 433 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`adc5bd1` |
| M7.5b | 固定摘要协议与单次调用器：Goal、Constraints、Progress、Key Decisions、Next Steps、Critical Context、read/modified files 模板；独立 system prompt、`tools=None`；空摘要、`length`、error 状态、意外 tool call 与 LLM error 一律失败不落盘 | `tests/context/test_summarizer.py` 9 passed；全量 442 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`ded341d` |
| M7.5c | 压缩输入 plan：活动投影 + `find_cut_point` 选切点，按对象同一性映射回真实 entry；列出待摘要/保留 entry id、firstKeptEntryId、tokensBefore、system 快照、modifiedFiles 与 `previous_summary`；无安全切点只返回原因 | `tests/context/test_compaction_plan.py` 6 passed；全量 448 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`9efa836` |
| M7.5d | Compaction Result：对 plan 序列化并单次调用生成摘要与 usage；重复压缩改用 UPDATE 模板并把旧摘要放进 `<previous-summary>`，不重发更早原文；split-turn 的工具轮整体留在保留区；仍不写盘、不替换 AgentState | `tests/context/test_compaction_result.py` 5 passed；全量 453 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`f942068` |
| M7.5e | 事务提交：`AgentSession.compact()` 摘要成功后 append `CompactionEntry`（summary/firstKeptEntryId/tokensBefore/systemMessage/usage/modifiedFiles），再从 entry 路径重建并一次性替换 `AgentState.messages`；原始 message entry 不删、工具不重放；摘要/写盘/重建失败都不改 JSONL 与内存 | `tests/session/test_compaction_transaction.py` 6 passed；M7.5 总验收（原始 message 数不变、投影变短、resume 一致）已在用例内验证；全量 459 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`f947671` |
| M7.5f | CLI `/compact [instructions]`：`DEFAULT_KEEP_RECENT_TOKENS=20000` 触发手动压缩；instructions 只进摘要请求不落盘；显示摘要消息数、保留 entry 数、切点边界、压缩前后当前上下文估算与摘要实测用量；失败只报错不改 Session；`--no-session` 明确拒绝且不建 JSONL；不引入自动压缩 | `tests/cli/test_compact_command.py` 4 passed；M7.5 总验收在 CLI 层复核（6 条 entry = 5 message + 1 compaction，重放投影 4 条，原始 message 不删）；全量 463 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`eba468e` |
| M7.6a | `run_loop` 可选 `prepare_next_turn` 钩子：只在完整工具批次提交、turn 收尾后调用，下一次请求重新读取 `state.messages`；`None` 保持 Phase 1 事件行为，截断轮不触发，钩子异常直接冒泡；`Agent` 透传该钩子供 Session 层接入 | `tests/agent/test_loop.py` 11 passed（含钩子时序、多批次、跳过与失败冒泡）、`tests/agent/test_agent.py` 透传用例；全量 468 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`038a7ee` |
| M7.6b | 新 user 消息前自动压缩：`AgentSession.run()` 先用 M7.4g 策略（估算 > 窗口 − reserve）判断，需要时复用 M7.5 事务、保留预算同手动 `/compact`；窗口未知或未超阈值不动状态，每次 run 只检查一次，压缩成功后只追加一条 user 消息；摘要失败不写半成品 | `tests/session/test_auto_compact_before_prompt.py` 7 passed（小窗口阈值、单次追加、未知窗口、仍超阈值不重复压缩、摘要失败无写入、空任务不触发）；全量 475 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`ae18d7b` |
| M7.6c | 工具轮之间的自动压缩：`AgentSession` 把同一策略判定与 M7.5 事务接到 `prepare_next_turn`，每个完整工具批次提交后按 M7.4g 阈值重新检查；`run()` 的 prompt 前检查与钩子共用 `_auto_compact_if_needed`，保留预算仍为 `DEFAULT_KEEP_RECENT_TOKENS`；成功后同一次 run 用重建投影继续，已执行的工具不重放（ToolMessage 原文与条数不变），不做 overflow 自动 retry；摘要失败保留已提交的工具结果、不写半成品 | `tests/session/test_auto_compact_tool_turn.py` 6 passed（工具轮越线压缩后同一 run 继续、未越线不压缩、未知窗口禁用、第二个工具轮增量压缩带 `<previous-summary>` 且不重发原文、摘要失败工具不重放且投影不半写、阈值夹具）；停用钩子接线后 3 个用例失败，确认覆盖真实触发路径；全量 481 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`6edc306` |
| M7.6d | 自动压缩失败语义：钩子抛出的 `MiniPiError` 在 Loop 内转成 `AgentEndEvent(reason="error")` 与 error assistant 消息（不追加假消息、不改投影），prompt 前的同类失败在 `AgentSession.run()` 内发成对 start/end 事件并返回 error 消息；无安全切点、摘要失败、写盘失败以及**压缩后仍超阈值**都不再发出下一次请求；一次性 CLI 以 error 结束时返回非零码。此处收紧 M7.6b 的旧行为（当时允许带着仍超阈值的投影继续） | `tests/session/test_auto_compact_errors.py` 7 passed（工具轮摘要失败仍保工具结果、无切点、已压缩仍超、单轮即超阈值、写盘失败、失败后可恢复、夹具窗口关系）、`tests/agent/test_loop.py` 钩子 agent error 用例、`tests/cli/test_compact_command.py` REPL 展示与一次性非零码；M7.6b/M7.6c 的失败用例同步改为新语义；去掉“压缩后仍超阈值”分支会打挂对应用例；全量 491 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；`a6d9d9f` |
| M7.6e | 离线端到端：新增 `tests/integration/`（不加 `integration` marker——那是真实 API 专用，目录名与 marker 无关）走生产装配路径（`build_default_registry` + 真实 `Workspace` + 真实 `read`/`write` + 真实 JSONL + `--resume`）：旧回复约 25k token 时用真实日志（read 回 1000 行、约 11k token）把投影推过阈值，压缩后同一次 run 继续；退出后 `AgentSession.resume` 的投影、stepCount、modifiedFiles 与内存一致，首个请求即压缩后投影加新任务；CliRunner 跨进程验证 `--resume` 用的也是压缩后投影 | `tests/integration/test_auto_compaction.py` 4 passed（工具批次真实执行且不重放、恢复一致并沿同一 leaf 续写、CLI 两次调用跨进程、夹具阈值关系与 read 双限自证）；停用工具轮钩子后 3 个用例失败；全量 495 passed, 3 deselected（`NO_COLOR` 清除、`TERM=xterm-256color`）；本提交 |

### 实施与审查规则

1. 每个新编号是一批可审查的最小行为；不提前创建后续编号的接口或占位实现。代码、必要测试、README 与本计划状态在**同一提交**；中文 `feat/fix/docs` 消息。
2. 每批运行针对性测试、全量离线测试和 `git diff --check`，再更新状态；真实模型测试只有执行过才能记“通过”。文件测试用 `tmp_path`，默认测试不联网。
3. 若设计改变 Session/Context/CLI 边界，同步 README 第 2 节与 AGENTS.md。发现文档与代码不一致，先修文档再继续实现。
4. 下一批只做 **M7.6f**；端到端已证明压缩链路与恢复一致，接着评估旧工具结果的成本感知提前压缩，必须用实测成本证明净收益，不能只因累计输入高就额外调用摘要模型。
