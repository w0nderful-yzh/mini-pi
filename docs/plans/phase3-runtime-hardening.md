# Phase 3：可靠性、CLI 体验与外部能力

> 状态：M7.8 与 M7.9 全部完成；M8–M10 未开始。当前实施入口是 M8.0。已完成的交付与验收放在文末。

本计划依据 mini-pi 当前源码、[Pi 本地生产链路](../design/pi-production-architecture.md)和已有验收记录。Pi 参考仓库基线为 `/Users/yzh666/workspace/pi` 的 `d1230ea`；它的能力是设计参考，不是必须逐项复制的清单。实现前核对当前代码，不能把计划写成已交付行为。

## 1. 下一步顺序与准入

| 顺序 | 里程碑 | 进入下一步的条件 |
| --- | --- | --- |
| 1 | M7.9.1–M7.9.3 完成语义与真实任务基线 | 已完成：未完成任务不会以成功退出，工作区状态与改动事实可观察，真实任务成功率/用量/耗时与 Session 规模已有可复现记录（[基线](../benchmarks/m7-9-baseline.md)） |
| 2 | M7.9.4 CLI 视觉整理 | 已完成：宽屏、窄屏、`TERM=dumb`、`NO_COLOR` 与非 tty 均清晰，展示不改变模型或 Session 事实 |
| 3 | M8.0 长任务交互 | 在真实长任务中确认追加指令的价值；安全轮次边界和持久化语义经测试 |
| 4 | M8.1 只读 LSP | 固定任务证明定位、引用或诊断收益；若没有收益，暂缓扩展 |
| 5 | M8.2–M8.4 MCP、活动工具集与恢复 | 至少一个明确要接入的本地服务；工具 schema、名称映射、恢复可验证 |
| 6 | M8.5 对照评测 | 与 M7.9 的同一基线比较完成率、工具选择、用量和耗时 |
| 7 | M9 Task / Memory、M10 Multi-Agent | 分别以真实跨 Session 工作流、需要隔离的并行任务为准入 |

M7.9 是小批次修正与测量，不修改 Agent 架构；M8 按收益逐项放行。每个里程碑只实现当前编号，不提前造通用插件框架。

### M7.9.1 未完成任务的退出语义

**已交付。** `run_loop` 在 `agent_end(step_limit)` 中带上实际生效的 `max_steps`；渲染层说明上限值与「任务未完成」，不会把最后一条 assistant 正文当作完成态。一次性 CLI 的退出码改为按终止原因集中映射（`mini_pi/cli/app.py` 的 `EXIT_CODES`）：`completed 0` / `error 1` / `budget_limit 2` / `step_limit 3` / `cancelled 130`；拿不到 `agent_end` 时按 1 处理。`step_limit` 与 `budget_limit` 一样只结束本次 run，已提交的消息、工具结果、文件改动与 Session 保留，交互模式下一条提问重新计数。

**验收：** `tests/cli/test_exit_semantics.py` 8 passed——五种终止原因的一次性退出码表（显式写死 0/1/2/3/130，并与 `EXIT_CODES` 对照）、`step_limit` 显示上限值与未完成并保留已提交的 tool 结果、REPL 在 `step_limit` 后继续接受输入、正常完成无未完成提示；`tests/test_console.py::test_renders_step_limit` 断言新文案。把 `EXIT_CODES["step_limit"]` 改回 0 会让专项用例失败。全部为 FakeLLM，不调用真实 API。

### M7.9.2 工作区状态与改动事实

**已交付。** 新增只读工具 `git_status`（`mini_pi/tools/git_status.py`，name=`git_status`）：`git rev-parse --show-prefix` 先确认 workspace 在 git worktree 内，再以 `git status --porcelain=v1 --branch --untracked-files=all --ignored=no -z -- .` 观察状态，输出给出分支与 `conflicted` / `staged` / `unstaged` / `untracked` 分组计数（冲突放最前，避免按 head 截断时被大量普通改动挤掉），组内按路径排序；重命名渲染为 `原路径 -> 新路径`，两侧都有改动的路径在两个分组各出现一次。路径基准统一为 workspace 相对（子目录 workspace 用 `--show-prefix` 去掉仓库根前缀），查询用 `-- .` 限定在 workspace 内，因此仓库其他目录的改动不会混入；前缀不匹配属于前提被破坏，直接 `ToolError`。非 git workspace 报可解释错误，不返回空的“干净”结果。`git_diff` 继续只负责内容差异，`bash` 与只读工具的 `modified_files` 一律留空，状态路径不写入 Session 元数据。CLI 侧补上 `Inspect git status` 标题与 `N changed path(s)` 结果行。

