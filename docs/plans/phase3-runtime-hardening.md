# Phase 3：可靠性、CLI 体验与外部能力

> 状态：M7.8 已完成；M7.9、M8–M10 未开始。当前实施入口是 M7.9。已完成的 M7.8 交付与验收放在文末。

本计划依据 mini-pi 当前源码、[Pi 本地生产链路](../design/pi-production-architecture.md)和已有验收记录。Pi 参考仓库基线为 `/Users/yzh666/workspace/pi` 的 `d1230ea`；它的能力是设计参考，不是必须逐项复制的清单。实现前核对当前代码，不能把计划写成已交付行为。

## 1. 下一步顺序与准入

| 顺序 | 里程碑 | 进入下一步的条件 |
| --- | --- | --- |
| 1 | M7.9.1–M7.9.3 完成语义与真实任务基线 | 未完成任务不会以成功退出；有可复现的任务成功率、用量与耗时记录 |
| 2 | M7.9.4 CLI 视觉整理 | 宽屏、窄屏、无颜色和非 tty 均清晰；展示不改变模型或 Session 事实 |
| 3 | M8.0 长任务交互 | 在真实长任务中确认追加指令的价值；安全轮次边界和持久化语义经测试 |
| 4 | M8.1 只读 LSP | 固定任务证明定位、引用或诊断收益；若没有收益，暂缓扩展 |
| 5 | M8.2–M8.4 MCP、活动工具集与恢复 | 至少一个明确要接入的本地服务；工具 schema、名称映射、恢复可验证 |
| 6 | M8.5 对照评测 | 与 M7.9 的同一基线比较完成率、工具选择、用量和耗时 |
| 7 | M9 Task / Memory、M10 Multi-Agent | 分别以真实跨 Session 工作流、需要隔离的并行任务为准入 |

M7.9 是小批次修正与测量，不修改 Agent 架构；M8 按收益逐项放行。每个里程碑只实现当前编号，不提前造通用插件框架。

### M7.9.1 未完成任务的退出语义

**现状：** `run_loop` 达到 `max_steps` 发出 `agent_end(step_limit)`，一次性 CLI 只把 `budget_limit`、`cancelled` 和 `error` 转为非零退出码，故可能把未完成任务报告为成功。

**交付：** 一次性模式对 `step_limit` 返回非零码，明确显示终止原因；已提交消息、工具结果、文件改动与 Session 保留，交互模式可继续提问。不把最后一条 assistant 当作已完成回答。

**验收：** FakeLLM 连续请求工具并达到步数上限的 CLI 用例；正常完成、预算限制、错误和取消的退出码回归；不调用真实 API。

### M7.9.2 工作区状态与改动事实

**现状：** `git_diff` 只查看已跟踪文件的未提交差异，未跟踪文件不可见；`bash` 无法可靠填写 `modified_files`。这会影响未来 Task 验收，不能靠模型的总结补齐事实。

**交付：** 为 Agent 提供明确的 Git 工作区状态观察方式，覆盖未跟踪、已暂存和未暂存路径；`git_diff` 继续负责内容差异。`bash` 的改动仍标为未知，不推断为完整的 `modified_files`。非 Git workspace 必须有可解释结果。

**验收：** 临时 Git 仓库中的四类状态（干净、未跟踪、已暂存、未暂存）及非 Git 目录；验证状态不会误写入 Session 消息元数据。

### M7.9.3 真实任务基线与规模测量

**交付：** 固定少量只读、跨文件定位、修改后验证、失败后恢复任务，统一记录任务完成、请求数、工具调用、耗时、Provider 实测 input/output、最终退出原因。另测多 Session 候选及长 JSONL 的 `--continue` / `/sessions` 耗时与内存；记录原始样本、模型、环境和脱敏结果。

**验收：** 离线 FakeLLM 链路可重复；真实 Provider 样本明确记录成功与失败，不把估算用量当实测，不因缺 Key 声称真实验收通过。基线形成后再决定 LSP、工具数量或 Session 加速是否有收益。

**后续按测量处理：** 当前候选任一损坏会阻断 `--continue` 与 `/sessions`，这是刻意的严格校验。若实际遇到损坏，补显式定位和隔离/修复操作，不静默跳过候选；若长会话测出瓶颈，再优化加载或索引，不预先改存储格式。

### M7.9.4 CLI 视觉整理

