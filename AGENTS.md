# AGENTS.md

本文件用于约束所有 AI Coding Agent 在 `mini-pi` 项目中的开发行为。

---

## 1. 项目目标

`mini-pi` 是一个使用 Python 从零实现的轻量级 Coding Agent Harness。

参考 Pi / Claude Code 一类 Coding Agent 的架构思想，但不照搬实现。

核心目标：

```text
Python
+
OpenAI / DeepSeek
+
Tool Calling
+
Agent Loop
+
Workspace
+
Code Tools
```

最终逐步扩展：

```text
Session
Context
LSP
MCP
Task
Memory
Multi-Agent
```

---

## 2. 技术边界

### 必须使用

```text
Python 3.12+
uv
Pydantic v2
pytest
openai SDK（同步 client）
Typer
Rich
ripgrep-bin（search 工具内置 rg，Python 扫描兜底）
OpenAI-Compatible API
```

### 模型 Provider

当前只支持：

```text
OpenAI
DeepSeek
```

不要主动扩展：

```text
Anthropic
Gemini
Ollama
其他 Provider
```

除非明确提出需求。

---

## 3. 禁止引入 Agent 框架

禁止使用：

```text
LangGraph
AutoGen
CrewAI
Dify
Coze
```

Agent Loop、Tool Dispatch、State、Session 必须由项目自己实现。

不要因为框架能少写代码就引入框架。

本项目本身就是为了理解和实现 Agent Harness。

---

## 4. 架构职责与依赖方向

模块职责：

```text
CLI (mini_pi/cli)
→ 用户交互、参数解析、AgentEvent 渲染

Agent (mini_pi/agent/agent.py)
→ 持有 AgentState，调用 run_loop，禁止直接读写文件 / 执行 Shell

Agent Loop (mini_pi/agent/loop.py)
→ 纯函数 run_loop(state, llm, registry, max_steps, on_event)
→ 控制 LLM / Tool 循环，发出 AgentEvent

LLM Client (mini_pi/llm)
→ 调用模型，统一消息与流式事件，错误编码进事件流

Tool Registry (mini_pi/tools/registry.py)
→ 注册、schema 校验、调度工具

Tool (mini_pi/tools/*.py)
→ 执行具体动作，只依赖 Workspace，不依赖 Agent

Workspace (mini_pi/workspace/workspace.py)
→ 路径解析与边界控制、文件读写

Session (第二阶段)
→ 管理一次会话生命周期

Context (第二阶段)
→ 管理模型上下文
```

依赖方向严格单向：

```text
CLI → Agent → (LLM, ToolRegistry) → Tool → Workspace
```

不要把多个职责塞入单个类。

尤其禁止：

```text
Agent 直接读写文件
Agent 直接执行 Shell
CLI 直接调用 Tool
Tool 直接控制 Agent Loop
Tool 绕过 Workspace 直接 open()
```

---

## 5. 第一阶段范围

当前阶段优先完成：

```text
LLM Client
Agent State
Agent Loop
Tool Registry
Workspace
CLI

read
write
edit
search
bash
git_diff

pytest
```

不要主动加入：

```text
MCP
LSP
Memory
Multi-Agent
SubAgent
Planner
Reviewer
Web UI
IDE Plugin
RAG
Vector DB
asyncio 并行工具执行
```

除非已经完成第一阶段闭环（M1-M6 全部完成）。

---

## 6. Agent Loop 与事件模型

核心循环：

```text
LLM
 ↓
Tool Call
 ↓
Tool
 ↓
Observation
 ↓
LLM
 ↓
...
```

不要将开发流程写死为：

```text
read → edit → test
```

模型应该根据当前 Observation 自主决定下一步 Tool。

### 循环结构

第一阶段单层循环，不引入 steering / follow-up 队列（属于 Session 阶段）：

```python
while steps_this_run < max_steps:
    assistant = 流式生成并聚合
    if assistant.stop_reason == "error": 结束（reason=error）
    if not assistant.tool_calls: 结束（reason=completed）
    if assistant.stop_reason == "length": tool call 全部转 error observation，下一轮
    for call in assistant.tool_calls: 执行并追加 ToolMessage
```

### 终止条件

```text
任务完成（无 tool call）
LLM 错误（stop_reason == error）
达到 max_steps（默认 50）
```

