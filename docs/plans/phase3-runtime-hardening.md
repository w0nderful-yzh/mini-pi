# Phase 3：Runtime Hardening 与外部能力（M7.8 / M8 / M9 / M10）

> 状态：**规划已定稿，尚未开始实施**。M7（Session / Context 与 CLI）已于 2026-09-24 验收完成；M7.8 未开始，M8–M10 未开始。本文件是 M7.8 的实施依据，同时承接 phase2 第 5 节的 M8–M10 概要。

**目标：** 在接入 LSP / MCP 之前，先把「上下文有多少、预算怎么算、钩子挂在哪、改动怎么被守护」四件事定下来；之后按 LSP → MCP → 动态工具集 → 工具集恢复 → 基准的顺序扩展外部能力。

**依据：** 用户提供的《mini-pi 代码质量评审与后续规划》（下称「评审」）、当前 `mini_pi` 实现与测试、[Pi 生产架构参考](../design/pi-production-architecture.md)、`AGENTS.md`。评审是需求素材；与仓库事实、Provider 协议或本文件的取舍冲突时，以本文件与仓库事实为准。

---

## 1. 评审核对：采纳、修正与推迟

核对方式是逐条在仓库里定位证据，避免把评审的示意数字当成结论。

### 1.1 核对属实，采纳

| 评审条目 | 结论 | 仓库证据 |
| --- | --- | --- |
| 3.1 | 估算规则是「4 字符 ≈ 1 token」，中文会低估 | `mini_pi/context/tokens.py:20` 定义 `_CHARS_PER_TOKEN = 4`，`:57` 用 `_ceil_div(len(...), 4)` 按字符数估算 |
| 3.1 | Tool Schema 不在估算输入里 | `estimate_tokens(messages)` 只接收消息；工具 schema 只在 `mini_pi/agent/loop.py:270`、`mini_pi/agent/agent.py:78` 交给 LLM |
| 3.2 | `bash` 不是沙箱 | `mini_pi/tools/bash.py` 只设置 `cwd = workspace root`，路径边界仅在 `mini_pi/workspace/workspace.py` |
| 3.3 | `AgentSession` 职责偏多 | `mini_pi/session/runtime.py` 425 行，同时管创建/恢复/装配/压缩/预算/模型切换 |
| 3.3 | `_cost_compaction_attempted` 是 per-run 状态 | `runtime.py:105` 初始化、`runtime.py:275` 每次 `run()` 重置 |
| 3.4 | 只有一个零参 `prepare_next_turn` | `mini_pi/agent/state.py:12`：`PrepareNextTurn = Callable[[], None]` |
| 3.5 | 空文件报错不当 | `mini_pi/tools/read.py:46`：空文件 `total=0`、`offset=1` → `offset 1 is beyond end of file (0 lines)` |
| 3.6 | 依赖 POSIX 语义 | `mini_pi/tools/process.py:90,111` `start_new_session`、`:211` `os.killpg(..., SIGKILL)` |
| 4 | 工具描述重复发送 | `mini_pi/agent/prompt.py:33,44` 的 `tools` 段 + 请求里的 Tool JSON Schema |
| M7.8.5 | 无 CI、无静态检查 | 无 `.github/workflows`；`pyproject.toml` dev 组仅 pytest/httpx；`uv run ruff` 不存在 |

### 1.2 需要修正的评审结论

1. **`estimate_tokens` 不是纯字符估算，工具成本大体已被间接计价。**
   `tokens.py:40-48` 优先采用最近一次 assistant 的 `usage.total_tokens` 作为已知前缀成本，只对其后的消息按字符估算。该 usage 来自一次**包含 tool schema** 的真实请求，所以对「本会话已发生过至少一次请求」的窗口判定，工具成本已经算进去了。真正缺口只有三处：① 本 Session 的首次请求（还没有 usage 锚点）；② `/context`、`predicted_input` 这类纯投影口径；③ 工具集变化后旧锚点失真。M7.8.2 必须分别处理，不能笼统声称「工具 schema 没算」。

