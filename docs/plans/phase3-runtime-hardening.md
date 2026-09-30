# Phase 3：最终能力收尾

> 状态：M8 在 M8.3 后按用户要求收尾；M8.4–M8.6 取消排期，未实现。后续仅完成简单 MITL、安全控制、只读 LSP、本地 MCP 和最终验收。本文是目标设计，M9–M12 均未实现；下一批为 M9.1。

MITL 按前文 human-in-the-loop 的诉求理解为“模型提出关键问题，等待人回答后继续”。项目结束边界为一个可恢复、能问人、能查代码语义、能接受控本地工具的单 Agent CLI。原 M9 Task/Memory、M10 Multi-Agent 排期撤销；以下编号重新分配，不保留这些能力的隐含承诺。

## 1. 剩余范围与执行顺序

| 批次 | 目标 | 进入下一批的条件 |
| --- | --- | --- |
| M9.1 | 简单 MITL：`ask_user` 与 CLI 回答 | 真正等待、回答后继续，取消和无交互端不猜答案，消息配对完整 |
| M9.2 | 工具权限与外部进程启动审批 | 拒绝时无执行副作用，授权不可由模型/仓库扩大，复用 M9.1 交互 |
| M10.1 | LSP 生命周期、definition、references | 本地 Python server 查询正确，位置编码、路径和退出边界通过 |
| M10.2 | LSP diagnostics 与文件版本同步 | 编辑后的诊断来自当前内容，过期/未就绪不冒充零错误 |
| M11.1 | MCP stdio 连接与协议生命周期 | 固定本地 server 初始化、发现、调用、超时与取消清理通过 |
| M11.2 | MCP 工具装配、schema、权限与恢复 | 活动工具集有界，映射不歧义，外部工具服从同一权限检查 |
| M12 | 联合验收与项目收尾 | 四项能力闭环通过，使用方式和限制准确，然后停止新增功能 |

安全控制必须先于 LSP/MCP 对真实用户开放。每次只实现一批，代码、相关测试与必需文档同批提交；不同时搭建通用扩展框架。

明确不做：运行中任意 steering/follow-up 队列、持久化等待工作流、跨重启待答恢复、Task/Memory、Multi-Agent、Session fork、后台任务调度、全屏 TUI、LSP 重构/自动修复、远程 MCP/HTTP/OAuth、MCP resources/prompts/sampling/elicitation、OS 沙箱、自动安装 server。终验中的缺陷修复属于收尾，不重启 M8 优化研究。

## 2. 当前事实与最小接入点

| 当前源码 / 符号 | 已有行为 | 后续修改边界 |
| --- | --- | --- |
| `mini_pi/agent/loop.py::_execute_tool_calls` | 串行执行；ToolError 回传；Ctrl+C 补齐当前与剩余调用后 cancelled | 问询独占批次校验、交互不可用时结束 run；Loop 不读 stdin |
| `mini_pi/tools/registry.py::ToolRegistry.execute` | 查找工具、Pydantic 校验、执行 | 在校验后、执行前插入权限检查；扩展工具自己的参数校验入口 |
| `mini_pi/tools/base.py::Tool` | Pydantic Args、schema、execute；ToolResult 分离模型正文和 UI details | 内置工具保持默认校验；MCP 使用原始 JSON Schema 校验，不假造宽松 Args |
| `mini_pi/cli/app.py` / `cli/input.py` / `cli/console.py` | 同步 REPL，run 期间仅处理取消，没有问答通道 | 注入交互处理函数，等待时收起 Live；TTY/非 TTY 行为明确 |
| `mini_pi/session/runtime.py::AgentSession` | 组装运行时、durable-first、严格恢复与投影 | 装配交互、权限和外部连接生命周期；不把这些塞入 Provider |
| `mini_pi/session/models.py` / `context/projection.py` | 已知 entry 联合；残缺工具批次严格拒绝恢复 | 审批和工具目录使用显式已知元数据 entry；投影不发送它们给模型 |
| `mini_pi/tools/process.py::run_process/run_shell` | 一次性进程、有界捕获、超时/取消杀进程组；继承父环境 | 提供明确子进程环境；长驻 LSP/MCP 另管生命周期，不复用等结束的运行器 |
| `mini_pi/context/sections.py` / `context/request.py` | 工具描述 patch、当前 wire/schema 的请求快照 | 活动目录变化只在空闲边界安装，下一请求按实际工具重新预测 |