### 事件

Loop 通过 `on_event: Callable[[AgentEvent], None]` 发出事件，CLI 是纯消费者：

```text
agent_start / turn_start / message_start / message_delta
message_end / tool_execution_start / tool_execution_end
turn_end / agent_end(reason: completed | step_limit | error)
```

规则：

- Loop 不做渲染、不读 stdin
- CLI 不参与决策、不直接调用 Tool
- `on_event` 为可选参数，测试时传 None 或列表收集器

---

## 7. Tool 规范

所有 Tool 必须：

1. 单一职责
2. 独立文件
3. 有清晰 name
4. 参数用 pydantic 模型结构化声明
5. 返回结构化结果
6. 可预期错误抛 `ToolError`，非预期异常直接冒泡
7. 可单独写 pytest
8. 文件操作必须经过 Workspace

接口：

```python
class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]

    def schema(self) -> ToolSchema: ...

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult: ...
```

Tool Registry：

```python
registry.register(tool)
registry.schemas()
registry.execute(name, arguments)
```

Registry 职责：

```text
未知工具      -> ToolNotFoundError
参数校验失败  -> ToolArgumentError
校验通过      -> tool.execute(**validated)
```

ToolResult 协议：

```text
ToolResult(content, details=None, modified_files=[])
content        回传模型
details        仅供 UI / 日志
modified_files 必须显式列出本工具改动的 workspace 相对路径；只读工具留空
```

禁止在 Agent Loop 中堆：

```python
if tool_name == ...
elif tool_name == ...
elif tool_name == ...
```

---

## 8. Workspace 安全边界

所有文件操作必须限制在 workspace 内。

```python
class Workspace:
    def resolve(self, path: str | Path) -> Path: ...
    def read_text(self, path: str | Path) -> str: ...
    def write_text(self, path: str | Path, content: str) -> Path: ...  # 原子写
    @property
    def root(self) -> Path: ...
```

`resolve` 语义：

```text
相对路径 -> root / path
绝对路径 -> 仅当 resolve 后仍在 root 内才允许
统一 Path.resolve()（跟随 symlink）后校验 is_relative_to(root)
越界 -> WorkspaceViolationError（ToolError 子类）
```

需要防止：

```text
../
../../
绝对路径逃逸
符号链接逃逸
```

错误路径应直接报错。

不要自动改写成“安全路径”。

不要静默 fallback。

Fail Fast。

---

## 9. Shell Tool

Shell Tool 必须：

```text
cwd = workspace root
timeout（默认 120s，上限 600s，>0）
capture stdout
capture stderr
return exit code
```

- 非 0 exit code 必须如实返回给 Agent（属于正常 Observation，不抛 ToolError）
- timeout 必须杀掉整个进程组（`start_new_session=True` + `os.killpg`）
- stdout / stderr 分离捕获，进程层有界（默认 1MB/流，超出按 head/tail 方向丢弃并标记），工具层再按 2000 行 / 50KB 双限截断并附提示
- 返回结构化 `ProcessResult(exit_code, stdout, stderr, timed_out, stdout_truncated, stderr_truncated)`

禁止：

```text
吞掉 stderr
强制返回 success
自动重试所有命令
```

危险操作的人工确认机制可以后续加入。

---

## 10. 文件修改

第一阶段：

```text
write   新文件或整文件重写，原子写，自动建父目录
edit    对原文精确匹配、唯一匹配、多 edit 不重叠
```

后续可升级：

```text
apply_patch
```

edit 规则：

```text
old_text 为空            -> ToolArgumentError
匹配次数 == 0            -> ToolArgumentError
匹配次数 > 1             -> ToolArgumentError（要求提供更多上下文）
多个 edit 范围重叠        -> ToolArgumentError
替换后内容无变化          -> ToolArgumentError
所有 edit 相对原始文件匹配，从后往前应用
成功返回 diff 到 details
```

原则：

```text
优先最小修改
避免整文件无意义重写
修改后允许 Agent 验证
```

验证方式：

```text
git diff
pytest
build
lint
```

---

## 11. LLM 层

LLM Provider 与 Agent Runtime 解耦。

结构：

```text
BaseLLMClient
├── OpenAIClient
└── DeepSeekClient
```

统一接口：