2. **中文低估的程度要实测，不接受评审的示意数字。**
   评审的「实际 80k / 估算 30k~40k」没有来源。M7.8.2 要求以 Provider usage 为真值量化偏差后再定系数，样本与结论写入 `docs/benchmarks/`。

3. **不把 `prepare_next_turn` 拆成 `before_request` / `after_tool_batch` / `after_run` 三套钩子。**
   Pi 的做法是把 Compaction、Prompt refresh、动态工具与模型切换都挂在**同一个 turn 边界**上（[参考 §5.1](../design/pi-production-architecture.md)），mini-pi 的 `prepare_next_turn` 正是这个边界。M7.8.4 只做两件事：给它一个显式上下文（`RunContext`）、允许它替换下一次请求的投影；需要多步时在会话层内部按固定顺序组合，不在 Loop 里加 `if`，也不引入插件框架。
   任务预算（`budget_limit`）继续留在 Loop：它依赖 Loop 自己刚算出的 `predicted_input`（`agent/loop.py:166`），外置反而要回传中间值。

4. **不删 System Prompt 的 `# Tools` 段（评审 §4）。**
   Pi 生产路径同样同时发送 `tools` prompt section 与 Tool JSON Schema，且 `buildRules()` 会按活动工具生成规则（[参考 §9.1](../design/pi-production-architecture.md)）。A/B 值得做，但要等 M8.3 把工具数量真正推上去之后，且必须同时测 Tool Call 成功率与任务完成率；默认保留现状，把 A/B 并入 M8.5。

5. **MCP 工具名不能用 `server:tool`（评审 §8.3 示例）。**
   Provider 的 function name 只接受字母、数字、下划线和连字符（≤64 字符），`github:get_issue` 会被 API 直接拒绝。M8.2 必须定义确定的「外部名 → Provider 安全名」映射，保证可逆（供展示与恢复）并对跨 server 重名 Fail Fast。当前 `ToolRegistry.register()` 只查重、不校验名字形态。

6. **M10 有评审未列出的硬前置。**
   Worker/Reviewer 需要**同一 Session 的分叉**，而 `AGENTS.md` 与 phase2 都写明「fork 留到后续」，`JsonlSession` 目前只有单 leaf 追加；worktree 创建也必须经 Tool（Agent 不得直接执行 git）。这两项要作为 M10 的前置里程碑显式排期，不能默认「做到 Multi-Agent 时自然就有」。

### 1.3 评审未覆盖、但按现状要一并处理的

- **窗口来源要收敛为一份。** 引入 `--context-window` 后，`cli/status.py:30`、`cli/status.py:126`（展示）与 `runtime.py:291`、`runtime.py:336`（决策）必须读同一份解析结果，否则「显示的窗口」和「决策用的窗口」会分叉。
- **文档同步项：** README §8 路线图、§9 已知限制（`bash` 无沙箱、平台支持、估算口径）、AGENTS.md §9 Shell Tool 措辞与 §20 开发顺序。
- **不扩张：** `write` / `edit` 的空值与唯一匹配语义已有覆盖（`AGENTS.md` §10），不借 M7.8 顺手改。

---

## 2. M7.8 Runtime Hardening

**完成标准：** 成本与窗口口径统一到一个请求对象；中文与工具 schema 不再被系统性低估（有实测记录）；窗口可由用户显式配置且展示与决策一致；per-run 状态有归属、turn 边界不靠新增分支扩展；后续每一步改动都有 CI 与静态检查守护。

### 2.0 执行顺序

编号沿用评审，落地顺序按依赖排列：