新增文件仅在对应批次创建：`mini_pi/interaction.py`、`cli/interaction.py`、`tools/ask_user.py`、`security.py`、`config.py`，以及小规模 `lsp/`、`mcp/` 客户端模块和各自 Tool 文件。工具执行仍经过 Registry；Agent 不读文件、启动进程或处理终端。

## 3. M9.1：简单 MITL

### 行为

- 新增 `ask_user(question, options?)`。每次一个问题，可提供 2–3 个互斥选项，也支持自由文本；选项不得隐含自动选择。空答案在 CLI 本地重新输入，不发新的模型请求。
- Prompt 明确：影响方案、数据迁移或破坏性行为且无法从仓库确认的关键选择应调用 `ask_user`。普通实现细节继续自主判断；不靠正文问号、关键词或工具名猜测是否需要人。
- `ask_user` 必须独占 assistant 的工具批次。混入 edit/bash 等时整批不执行，补齐错误 observations，要求模型单独问询；避免提问前或同批后续工具已经修改代码。
- CLI 显示问题并等待；等待期间不调用模型、不执行后续工具、不轮询 API。用户提交后由 Tool 返回真实回答，按原有 ToolMessage 提交，再继续同一 run；等待本身不消耗步骤，后续模型请求照常计数。
- CLI 提供回答 / 取消任务。Ctrl+C 或 EOF 取消并补齐工具配对；已提交消息和改动保留。非 TTY 或没有交互处理函数时，关键问询使 run 以现有 `error` 结束，并给出可交互续跑提示，不能返回空答案让模型继续猜。
- 问询与审批共用一个小型请求/响应结构及注入函数；stdin 仅由 CLI 读取，事件渲染不是回答通道。UI 的编号、色彩和提示语不写入消息。

### 恢复与限制

正常回答或取消后，resume 保留问题与真实回答/取消 observation，不再次执行旧工具。等待时遭遇 SIGKILL/崩溃可能留下已提交 assistant 与缺失结果；当前 `project_messages` 会严格拒绝，这一批不自动补答案、不猜执行状态，也不放宽其他损坏会话。支持跨重启继续等待需要独立持久化协议，本项目不做。

框架保证发起问询后等待，不保证模型识别所有需要人工判断的情形。必须阻止的操作由 M9.2 的硬权限检查处理。

**验收：** FakeLLM 断言回答前没有下一请求或副作用、回答后准确继续；混合批次零执行；取消/EOF/非 TTY/写盘失败；正常 resume、强杀后严格报错；真实 TTY 至少完成一次“问人→选方案→修复→验证”。

## 4. M9.2：最小安全控制

### 权限模式与审批

规划 CLI 参数 `--permissions workspace|readonly`，默认 `workspace`；这是工具权限模式，不是系统沙箱。

| 操作 | workspace | readonly |
| --- | --- | --- |
| ask_user | 允许，需要交互端 | 允许，需要交互端 |
| 内置 read/search/git_diff/git_status | 允许，保持 Workspace 边界 | 允许 |
| 内置 write/edit | 允许，仅 Workspace 内 | 拒绝 |
| bash 任意命令 | 每次审批 | 拒绝 |
| 受信任配置的只读 LSP 查询 | 启动批准后允许 | 启动批准后允许 |
| MCP 工具 | 本地策略标明可信只读的才自动允许，其他每次审批 | 仅允许本地策略明确标为可信只读的工具 |
| 新 LSP/MCP 进程 | 启动前审批 argv、cwd 和环境变量名称 | 同样审批 |