**验收：** `tests/test_git_status.py` 11 passed——干净、未跟踪、已暂存、未暂存（含两种删除）、两侧同时改动、暂存重命名、合并冲突、忽略文件、子目录 workspace 只报告本子树、非 git 报错、`modified_files` 为空、`AgentSession` 落盘后 ToolMessage 与原始 JSONL 都没有把脏路径写成 `modifiedFiles`；`tests/test_bash.py` 增加“bash 从不声明 modified_files”；`tests/cli/test_tool_events.py` 增加 `Inspect git status` 标题与 `N changed path(s)` 结果行；`tests/test_registry_defaults.py` 与 README 工具清单同步。全量 588 passed / 5 deselected。全部为离线真实 git 命令，不调用真实 API。

**遗留：** `git_diff` 仍不显示未跟踪文件内容（未跟踪文件没有索引侧差异，用 `git_status` 定位后 `read` 查看）；`bash` 的改动继续标为未知。

### M7.9.3 真实任务基线与规模测量

**已交付。** 新增离线基线用例 `tests/integration/test_task_baseline.py`（四条固定任务形态：只读、跨文件定位、修改后验证、命令失败后换命令并修复；脚本化 FakeLLM 走生产装配路径，断言请求数、工具调用数、终止原因、`modified_files`，任务完成与否由任务结束后重跑 pytest 的真实结果判定）与驱动器 `docs/benchmarks/m7-9-baseline.py`（`real` / `scale` / `tools` 三种模式，真实任务在临时 workspace 与临时 HOME 中运行，`--only` 可重跑单条，`--tool-counts` 扫描工具数）。记录见 [M7.9.3 基线](../benchmarks/m7-9-baseline.md)。

真实 Provider（`deepseek-flash`）四条任务各一次，全部 `completed`、退出码 0：合计 16 次请求、20 次工具调用、35,689 实测 input / 1,587 实测 output、15.1s，两个修复任务的 `calculator.py` 改动由 pytest 复验通过，两个只读任务没有改动文件。规模测量：1600 条消息恢复 129 ms / 6.87 MB 分配峰值，40 个候选 × 200 条时 `/sessions` 与 `--continue` 约 1.3 s。

**验收与随之落地的估算修正：** 离线用例 5 passed 且可重复；真实样本按 Provider usage 记录，未把估算当实测。`tools` 模式复核发现 7 个工具时低估 16.6%（超出 M7.8.2 的 ±15% 容差），按 1/3/7 工具扫描拟合出「一次性 220 + 每工具 32 结构开销」，`estimate_tools_tokens()` 改为按工具数累加结构开销；0–7 个工具复验（含未参与拟合的 2 与 5）全部在 ±5% 内，预算夹具阈值随新预测重新标定。该修正只影响工具项，`source` 仍标为 estimated；只在 `deepseek-flash` 上对拍过。

### M7.9.4 CLI 视觉整理

**已交付。** 新增 `mini_pi/cli/style.py` 作为唯一视觉词汇：语义色（青进行中 / 绿成功 / 黄注意 / 红失败）、行首标记（`● ✓ ✗ ⚠ ›`）、统一标签列宽与千位分隔。启动页改为两行身份栏（版本·模型 / 项目·短 Session ID + 右对齐 `/help for commands`），40 列、`TERM=dumb` 与非 tty 按字段分行。任务流中工具结束行重复操作标题（`✓ Read app.py · completed`）并合并结果，收尾统一为 `Completed/Stopped/Failed/Cancelled · N tools · M requests · in/out · 秒`；取消按失败色展示。状态页 `/status` 默认只给决策信息，请求/工具/耗时移到 `/status full`；`/context` 分三组，`/tools` 与 `/sessions` 对齐列。展示层关闭 Rich 自动高亮（它会切碎语义色），tty 内长行按宽度折行、非 tty 保持每事件一行的确定性文本。未改 `banner.txt` / `thinking.txt`、未加依赖或 AgentEvent、未动输入键位。