**定位：** 保留 `mini_pi/assets/banner.txt` 和 `thinking.txt` 原样，采用“清楚的层级、少量强调色、默认紧凑”的终端风格。只改 `mini_pi/cli/` 的展示与必要的终端测试；不引入全屏 TUI、新依赖或新的 AgentEvent，也不调整输入键位。

**启动页：** 宽 tty 在现有 ASCII Banner 下，将版本/模型与项目/短 Session ID 排成两行清晰的身份栏，再给一行 `/help` 提示；保持适度留白，不加大边框或重复打印完整路径。40 列窄屏及非 tty 继续按短字段分行，`--no-banner` 仍可关闭 Art；完整 cwd 和 Session 路径只在 `/status full`。

**任务流：** 用户输入、模型正文、工具操作、最终结果使用一致的间距与语义颜色：青色表示进行中，绿色表示工具成功，黄色表示预算或截断等注意，红色表示失败/取消。颜色必须辅以文字或符号，不能独自承载状态。tty 的工具执行中可用单条临时进度，完成后收敛为一条有界结果；非 tty 只输出确定性、无 ANSI 控制的最终文本行，不做动态重绘。保留现有 `thinking.txt` 的瞬时状态，正文或工具事件出现时清理。未知、组合或含凭据命令仍用保守标题，失败状态只根据真实事件和退出码显示。

**状态页：** `/status`、`/context`、`/tools` 使用一致的标签宽度、分组与数值对齐；默认先呈现用户要做决策的信息（模型/项目/会话、当前请求估算、最近任务实测、预算/压缩状态），详细统计留在对应命令，不在每轮结果后重复铺开。`/sessions` 保持严格候选校验与短 ID，列表仅改善对齐和当前标记。

目标布局示意（文案和间距可在 tty 验收时微调，不规定具体颜色代码）：

```text
mini-pi 0.x · deepseek/deepseek-flash
mini-pi · session a1b2c3d4                         /help

› 检查并修复测试
✓ Read tests/test_cli.py · completed
✗ Run pytest · exit 1 · stderr: assertion failed
✓ Edit tests/test_cli.py · 1 file changed
✓ Run pytest · exit 0

Completed · 4 tools · 3 requests · provider in/out …
```

**验收：** 用固定 FakeLLM 事件和真实 pty 检查 40/80/120 列、`NO_COLOR`/`TERM=dumb`、非 tty、`--no-banner`、长路径/中英文/多行正文，以及成功、ToolError、shell 非零、超时、截断、预算、`step_limit` 与取消。检查输出不越界、不泄露凭据、无残留 Live 区域；普通模式有界，`--verbose` 仅展开工具已捕获内容。对比修改前后的 JSONL、模型请求及 `ToolMessage`，确认 UI-only 数据没有进入事实链。保留脱敏 tty 截图或转录作为视觉验收记录。

## 2. M8 交互与外部能力

### M8.0 长任务交互

先定义最小的用户输入队列：steering 在完整 assistant/tool batch 后、下一次请求前提交；follow-up 在 Agent 原本要结束时提交。待发送输入不得提前进入模型消息或 JSONL；成功提交后仍遵守 durable-first、tool call/result 配对和预算边界。CLI 在运行期间可接收输入，但同一 Agent 仍串行执行模型请求与工具；Ctrl+C 继续表示取消当前 run。

验收：输入先后顺序、取消前后的队列处理、提交失败、Session resume、非 tty 一次性模式与真实 tty 人工操作。先验证 M7.9 基线中确有长任务交互收益，再定具体输入并发机制；不引入并行工具执行。

### M8.1 只读 LSP

`Tool → LspAdapter → JSON-RPC → Language Server`，先用一个实际语言服务器验证 `definition` / `references` / `symbols` / `diagnostics`。文件 URI 必须经 `Workspace.resolve`；初始化、`initialized`、崩溃、超时、JSON-RPC 错误及 workspace 越界都要明确处理。长驻进程在会话关闭和空闲超时回收，不在每次 `run()` 后重启。

验收：离线假 server 覆盖握手、响应、崩溃、超时、越界；真实 server 人工验证一次；与 M7.9 相同定位任务对照完成率、耗时和模型用量。若现有 `search` + `bash` 已足够，保留原型而不继续扩展 rename / format / codeAction。

### M8.2 MCP stdio client

只做 client 和 stdio transport，且以明确的本地 server 场景启动。外部工具经 `ToolRegistry` 调度，Loop 不感知 MCP。Provider 工具名须符合函数名限制并稳定映射到 server/tool 原名；反向映射要可持久恢复。JSON Schema 必须完整校验受支持子集，不支持的构造 Fail Fast。配置和凭据仅在用户级目录或环境变量，Session 不存 Key。