- 检查点在 Registry 参数校验后、实际 execute 前；没有批准时不启动命令、不写文件、不发送 `tools/call`。权限层覆盖全部模型发起工具，也覆盖外部进程启动。
- 审批只提供“允许本次 / 拒绝本次 / 取消任务”。拒绝本次返回明确 `is_error` observation，模型可以改做已允许的工作；取消任务停止整轮，补齐剩余调用。没有输入能力时需要审批的操作使 run 以 `error` 结束，不默认同意。
- 审批 UI 展示真实工具名、完整待执行参数和 cwd，秘密值脱敏且不可回显；较长内容可展开，不能只展示可能掩盖行为的简短标题。授权绑定工具身份、参数、cwd 和当前配置，检查后参数不得替换；不做命令字符串黑名单、自动“低风险 shell”猜测或全局自动批准。
- 权限模式和 server 配置来自用户级 `~/.mini-pi/config.json` 与显式 CLI 设置。配置采用严格模型；仓库 `AGENTS.md`、模型回答和 MCP 描述不能启用 server 或改变权限。server 用 argv 启动，不拼 shell，不发现即自动运行。
- 子进程使用最小显式环境，Provider API Key 不随父环境透传；server 只通过配置中的环境变量引用得到必要凭据。配置/Session/审批日志不保存凭据值，输出先脱敏再展示。该措施不阻止获准进程读取用户文件或联网。
- 持久化会话中，审批决定在副作用前写入已知 `ControlEntry`：工具身份、参数/配置指纹、一次性 allow/deny。由 Session 注入记录函数，Registry/Tool 不读取 JSONL；它是宿主审计事实，不进入模型消息，也不恢复成授权；写入失败不执行。`--no-session` 保持内存模式，明确没有持久审计。

新 entry 的解析与投影同时修改：新实现兼容旧 JSONL；旧实现遇到不认识的 entry 明确拒绝，未知 entry 仍不得静默跳过。原文件不改写，审批元数据不能插入 Provider 消息而破坏工具配对，compaction 保留这些事实且不把它们当摘要正文。

### 边界

文件工具仍由 Workspace 限界。获准 bash、LSP 和 MCP 进程可接触工作区外与网络；readonly 只限制客户端发起哪些操作，无法证明 server 或 git 子进程没有副作用。MCP 的 `readOnlyHint` 等 annotation 不能作为自动授权依据；由用户侧策略明确指定可信工具。[MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)要求对不可信来源的 annotations 保持不信任。

**验收：** 允许、拒绝、取消、非 TTY、直接 Registry 调用、同批多调用、参数变更、readonly；启动被拒绝时零进程；模型/仓库/外部描述无法修改权限；未显式配置的秘密不进入子环境，显式提供的凭据不进入审计/终端；审批写入失败时零执行；元数据不影响配对、usage、压缩切点与摘要正文，恢复和新 Session 不继承一次性授权。额外用真实 shell 向临时标记文件写入，证明拒绝后文件不存在。

## 5. M10：只读 LSP

### M10.1：连接、定义和引用

- 第一套验收环境选择 Python + 手动安装并固定版本的 Pyright language server；只配置一个 workspace、一个 server，不实现自动下载和全语言发现。默认关闭，用户显式配置后，第一次查询时审批并启动。
- 实现 LSP 3.17 所需的 stdio 子集：Content-Length 分帧、请求 id、initialize/initialized、声明能力、超时、通知、有界 stderr、shutdown/exit 和进程组清理。配置/控制请求按已声明能力处理；不支持的请求明确返回协议错误，不能挂起等不到回应。
- 注册 `lsp_definition(path, line, column)`、`lsp_references(path, line, column)` 两个工具。面对模型使用 1-based 行与 Unicode 字符列；客户端验证范围并转换成协商后的 LSP 编码，至少支持默认 UTF-16。中文和 emoji 不能按字节列直接传入。[LSP Position 规范](https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/types/position.md)
- 输入通过 Workspace，结果支持 Location/LocationLink，输出有界且标记截断；工作区外目标明确标为受限，不读取内容，不改写为工作区内路径。查询方法只读不代表 server 进程受 Workspace 隔离。
- 不注册 rename/codeAction/executeCommand，不执行 server 发来的 workspace/applyEdit。Query 的能力不存在时明确报错，模型自行选择 search/read，不静默冒充成功。

**验收：** 离线假 server 覆盖分帧、交错通知/请求、初始化失败、中文/emoji、非法位置、外部 URI、超大结果、超时与崩溃；真实 Pyright 在固定小仓库中返回正确跨文件定义与引用。断连不自动重放任何业务请求，下一次显式调用可重新启动并审批。