| 顺序 | 子项 | 为什么在这个位置 |
| ---: | --- | --- |
| 1 | M7.8.0 前置小修（本文件新增） | 无依赖，先清掉已确认的错误行为与文档歧义 |
| 2 | M7.8.5 CI 与静态检查 | 后面每一步都要靠它守卫 |
| 3 | M7.8.1 RequestSnapshot | 统一口径的载体，是 2 的前置 |
| 4 | M7.8.2 Token 估算升级 | 依赖 M7.8.1 的请求对象 |
| 5 | M7.8.3 Context Window 配置化 | 独立小改，可在 M7.8.5 之后任意时刻插入 |
| 6 | M7.8.4 Runtime Hook 与 RunContext | 结构调整风险最高，放最后 |
| 7 | M7.8.6 总验收与文档同步 | 收口 |

### M7.8.0 前置小修

**改动：**

- `read` 空文件（**唯一剩余代码改动**）：`total == 0` 时返回空内容并附一行说明（如 `[File is empty.]`），只有 `total > 0 and offset > total` 才报 `offset ... beyond end of file`。语义定为「空文件永远返回空内容」，让模型能区分「文件是空的」与「路径错了」。
- 文档措辞：README §2/§4/§9（`bash` 是**无沙箱本地 shell**、支持平台为 **macOS / Linux**、当前估算口径）与 `AGENTS.md` §9 边界说明。**这部分已随本规划提交一并落地**，实施该子项时只需复核，不再重复修改。

**不做：** 不实现 Docker / VM 沙箱，不引入 Permission / Capability 系统（等真实需求）。

**验收：** `tests/test_read.py` 增加空文件与 `offset` 越界两组用例（空文件返回空内容、非空文件越界仍报错）；已落地的文档措辞与实际行为一致。

### M7.8.5 CI 与静态检查

**改动：**

- 新增 `.github/workflows/ci.yml`，步骤与本地一致：`uv sync` → `uv run pytest` → `uv run ruff check .` → `python -m compileall -q mini_pi` → `git diff --check`。
- `pyproject.toml`：dev 组加 `ruff`，提交最小 `[tool.ruff]` 配置（`target-version = "py312"`，行宽与现有风格一致）。
- 首次接入会有存量告警：通过**选定能一次清零的规则集**处理，禁止 `# noqa` 批量掩盖；清理纳入本子项提交。

**不做：** 不上 mypy（当前没有类型化基线，成本高于收益），不做自动发布。

**验收：** 本地 `uv run ruff check .` 干净；CI 步骤在本地逐条复现结果一致；离线测试保持全绿。

### M7.8.1 RequestSnapshot

**目标：** 用「一次真实请求」取代「一堆消息」，作为窗口、预算、展示、未来基准的统一口径。

**改动：**

- 新增 `mini_pi/context/request.py`：

  ```python
  @dataclass(frozen=True, slots=True)
  class RequestSnapshot:
      messages: tuple[Message, ...]
      tools: tuple[ToolSchema, ...]
      provider: str
      model: str
      input_tokens: int
      source: TokenSource   # usage | estimated | mixed
  ```

- `estimate_request(...)` 是唯一入口；`estimate_tokens(messages)` 退化为内部实现或薄封装，不能留下第二套口径。
- 收敛调用点：`agent/loop.py:166,177`（预算预测）、`runtime.py:295,307`（窗口判定）、`cli/status.py`（`/context` `/status`）、`context/stats.py`。
- `ContextStats` 增加 `tools` 分量，`/context` 显示工具 schema 占用。

**不做：** 不把 `RequestSnapshot` 写进 JSONL（它是派生量，不是事实）；不引入 Provider 专用 tokenizer 依赖。

**验收：** 同一份 messages + tools 在各调用点得到同一个 `input_tokens`（新增一致性用例）；`/context` 分量之和等于 `total`；含 usage 的轮次上，估算与 Provider usage 的差可解释且被记录。

### M7.8.2 Token 估算升级

**目标：** 不严重低估实际 Context。

**改动：**