```python
class LLMClient(Protocol):
    def stream(self, messages, tools=None) -> Iterator[StreamEvent]: ...
    def complete(self, messages, tools=None) -> AssistantMessage: ...
```

规则：

```text
同步 SDK + stream=True
tool call 增量按 index 聚合，结束后显式解析 JSON（非法 JSON 报错，禁止静默 {} 兜底）
错误编码为 ErrorEvent，不裸抛给 Agent Loop
429 / 5xx / 网络错误最多重试 2 次（指数退避），400 / 401 / 403 不重试
重试仅允许发生在尚未产出任何事件之前；已开始输出不重试
DeepSeek 差异只允许出现在 DeepSeekClient：base_url、API Key 环境变量、reasoning_content 回放
```

凭据管理：

```text
启动选择顺序：显式 --provider/--model > auth.json 中上次成功连接 > 内置默认值
解析顺序：环境变量 > ~/.mini-pi/auth.json（目录 0700 / 文件 0600）
交互式通过 /connect 选择 provider、隐藏输入 Key、真实请求验证后原子保存 Key 与 provider/model
禁止把 Key 写入项目目录、日志或提交到 git
```

Agent 不应包含 Provider-specific 逻辑。

---

## 12. State

第一阶段状态保持简单：

```python
@dataclass
class AgentState:
    messages: list[Message]
    step_count: int = 0
    modified_files: set[str] = field(default_factory=set)
```

`step_count` 为会话累计值；`max_steps` 是单次 `run()` 的限制，不是累计限制。

不要提前加入几十个状态字段。

没有真实需求，不建状态。

---

## 13. Session

Session 在第二阶段（M7）引入。

职责：

```text
session_id
messages
model
cwd
config
context
save
resume
```

采用 JSONL entry 链（参考 pi SessionManager 最小子集）：

```text
header: {type: session, version, id, timestamp, cwd}
entry:  {id, parentId, timestamp, type, ...}
type:   message | compaction
```

- 追加即以当前 leaf 为 parent，再前移 leaf
- resume = 读取 entries + 沿 parentId 回放
- fork 留到后续

M7 的详细设计、子里程碑与验收标准见 [`docs/plans/phase2-session-context.md`](docs/plans/phase2-session-context.md)。

不要第一阶段就引入数据库。

---

## 14. Context

不要启动时扫描并把整个仓库塞进模型。

优先：

```text
System Prompt
AGENTS.md
User Task
少量环境信息
```

然后 Agent 自己通过：

```text
search
read
lsp
```

按需拉取上下文。

Context Compaction 放在第二阶段：

```text
阈值：tokens > context_window - reserve
切点：只切 user / assistant 消息，绝不拆散 tool_call / tool_result 配对
摘要：单次 LLM 调用，固定模板；失败视为压缩失败
时机：turn 边界
```

---

## 15. MCP / LSP

MCP、LSP 都是后续能力，不属于 Agent Core。

结构上保持：

```text
Agent
  ↓
Tool
  ↓
MCP / LSP Adapter
```

不要让 Agent 直接依赖 MCP 或 LSP。

---

## 16. Multi-Agent

Multi-Agent 属于后期能力。

只有在以下场景再引入：

```text
角色职责明显不同
上下文需要隔离
任务可以并行
需要独立 Review
```

不要为了简历关键词提前设计多 Agent。

---

## 17. Fail Fast 与错误分类

本项目明确偏好 Fail Fast。

```text
ToolError（ToolNotFoundError / ToolArgumentError / WorkspaceViolationError）
→ 可预期失败，Agent Loop 转成 is_error ToolMessage 回传模型

LLMError
→ LLM 层可预期失败，编码进 ErrorEvent

其他异常
→ 程序缺陷，直接冒泡，不捕获、不兜底
```

不要：

```text
吞异常
静默 fallback
随意默认值
自动修正错误参数
宽松校验
```

如果：

```text
Tool 参数错误
路径错误
配置错误
模型返回非法结构
```

应明确报错。

只有业务规则明确要求时才允许兜底。

---

## 18. 编码风格

```text
Python 3.12+
完整类型标注
小函数
明确职责
显式依赖
优先组合
避免深层继承
```

### 注释规范

编写代码时必须附带简要中文注释：