### M10.2：诊断与同步

- 新增 `lsp_diagnostics(path)`，仅针对显式查询的文件获取诊断；按协商的 textDocumentSync 能力发送 didOpen/didChange/didClose，并维护文档版本。
- 每次查询前检查磁盘实际内容并同步，不能只依赖 modified_files：bash 或用户也可能改文件。跟踪已打开文件即可，不扫描全仓库、不建设文件监听平台；诊断工具报告内容版本/指纹和是否确认新鲜。
- 支持 publishDiagnostics 的受限缓存或 server 已声明的 pull diagnostics。旧版本结果不得当作当前诊断；缺少 version 时采用保守的失效与等待规则，无法确认当前结果便返回未就绪错误。空诊断只有在确认当前结果后才能报告。
- Session/CLI 管理长驻连接；退出、`/new` 替换运行时和启动失败都关闭旧资源。单次任务取消清理活动请求，协议状态不确定时关闭进程，下次重新初始化。不得每次请求重启 server；不得把一次性 `run_process` 当长驻客户端。

**验收：** edit/write/bash/外部直接修改后诊断更新；错误消失、新错误、延迟通知与过期缓存；查询/取消/`/new`/退出无遗留子进程；真实 server 完成“查定义→编辑→当前诊断→测试”。诊断并不替代任务外部测试。

## 6. M11：本地 MCP 工具

### M11.1：stdio 客户端