- 字符规则从单一「4 字符 / token」改为**按字符类加权**（ASCII、CJK、其他宽字符分别取系数），保持向上取整与可复现。
- 工具 schema 文本（`json.dumps(schema, sort_keys=True)` 稳定序列化）计入估算；`source` 继续区分实测与估算，**不得**把含估算的结果标成 `usage`。
- 保留 usage 锚点语义（`tokens.py:40-48`），但锚点与当前工具集不一致时按估算重算并在 `source` 上体现。

**方法（先测后调）：**

- 新增可对拍的用例：固定中文、英文、混合样本，比较 `estimate_request()` 与真实 `usage.input_tokens`，输出偏差比。
- 用真实 DeepSeek 跑一次最小样本（1 次请求、短输入），把偏差与最终系数写入 `docs/benchmarks/m7-8-token-estimation.md`；**没有实测前不写具体倍数**。

**不做：** 不追求 tokenizer 级精确，不引入 `tiktoken` 等新依赖，不按模型维护系数表（除非实测显示必须）。

**验收：** 中文样本不再系统性低估（偏差落在记录文件声明的容差内）；`tests/context/test_tokens.py` 覆盖三类字符、工具 schema、usage 锚点与混合来源；全量离线测试全绿。

### M7.8.3 Context Window 配置化

**改动：**

- CLI 新增 `--context-window INT`（并允许 `--reserve-tokens`，默认沿用 `DEFAULT_RESERVE_TOKENS = 8192`）。
- 解析一次、注入一处：`AgentSession` 持有解析后的策略，`run()`、工具轮钩子、`/context`、`/status` 全部读同一份，删除 `cli/status.py` 里各自的 `resolve_policy(model)` 调用。
- 未知模型且未传窗口 = 维持现状：自动压缩关闭，`/context` 明确显示「未配置」。

**验收：** 自定义模型名 + `--context-window` 能触发自动压缩（小窗口夹具）；`/context` 显示的窗口与决策使用的窗口是同一个值；不传参时行为与当前完全一致（回归用例）。

### M7.8.4 Runtime Hook 与 RunContext

**改动：**

- 新增 per-run 状态对象 `RunContext`（`run_id`、`request_count`、`input_budget`、`cost_compaction_attempted`、`cancelled`），把 `runtime.py:105` 的 `_cost_compaction_attempted` 迁入，`run()` 开始时新建，避免跨 run 泄漏。
- `prepare_next_turn` 契约升级为接收 `RunContext`，并允许返回替换后的投影；`None` 时保持现有事件与行为，避免打挂已有测试。
- 会话层内部把它组合成有序步骤（窗口压缩 → 成本压缩 → 未来的 prompt / 工具集 refresh），每步独立可测；Loop 内不新增分支判断。
- 新增 `after_run`（会话层收尾：usage 汇总，以及未来 LSP / MCP 的生命周期回收）。
- **不新增 `before_request`**，理由见 §1.2 第 3 条。

**不做：** 不做通用插件框架、事件总线或 `HookManager` 抽象；不做并行工具执行。

**验收：** `tests/agent/test_loop.py` 与 `tests/session/*` 全绿；新增用例证明 per-run 状态不跨 `run()` 泄漏（连续两次 run 各触发一次成本压缩）；投影替换路径与现有压缩用例行为一致。

### M7.8.6 总验收与文档同步

- 离线全量回归 + 不变量专项（`--no-session`、append-only、tool pair、预算、compaction）。
- 真实 DeepSeek 一次最小请求对拍（估算 vs usage），记录偏差。
- 确认默认关闭或默认不变的新能力没有改变现有行为。
- 同步 README §2/§8/§9、`AGENTS.md` §6/§9/§20/§23 与本文件状态表，并在 phase2 第 5 节留指向本文件的入口。

---

## 3. M8 External Capabilities

### M8.1 LSP（只读）

