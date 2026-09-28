# AGENTS.md

本文件只列 mini-pi 开发时必须遵守的约束。当前状态、下一步任务与验收标准看 [Phase 3 计划](docs/plans/phase3-runtime-hardening.md)；已交付能力和使用限制看 [README](README.md)。不要把旧里程碑的实现过程复制回本文件。

## 项目边界

- 目标是用 Python 自行实现轻量 Coding Agent Harness：模型根据工具 observation 自主决定下一步，不把任务写死为 `read → edit → test`。
- 使用 Python 3.12+、uv、Pydantic v2、pytest、同步 openai SDK、Typer、Rich、prompt_toolkit 和 ripgrep-bin；仅支持 OpenAI 与 DeepSeek 的 OpenAI-Compatible API。
- 不引入 LangGraph、AutoGen、CrewAI、Dify、Coze 等 Agent 框架；不为未来功能提前引入通用插件框架、数据库、RAG、向量库或并行工具执行。
- 现阶段支持 macOS/Linux。需要 Windows、其他 Provider、MCP server、远程 transport、通用多 Agent 调度时，先有明确需求和独立设计。

## 架构与事实边界

`CLI → AgentSession → Agent → run_loop → (LLM, ToolRegistry) → Tool → Workspace`

- CLI 负责输入、参数和 AgentEvent 渲染，不决定工具、不直接调 Tool，也不把 UI 图案、spinner、用量文案或 Session 路径写入模型消息。
- Agent 持有 `AgentState` 并调用 Loop；不直接读写文件或执行 Shell。Loop 控制 LLM/Tool 循环，不读 stdin、不渲染、不依赖 JSONL。
- Session 负责装配、完整消息持久化与恢复；Context 负责项目规则、当前请求预测和压缩投影。LLM 层只处理 Provider 协议；Tool 经 Registry 调度，文件操作一律经 Workspace。
- 完整 system/user/assistant/tool 消息先由 `on_message_commit` 写盘，再进入内存；写盘失败直接停止。JSONL 原始记录不因展示或压缩被改写，tool call/result 必须配对。
- `RunContext` 只保存单次 `run()` 的临时状态。`prepare_next_turn` 仅在完整工具批次提交并结束 turn 后调用，可返回下一请求的替换投影；任务预算仍由 Loop 计算。不随意增加 Loop 分支或新钩子体系。

## Loop、错误与取消

- 终止原因是 `completed / step_limit / budget_limit / error / cancelled`；CLI 与一次性进程退出语义必须如实反映，不能把未完成或取消当成功。
- 预期的 `ToolError` 转为 `is_error` ToolMessage；LLM 预期错误编码为 `ErrorEvent`；其他异常视为程序缺陷直接冒泡，不静默兜底或自动修正参数。
- 模型输出 `length` 时不执行可能截断的工具调用；补错误 observation 保持配对。用户中断时不补假 assistant，已提交消息、工具结果和文件改动保留；被中断及未执行的工具调用补 cancelled observation。
- `bash` 超时或中断必须杀掉整个进程组；非零退出码和 stderr 如实回传。默认展示有界且脱敏，`--verbose` 也不能超出工具已捕获的内容。

## Tool、Workspace 与 Shell

- 每个 Tool 单一职责、独立文件，参数使用 Pydantic 模型；Registry 统一做名称查找与参数校验。`ToolResult.content` 回传模型，`details` 仅供 UI；`modified_files` 只列工具确知改动的 workspace 相对路径，未知不能猜。
- Workspace 路径先 `Path.resolve()` 再检查仍在 root 内；`../`、绝对路径及 symlink 逃逸直接报 `WorkspaceViolationError`，不改写为安全路径。写文件原子替换。
- `edit` 对原文精确且唯一匹配；空旧文本、无匹配、多匹配、重叠或无变化均显式失败，多项替换从后向前应用。`read`、`bash` 输出保持行数/字节上限和截断提示。
- 文件 Tool 有 Workspace 边界；`bash` 是无沙箱的本地 Shell，只固定 cwd，可访问工作区外及网络。README、CLI 和计划不得把它称为沙箱。安全边界改变需有单独设计与验证。

## LLM、Session 与 Context

- 流式 tool call 按 index 聚合，JSON 参数显式解析；已输出事件后不重试。408/409/429、5xx 和网络错误最多重试两次；认证与普通参数错误不重试。DeepSeek 的 `reasoning_content` 回放差异只放在 DeepSeekClient。
- API Key 只从环境变量或用户级 `~/.mini-pi/auth.json` 读取；不得写入项目、日志或 Session。真实认证文件和输入历史保持限制权限。
- JSONL Session 严格加载，损坏候选不能被静默跳过；恢复沿活动 parent 链构建投影。Session fork 尚未实现，需求和前置见 Phase 3。
- 每次任务前只读取 git root 到 workspace 祖先链的 `AGENTS.md`；规则变化记录 prompt section patch，不启动时扫描整个仓库。读取失败在提交用户消息前报错。
- 下一请求的窗口和任务预算使用同一批将发送的消息及工具 schema（RequestSnapshot）。Provider `usage.input_tokens` 是历史实测；当前请求预测是估算，不能把上轮 `usage.total_tokens` 当下一轮 input 真值。未知窗口不启用自动压缩。
- Compaction 只在安全切点替换可恢复的模型投影，不删原始 JSONL，不拆 tool call/result，也不丢 `modified_files`。窗口触发的摘要失败及写盘失败终止本次 run；成本触发的摘要失败只放弃优化，写盘失败仍终止。不把离线成本估算说成真实节费。

## 编码与验证

- 完整类型标注，职责明确，优先小函数与组合；无真实需求不引入 Manager、Factory、Adapter 等抽象。
- 每个方法/函数（含私有函数、property、`__init__`）写一行中文 docstring；模块级和公共类写一行中文说明。非直觉的协议、重试、截断及安全边界用中文注释解释原因；关键测试断言和特殊场景同样说明原因，不留废话或死代码。
- 默认 pytest 不联网；Agent/Loop 用 FakeLLM，文件用 `tmp_path`。真实 API 用 `@pytest.mark.integration`；`tests/integration/` 是不加 marker 的离线端到端测试。
- 代码变更做针对性测试，再运行 `uv run pytest`、`uv run ruff check .`、`uv run python -m compileall -q mini_pi` 和 `git diff --check`；真实 Provider 或人工 CLI 只记录实际执行的结果。

## 文档与提交

- README 第 8 节是里程碑状态表，Phase 3 计划记录下一步任务和验收；已完成事项保留交付物、验证与提交号，不在 AGENTS.md 维护历史流水账。
- 同一任务的代码、测试、README 与计划状态放在同一个提交；提交消息格式 `<type>: <中文说明>`，type 仅用 `feat`、`fix`、`docs`。未验证不得写“通过”。
- 修改 Agent Core、Session、Context 的设计前，读 [Pi 生产架构参考](docs/design/pi-production-architecture.md)对应章节并核对 README 第 2 节；发现文档与代码不一致，先修文档再继续。
- 历史交付与当前路线分别见 [Phase 1](docs/plans/phase1-core-runtime.md)、[Phase 2](docs/plans/phase2-session-context.md)、[Phase 3](docs/plans/phase3-runtime-hardening.md)。用户提出的明确需求优先于本文的一般开发顺序。