- 第一版仅本地 stdio、tools 能力。优先使用官方 [Python SDK](https://github.com/modelcontextprotocol/python-sdk)，固定依赖与支持版本，按 [Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) 协商；不支持 server 返回版本时断开，不标榜支持整个最新规范。
- 同步 Agent 外围持有一个受管的异步 SDK 生命周期，提供串行调用入口；不为每次请求新建 event loop，也不让 Loop/Provider 改为异步调度。LSP 与 MCP 不强行共用分帧实现：MCP stdio 是换行分隔消息。[Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- 从用户配置读取明确 server id、argv、cwd、必要 env 引用与工具白名单，启动必须经过 M9.2。支持 initialize、initialized、tools/list 分页、tools/call、超时/断连、关闭 stdin 后限时退出与进程组清理。
- 未声明 sampling/elicitation 等能力；server 请求这些功能时明确拒绝，不代它调用模型或提示用户授权。通知和 stderr 有界，协议 stdout 混入普通日志视为协议错误。
- 用固定本地测试 server 覆盖协议；正式接入验收还需一个手动安装、固定版本的参考 stdio server。未配置时默认工具集保持原样。

**验收：** 真实子进程协议往返、分页、版本/能力不匹配、错误 id、日志污染、卡死/取消/崩溃、启动拒绝、环境秘密隔离与进程清理；不自动重试或重放可能有副作用的 tools/call。

### M11.2：装配与恢复

- 工具仅按明确白名单启用，数量/描述/schema 大小受限；一次 run 的目录冻结。提供 CLI `/tools` 查看启用情况及来源，`/mcp reload` 仅在空闲时显式刷新；运行中的 list_changed 只标记需要刷新。
- 为外部 `(server_id, original_tool_name)` 建立稳定 Provider 别名与反向表；按两个现有 Provider 的名称约束验证，点号/长名称用稳定编码与摘要处理，碰撞明确失败。不能通过拆字符串或截断名称猜回原工具。
- 现有内置工具继续使用 Pydantic；MCPTool 覆盖 `schema()` 与参数校验入口，使用受支持 JSON Schema 方言验证原始参数。只解析本地引用，不联网取 `$ref`；不支持/不适用于 Provider 的 schema 对该工具显式报错，不删 required、不随意放宽。
- 文本块与 structuredContent 转成稳定、有界的 ToolResult；提供 outputSchema 时验证结构；`isError` 转成错误 observation。图片/音频/资源块暂不支持时明确报告，不能静默丢掉后声称成功，不自动打开 URI。外部工具的 modified_files 默认为未知，不猜路径。
- MCP Tool 通过同一个 Registry 权限检查；annotations、server instructions 和工具描述是外部资料，不能自动提升为宿主规则、批准、执行代码或修改配置。描述标明来源，不拼入 Working rules；宿主保护的是权限边界，不能宣称模型完全免疫 prompt injection。
- Session 追加已知 `ToolCatalogEntry` 保存有效目录身份、原名/别名、schema 与配置指纹等非秘密事实。它不进入模型消息；tools section 变化按既有 patch 提交，当前 registry.schema 进入下一请求预测。外部配置、凭据和一次性批准不从 JSONL 恢复。
- resume 先恢复历史调用原貌，再按当前用户配置装配；历史目录不会启动已移除 server。旧结果不重新调用远端，已禁用别名的新调用返回错误；当前工具/schema 的变化在新 user 消息前提交，禁止把旧别名绑定到另一工具。

**验收：** 点号/超长/碰撞名称、参数错误和本地 refs、structuredContent/outputSchema/isError、不支持内容、有界输出、审批拒绝零调用；禁用/刷新/schema 变化、目录元数据与 compaction、旧 JSONL 读取、resume 不重放；对两个 Provider 做 wire 测试，并记录新增工具数/schema 的每请求 Token 估算与可用 Provider 实测差异，不承诺效率收益。

## 7. M12：验收通过后结束项目

1. **离线回归：** 四项能力的边界用例、旧会话兼容、取消、配对、durable-first、预算/压缩原有回归，全量 pytest、Ruff、compileall、diff 检查通过。
2. **真实闭环：** 固定小仓库中完成问询选方案、LSP 定位/诊断、权限拒绝与批准、本地 MCP 调用、实际修改和任务后外部测试。真实运行至少一个已配置 Provider；另一 Provider 的协议兼容用离线测试验证，未配置的真实 API 如实列为未测。
3. **生命周期与 CLI：** 交互/一次性/非 TTY，40/80 列、NO_COLOR、取消、EOF、`/new`、正常 resume；长驻进程无遗留，秘密不出现在宿主输出或元数据。
4. **交付：** README 写清安装配置、默认关闭的外部能力、权限模式和所有限制；AGENTS 只保留有效约束；详细验收证据沿用 `docs/benchmarks/`。不声称审批等于沙箱，不声称强杀后的残缺工具轮可恢复。

M12 达标即视为本项目完成，后续仅修 bug 和必要兼容问题。没有自动进入下一阶段的扩展清单。

## 8. 已完成与已收尾的基线

M8 的成本优化评审和 CLI 设计已经落在源码、测试及下列评测中；候选未采纳不等于对应试验没有完成。取消 M8.4–M8.6 是本次范围决策，不是完成声明。

| 里程碑 | 已交付事实与依据 |
| --- | --- |
| M7.8 | 请求预测、窗口与成本触发、取消/持久化；[验收](../benchmarks/m7-8-final-acceptance.md) |
| M7.9.1–M7.9.3 | 完成语义、git_status、真实任务与规模；[基线](../benchmarks/m7-9-baseline.md) |
| M7.9.4 | CLI 视觉整理；[验收](../benchmarks/m7-9-4-cli-visual.md) |
| M8.0 | 固定任务、外部判定与真实运行；[基线](../benchmarks/m8-0-eval-baseline.md) |
| M8.1 | 紧凑启动、TTY Markdown、空白输入与续读；[验收](../benchmarks/m8-1-cli-visual.md) |
| M8.2 | 合批 Prompt 对照完成，候选未采纳；[结果](../benchmarks/m8-2-prompt-batching.md) |
| M8.3 | 修正区域估算；8k 候选未采纳，默认 20k；[结果](../benchmarks/m8-3-context-cost.md) |
| M8.4–M8.6 | 预算维度、重复验证与推理参数的后续研究取消，不继续排期 |

设计继续遵循 [README 第 2 节](../../README.md#2-从-pi-学到的设计取舍)与 [Pi 生产架构参考](../design/pi-production-architecture.md)：单 Agent、串行工具、JSONL 不改写、工具调用/结果配对、宿主与 Provider 分层、非预期异常冒泡。里程碑落地时更新 README 第 8 节和本计划状态，不能把目标设计写成已实现行为。