- **能力：** `definition` / `references` / `symbols` / `diagnostics`，全部只读；不实现 rename / format / codeAction。
- **结构：** `Tool → LspAdapter → JSON-RPC → Language Server`，Agent 与 Loop 不感知 LSP。
- **复用：** 长驻子进程的启动、超时与进程组清理沿用 `tools/process.py` 的既有语义；路径 → `file://` URI 必须经 `Workspace.resolve`。
- **必须处理：** 初始化握手与 `initialized`、server 崩溃、请求超时、server 不存在、workspace 外文件、JSON-RPC 错误 —— 一律转 `ToolError`，不静默返回空结果。
- **生命周期：** 由 M7.8.4 的 `after_run` 回收，或空闲超时退出。
- **验收：** 离线假 server（脚本化 JSON-RPC）覆盖握手、正常响应、崩溃、超时、越界路径；真实语言服务器人工验证一次并记录。

### M8.2 MCP（stdio client）

- **范围：** client only / stdio only；不做 MCP server、OAuth、远程 transport。
- **结构：** `ToolSource` 抽象 → `BuiltinToolSource` / `McpToolSource`，`ToolRegistry` 只保留 `schemas()` 与 `execute(name, args)`，**Loop 不改**。
- **名字：** 外部名映射为 Provider 安全名（`[A-Za-z0-9_-]{1,64}`），映射确定且可逆；跨 server 重名 Fail Fast；参数校验仍用 pydantic（由 schema 生成动态模型）。
- **配置：** server 配置放用户级目录，凭据只走环境变量或 `~/.mini-pi`，禁止写入项目目录、日志或提交。
- **错误：** server 未启动、中途退出、协议错误、工具自身报错 → `ToolError`；不静默降级为「工具不存在」。
- **验收：** 离线假 stdio server（真实 subprocess + 脚本化 JSON-RPC）覆盖握手、`tools/list`、`tools/call`、崩溃、超时、非法 JSON、重名冲突。

### M8.3 Active Tool Set

- **问题：** MCP 会把工具数从 6 推到几十上百，`registry.schemas()` 全量发送会占掉可观的固定上下文。
- **方案：** `ToolCatalog` + 显式配置的 `ActiveToolSet`（第一版按名字 allowlist，不做模型自动选择）。
- **依赖：** M7.8.1 —— 没有 `RequestSnapshot` 就无法量化「工具变多到底多花多少」。
- **验收：** 过滤生效且顺序稳定；`/tools` 区分 active / total；被禁用工具调用返回可预期错误。

### M8.4 External Tool Restore

- **问题：** resume 后必须能重建原 Session 的工具集，否则历史里出现过的工具今天不存在，Session 语义漂移。
- **方案：** 工具集变化继续经现有 prompt section diff / patch 机制持久化（`mini_pi/context/sections.py` 的 `diff_sections` / `apply_section_patch`），resume 时按记录重建；server 不可用时明确报错，**不静默丢弃**历史调用。
- **验收：** 跨进程 resume 后 `schemas()` 与创建时一致（比较稳定序列化结果）；外部 server 缺失时报错可解释。

### M8.5 Benchmark

- **三组对照：** builtin only / builtin + 少量 LSP+MCP / builtin + 大量工具（ActiveToolSet 过滤前后各一组）。
- **指标：** 请求次数、Tool Call 数、Provider input/output tokens、当前投影大小、任务成功率、耗时、工具选择错误。
- **并入评审 §4 的 A/B：** System Prompt 保留 `# Tools` 段 vs 精简为一句，同时比较 token 与 Tool Call 成功率。
- **验收：** 固定任务、固定模型、原始 usage 脱敏留存，记录可复现；不把估算当实测。

---

## 4. M9 Task / Project Memory

- **Task：** 描述长期工作状态，不是 Thought 日志。字段：`id / goal / status / acceptance_criteria / sessions / modified_files / verification`。`verification` 必须来自真实命令结果，不采信模型自称完成；持久化方式（JSONL entry 或独立 store）在 M9 设计时确定，前提是 resume 后可恢复。
- **Memory：** 只存可核对的项目事实（`key / value / source / timestamp / confirmed`），默认人工确认后写入，按项目 + key 检索；**明确不把 compaction summary 当事实**。
- **门槛：** 出现真实跨 Session 工作流后再开工；不引入 RAG / Vector DB。
- **验收：** Task 跨 resume 可读且状态不漂移；每条 Memory 可追溯来源；未确认的推断不写入。