**验收：** `tests/cli/test_visual_layout.py` 19 passed（全部状态都有文本标记、结果行重复标题、收尾行顺序与千位分隔、partial/缺失 usage 标注、状态页共用一个值列、窄屏/`TERM=dumb`/非 tty 字段行、宽屏身份栏不越界、会话列表对齐、非 tty 无 ANSI 且每事件一行）；既有终端测试同步到新契约。真实 pty 记录见 [M7.9.4 视觉验收](../benchmarks/m7-9-4-cli-visual.md)：80 列任务流与状态页、40 列窄屏、`TERM=dumb`、`NO_COLOR`、非 tty 一次性输出与任务中 Ctrl+C 全部通过断言，无残留 Live 区域。全量 612 passed / 5 deselected。展示改动不写 `ToolMessage`、JSONL 与模型请求。

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
- `/sessions` 与 `--continue` 继续严格加载全部候选：任一候选损坏即整体失败，不静默跳过；若真实遇到损坏再补显式定位与隔离操作，不因此放宽校验或预先改存储格式。
- 文件工具经 Workspace 限界；`bash` 是无沙箱本地 Shell，仅固定 cwd。接入不可信外部工具前要重新评估执行权限；不能把 cwd 描述成沙箱。
- 只支持 OpenAI / DeepSeek；不因参考 Pi 而引入其他 Provider、Agent 框架或通用扩展系统。
- 非预期异常直接冒泡；预期的工具、模型和 Session 错误按现有协议显式处理。

## 5. 状态与交付记录

| 里程碑 | 状态 | 交付依据 |
| --- | --- | --- |
| M7.8 Runtime Hardening | 已完成 | `0e5848e`、`0517a4e`、`4fbedde`、`e19182b`、`2bc65ee`、`96c6279`；总验收 `5247b55`；[记录](../benchmarks/m7-8-final-acceptance.md) |
| M7.8 CI 修补 | 已完成 | `fb5f387`；彩色帮助输出回归，566 passed / 5 deselected；[GitHub CI](https://github.com/w0nderful-yzh/mini-pi/actions/runs/36373856072) 的 Test/Lint/Compile 通过 |
| M7.9.1 未完成任务的退出语义 | 已完成 | `step_limit` 携带上限值、一次性退出码按终止原因映射（0/1/2/3/130）；全量 574 passed / 5 deselected；`8a8d297` |
| M7.9.2 工作区状态与改动事实 | 已完成 | 新增只读 `git_status`（分组状态 + workspace 相对路径 + 非 git 可解释失败），状态路径不进入 Session 元数据；全量 588 passed / 5 deselected |
| M7.9.3 真实任务基线与规模测量 | 已完成 | 离线基线用例 5 passed；真实 DeepSeek 四条任务 4/4 completed（16 请求 / 20 工具调用 / 35,689 input）；工具开销按 1/3/7 扫描改为「一次性 220 + 每工具 32」，0–7 工具复验 ±5% 内；全量 593 passed / 5 deselected；[基线](../benchmarks/m7-9-baseline.md) |
| M7.9.4 CLI 视觉整理 | 已完成 | 统一视觉词汇、身份栏、工具结果行带标题、收尾统计行、状态页对齐与分组；[视觉验收](../benchmarks/m7-9-4-cli-visual.md)；全量 612 passed / 5 deselected |
| M8.0–M8.5 | 未开始 | 交互、只读 LSP、MCP、活动工具集、恢复、对照评测 |
| M9 / M10 | 未开始 | 分别等待跨 Session 工作流和隔离并行任务 |

M7.8 细节留在上述提交、测试与验收记录中，已完成任务不再占据计划正文。每批代码、测试、README 与计划状态合并提交；验证只写实际运行结果。修改 Agent Core、Session 或 Context 的设计前，先核对 [Pi 生产架构参考](../design/pi-production-architecture.md)对应章节和 README 第 2 节。