验收：真实 subprocess 驱动的离线假 server 覆盖初始化、`tools/list`、`tools/call`、重名、非法 schema、崩溃、超时和错误结果。没有明确 server 需求时暂缓此项。

### M8.3 活动工具集

外部工具多起来后再引入显式 allowlist，稳定过滤请求 schema 与实际可执行工具；`/tools` 区分活动和全部工具。禁用工具的调用返回明确错误。基于 M7.9 用量记录证明过滤收益，不做 AI 自动挑选工具。

### M8.4 外部工具恢复

Prompt section patch 只记录模型可见文本，无法单独恢复 server 配置、完整 schema、名称映射及 allowlist。采用不含凭据的工具集快照/变更记录，或引用有版本与摘要的用户级配置；resume 时校验当前 `tools/list`，缺失或漂移明确报错，不静默删工具。

### M8.5 对照评测

在 M7.9 固定任务和模型上对比 builtin、少量 LSP/MCP、较多工具及活动过滤。记录请求、工具调用、实测用量、完成率、耗时、工具选择错误；可附加 System Prompt `# Tools` 段保留/精简 A/B，同时检查任务完成率。没有真实 Provider 结果前不宣称节省费用。

## 3. M9 与 M10 准入

**M9 Task / Project Memory：** 仅在出现跨 Session 工作流后启动。Task 记录目标、状态、验收、关联 Session、改动与真实验证命令；`bash` 改动未知不能伪称完整。Memory 只存带来源和时间、可核对且默认经人工确认的项目事实；compaction summary 不是事实库。不引入 RAG 或向量数据库。

**M10 Multi-Agent：** 仅在角色确实不同、上下文需要隔离且任务可以并行时启动。先交付 Session fork/多 leaf 和通过 Tool 建立的 worktree 隔离，再验证 Parent → Worker（独立 worktree、可写）→ Reviewer（只读）。父子只交换结构化任务与结果，不共享 transcript；暂不做通用调度器。

## 4. 始终保持的边界

- `CLI → AgentSession → Agent → run_loop → (LLM, ToolRegistry) → Tool → Workspace`。CLI 只消费事件；Agent 不直接执行 Shell，Tool 不控制 Loop。
- JSONL 保存完整事实；展示摘要不改 `ToolMessage`；压缩只更换可重建的模型投影，不拆散 tool call/result。
- 当前请求的窗口与任务预算共用 RequestSnapshot；历史 Provider usage 与估算分开，未知窗口不启用自动压缩。
- 文件工具经 Workspace 限界；`bash` 是无沙箱本地 Shell，仅固定 cwd。接入不可信外部工具前要重新评估执行权限；不能把 cwd 描述成沙箱。
- 只支持 OpenAI / DeepSeek；不因参考 Pi 而引入其他 Provider、Agent 框架或通用扩展系统。
- 非预期异常直接冒泡；预期的工具、模型和 Session 错误按现有协议显式处理。

## 5. 状态与交付记录

| 里程碑 | 状态 | 交付依据 |
| --- | --- | --- |
| M7.8 Runtime Hardening | 已完成 | `0e5848e`、`0517a4e`、`4fbedde`、`e19182b`、`2bc65ee`、`96c6279`；总验收 `5247b55`；[记录](../benchmarks/m7-8-final-acceptance.md) |
| M7.8 CI 修补 | 已完成 | `fb5f387`；彩色帮助输出回归，566 passed / 5 deselected；[GitHub CI](https://github.com/w0nderful-yzh/mini-pi/actions/runs/36373856072) 的 Test/Lint/Compile 通过 |
| M7.9.1–M7.9.4 | 未开始 | 完成语义、工作区状态、真实任务与规模基线、CLI 视觉整理 |
| M8.0–M8.5 | 未开始 | 交互、只读 LSP、MCP、活动工具集、恢复、对照评测 |
| M9 / M10 | 未开始 | 分别等待跨 Session 工作流和隔离并行任务 |

M7.8 细节留在上述提交、测试与验收记录中，已完成任务不再占据计划正文。每批代码、测试、README 与计划状态合并提交；验证只写实际运行结果。修改 Agent Core、Session 或 Context 的设计前，先核对 [Pi 生产架构参考](../design/pi-production-architecture.md)对应章节和 README 第 2 节。