---

## 5. M10 Multi-Agent

**前置（评审未列，必须显式排期）：**

- Session 分叉 / 多 leaf：`JsonlSession` 目前只有单 leaf 追加，`AGENTS.md` 明确 fork 留到后续。
- Worktree 隔离经 Tool 实现（Agent 不得直接执行 git），子 Agent 的 `Workspace` 指向 worktree。
- 子 Agent 事件桥接到 CLI（每个子 Session 一个渲染前缀），且不污染父 Session 的 transcript。

**第一版只做一个确定场景：** Parent → Worker（独立 worktree，可写）→ Reviewer（只读）。核心解决上下文隔离、workspace 隔离、独立 Session 与结构化结果交换；父子只交换结构化任务 / 结果，不共享 transcript。

**不做：** 通用 Agent Scheduler、动态角色协商、跨 Agent 自由对话。

---

## 6. 依赖与顺序

```text
M7.8.0 前置小修 ─┐
M7.8.5 CI/ruff ──┼→ M7.8.1 RequestSnapshot → M7.8.2 Token 估算 ─┬→ M8.3 ActiveToolSet
M7.8.3 --context-window ─────────────────────────────────────────┘        ↓
                                    └→ M7.8.4 RunContext/Hook ─┬→ M8.1 LSP → M8.2 MCP → M8.4 Restore → M8.5 Benchmark
                                                               └→ M9 Task / Memory

M10 前置：Session fork + worktree Tool → Worker / Reviewer
```

M7.8 内部顺序见 §2.0；M8 内部必须先 LSP 后 MCP（先验证长驻进程与生命周期，再叠协议与工具集）。

---

## 7. 本阶段明确不做

```text
Docker / VM 沙箱            Windows 支持
Permission / Capability 系统 MCP server / OAuth / 远程 transport
AI 自动选择工具              通用插件框架 / HookManager
mypy                        并行工具执行
RAG / Vector DB             通用 Agent Scheduler
```

---

## 8. 里程碑状态表

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M7.8.0 | 前置小修：`read` 空文件、`bash` 无沙箱与平台说明 | 未开始 |
| M7.8.5 | CI 与 ruff 静态检查 | 未开始 |
| M7.8.1 | RequestSnapshot 统一请求口径 | 未开始 |
| M7.8.2 | Token 估算升级（CJK 安全 + 工具 schema + 实测校准） | 未开始 |
| M7.8.3 | Context Window 配置化（`--context-window`） | 未开始 |
| M7.8.4 | Runtime Hook 与 RunContext | 未开始 |
| M7.8.6 | M7.8 总验收与文档同步 | 未开始 |
| M8.1–M8.5 | LSP / MCP / ActiveToolSet / Restore / Benchmark | 未开始 |
| M9 | Task / Project Memory | 未开始（有门槛） |
| M10 | Multi-Agent（含 Session fork 与 worktree 前置） | 未开始 |

### 实施与审查规则

1. 每个编号是一批可审查的最小行为；不提前创建后续编号的接口或占位实现。代码、测试、README 与本文件状态在**同一提交**，中文 `feat/fix/docs` 消息。
2. 每批运行针对性测试、全量离线测试与 `git diff --check` 后再更新状态；真实模型结论只有执行过才能写「通过」，并记入 `docs/benchmarks/`。
3. 若改到 Agent Core / Session / Context 边界，同步 README 第 2 节与 `AGENTS.md`；发现文档与代码不一致，先修文档再继续实现。
4. 本阶段的核心不是「加更多 AI 功能」，而是在增加能力的同时保持：Agent Loop 简单、Tool 边界稳定、Context 可控、Session 可恢复、成本可观测、错误可解释。