```text
模块级、公共类/公共函数：一行中文 docstring 说明职责
非直觉逻辑（边界处理、协议细节、重试/截断/路径安全等）：必须行内注释
注释解释“为什么”，不逐行翻译“做了什么”
禁止废话注释（如 # 返回结果）与注释掉的死代码
测试中的关键断言与非显然场景应有中文注释
```

不要为了“企业级”增加无意义抽象。

禁止无需求引入：

```text
Manager
Factory
AbstractFactory
Adapter
Facade
Repository
Service
```

如果一个普通函数能解决，就用普通函数。

---

## 19. 测试

使用 pytest。

优先测试：

```text
Agent Loop
Tool Calling
Tool Registry
Workspace 路径逃逸
Shell Timeout
File Edit
截断策略
异常处理
LLM Mock
事件序列
```

约定：

```text
Agent / Loop 测试使用 FakeLLMClient（脚本化事件流），不调用真实 API
真实 API 测试标记 @pytest.mark.integration，默认通过 addopts 排除
文件测试使用 tmp_path，不触碰真实项目文件
禁止测试依赖网络
```

测试命令：

```bash
uv run pytest
uv run pytest -m integration   # 需要 API Key
```

---

## 20. 开发顺序

```text
M1 LLM 调通
M2 Tool Calling
M3 Agent Loop
M4 文件 / Shell Tool
M5 真实代码修改闭环
M6 pytest 完善
M7 Session / Context
M8 LSP / MCP
M9 Task / Memory
M10 Multi-Agent
```

不要跨阶段同时开太多功能。

每个里程碑的详细任务拆解见 `docs/plans/phase1-core-runtime.md`。

---

## 21. Phase 1 完成标准

以下流程稳定跑通：

```text
用户提出代码任务
  ↓
Agent 搜索代码
  ↓
读取相关文件
  ↓
定位问题
  ↓
修改代码
  ↓
执行测试 / 构建
  ↓
观察失败
  ↓
继续修复
  ↓
验证成功
  ↓
输出最终总结
```

如果只是：

```text
User → LLM → Answer
```

这不是 Coding Agent。

如果只是：

```text
User → 固定 Workflow
```

也不是本项目目标。

目标是：

> 由模型在 Agent Loop 中根据当前上下文和 Tool Result 自主决定下一步行动。

---

## 22. 文档、计划与提交同步（强制）

- `README.md` 的路线图状态表必须与实际情况一致
- `docs/plans/phase1-core-runtime.md` 中未完成任务的复选框必须随开发进度勾选
- 已完成的任务与里程碑在计划文档中精简为「交付物 + 验收（含提交号）」摘要，删除完整代码块与步骤
- 完成一个里程碑后：运行测试 → 更新 README 状态 → 勾选计划任务
- git commit message 统一使用 `<type>: <中文说明>`，type 只使用：
  - `feat`：新增功能，例如 `feat: 实现项目规则发现`
  - `fix`：修复缺陷，例如 `fix: 修复符号链接越界校验`
  - `docs`：纯文档变更，例如 `docs: 细分 M7 审查任务`
- 同一任务的代码、测试、README 与计划状态必须合并到同一个提交，禁止再拆成“代码提交 + 文档提交”
- 包含代码变更时按实际目的使用 `feat` 或 `fix`；只有不含代码的纯文档任务才使用 `docs`
- 设计决策发生变化时，同步更新 `README.md` 第 2 节（设计取舍）与本文件
- 禁止在没有实际验证的情况下声称测试通过

---

## 23. 参考文档（开发时必读）

项目开发时必须参考以下文档，架构与实现取舍以它们为准：

```text
docs/design/pi-production-architecture.md   Pi 生产架构参考（Agent/AgentSession/Loop/Tool/Session 链路）
docs/plans/phase1-core-runtime.md           Phase 1（M1-M6）交付物与验收
docs/plans/phase2-session-context.md        Phase 2（M7）设计与子里程碑；M8-M10 准入条件
README.md 第 2 节                            与 pi 的设计取舍对照
```

- 新增/修改 Agent Core、Session、Context 相关设计前，先读 `pi-production-architecture.md` 对应章节，确认与 pi 的差异是有意为之
- 实现过程中如发现文档与代码不一致，先更新文档再继续，不允许“文档写一套、代码做一套”
