# M12：最终联合验收

日期：2026-10-03。环境：macOS、Python 3.12.13。本次验证实际运行时与 CLI，
不扩展功能范围；M8 取消项及 Task/Memory/Multi-Agent 保持未实现。

## 1. 项目 review 与修复

| 已核对契约 | 当前来源与证据 |
| --- | --- |
| 单 Agent、串行执行、问询独占与真实等待 | `agent/loop.py`、`tools/ask_user.py`；问询边界测试与真实联合任务 |
| durable-first、工具配对、严格恢复 | `session/jsonl.py`、`context/projection.py`；提交失败、残缺会话、控制/目录元数据和 resume 回归 |
| 请求预算、窗口与压缩事务 | `context/request.py`、`session/runtime.py`、`agent/loop.py`；预算、切点、摘要失败和写盘失败回归 |
| 权限与路径边界 | `security.py`、`tools/registry.py`、`workspace/workspace.py`；拒绝零执行、审批先写盘、参数复制、环境隔离和路径逃逸测试 |
| 外部 stdio 生命周期 | `lsp/`、`mcp/`；错误协议、超时、断连、SIGINT、孙进程、两个固定官方 server 测试 |
| Provider、CLI 与认证分层 | `llm/`、`cli/interaction.py`、`auth.py`；离线 wire/重试、凭据权限、真实 PTY 与非 TTY 验收 |

发现并修复两项问题：

1. **P1：reload 后新环境凭据不在 CLI 脱敏表中。** 刷新前登记新凭据，并累计保留旧值，
   以保护历史详情；按值长度优先屏蔽，避免短值先替换导致长值部分泄露。
   `tests/mcp/test_runtime.py::test_cli_reload_redacts_new_secrets_before_startup` 用真实
   审批处理函数展示含新凭据的 argv，确认在启动前已脱敏、旧值仍被屏蔽。
2. **P2：SDK 先导入 MCP catalog 会触发循环依赖。** `ToolRegistry` 将目录运行时导入
   延后至装配阶段，类型导入保持 `TYPE_CHECKING`；独立 Python 子进程测试证明它
   不依赖 pytest 已预先导入 tools 的顺序。

这次 review 没有改写 Agent Core、增加框架或放宽会话恢复契约。上述源码核对和测试
证明所列契约，不代表穷尽所有缺陷或使获准进程具备沙箱隔离。

## 2. 离线与参考 server

| 检查 | 实际结果 |
| --- | --- |
| `env -u NO_COLOR TERM=xterm-256color uv run pytest -q` | **848 passed、2 skipped、5 deselected**，37.81 秒 |
| 收尾修改相关问询/审批/MCP runtime/wire | **28 passed** |
| LSP/MCP 专项（包括显式安装的固定 server） | **124 passed**；收尾修复后的两个官方参考 server 用例再次执行通过 |
| `uv run ruff check .` | 通过 |
| `uv run python -m compileall -q mini_pi` | 通过 |
| `git diff --check` | 通过 |

默认跳过的两项为需显式安装 server 的 Pyright/filesystem 用例，已另外执行。
默认排除的五项为真实 API integration 测试；本次真实 Provider 使用下述联合驱动器，
没有把这些排除项描述为通过。两个 Provider 的 MCP wire 使用实际 SDK 请求加离线
HTTP transport 验证。server 安装在临时 npm 前缀，未改全局安装或真实用户配置。

## 3. 真实 Provider 联合任务

[驱动器](m12-joint-runtime.py)使用真实 `DeepSeek / deepseek-flash`、
`pyright@1.1.414`、`@modelcontextprotocol/server-filesystem@2026.8.31`，
走生产 `AgentSession → Agent → Loop → Registry`。模型根据观察选择工具；宿主
注入的回答/审批由脚本扮演用户，按明确 fixture 目标校验后提交，不声称真人手工输入。

固定仓库包含一个舍入错误、一处类型错误和不得修改的 unittest。外部判定：

- 模型提出 half-up/bankers 问题并独占批次；回答前等待期间源码和 JSONL 字节不变。
- MCP 实际读取规则、列目录；LSP 返回跨文件定义/引用。修改前 app 有类型错误，
  修改后 app/rounding 当前内容 hash 匹配、`fresh=true`、诊断为空。
- MCP `write_file` 第一次拒绝，文件不存在；相同参数第二次单独批准才写入。
  startup、拒绝、批准均留下 ControlEntry，目录留下 ToolCatalogEntry。
- 模型执行 unittest 成功，宿主另外执行同一测试也成功；测试文件摘要未变。
- JSONL 严格 replay 通过；关闭两个 server 后均无存活 PID。当前配置禁用 MCP/LSP
  后正常 resume，真实模型继续总结，旧工具调用数不增加、原 JSONL 前缀保持不变。
- 实际 Provider key 不在最终 JSONL、控制/目录元数据或结果 JSON 中。

原始结果：[m12-joint-runtime.json](m12-joint-runtime.json)。修复任务用量为
**10 次请求、18 次工具调用、input 57,674 / output 2,906 tokens**；连同一次 resume
总结约 **18.21 秒**。用量表只统计修复任务，resume 为另一次请求；这些是单个固定
小任务的测量，不是节省成本、速度保证或全部模型稳定性的证明。

## 4. 实际终端与生命周期

[CLI 驱动器](m12-cli-driver.py)运行生产 CLI/输入/审批/Registry，模型为 FakeLLM，
LSP/MCP 为真实离线子进程；HOME、认证配置、Session 和文件全部隔离。
原始结果：[m12-cli-results.json](m12-cli-results.json)。

| 场景 | 已验证 |
| --- | --- |
| 80 列交互 PTY | 编号回答、server 审批、LSP/MCP 调用、shell 拒绝/允许、SIGINT、`/new`、EOF、resume |
| 40 列交互 PTY | 同一完整流程，提示与输入正常 |
| 80 列 NO_COLOR PTY | 同一流程，未输出前景/背景颜色 SGR |
| 非 TTY 一次性成功 | 退出 0，原始 Markdown，无 ANSI |
| 非 TTY 审批 / 问询 | 各退出 1，不消费管道作为决定，没有未经允许的副作用，严格 replay 通过 |
| 一次性 PTY 问询 EOF | 退出 130，取消结果完整配对，没有下一模型请求 |

三个交互场景均验证 shell 和 server PID 被回收；`/new` 与恢复保留原会话，新目录
重新审批启动，禁用配置的恢复不启动旧 server。环境凭据哨兵不出现在终端/Session，
MCP observation 含明确 `[REDACTED]`。临时脱敏转录位置在结果 JSON 中；不作为永久
用户数据保存到仓库。

## 5. 复现与结束边界

先显式安装固定 server（或复用已安装路径），驱动器不会自动安装。真实任务需要
已配置 DeepSeek，并会产生 API 用量；答案和审批规则写在驱动器内，仅适用于 fixture。

```sh
npm install --prefix /tmp/mini-pi-m10-pyright --no-audit --no-fund pyright@1.1.414
npm install --prefix /tmp/mini-pi-m11-reference --no-audit --no-fund @modelcontextprotocol/server-filesystem@2026.8.31
uv run python docs/benchmarks/m12-joint-runtime.py --pyright /tmp/mini-pi-m10-pyright/node_modules/.bin/pyright-langserver --filesystem /tmp/mini-pi-m11-reference/node_modules/.bin/mcp-server-filesystem --output /tmp/m12-joint-runtime.json
env -u NO_COLOR TERM=xterm-256color uv run python docs/benchmarks/m12-cli-driver.py --output /tmp/m12-cli-results.json
env -u NO_COLOR TERM=xterm-256color MINI_PI_TEST_PYRIGHT=/tmp/mini-pi-m10-pyright/node_modules/.bin/pyright-langserver MINI_PI_TEST_MCP_FILESYSTEM=/tmp/mini-pi-m11-reference/node_modules/.bin/mcp-server-filesystem uv run pytest -q tests/lsp/test_pyright.py tests/mcp/test_reference.py
```

README 安装/配置、默认关闭、权限模式和已知限制已核对，README §8、Phase 3、AGENTS
同步收尾。后续仅维护 bug 和必要兼容，不自动进入新增功能阶段。

未测项和限制：OpenAI 真实 API 未配置；本批未执行 Linux 或远端 CI。真实闭环仅一个
固定小任务，跨能力窗口自动压缩、长时/大仓库与更多 server 的量化验证未覆盖。
旧预算/压缩逻辑已离线回归。审批不是沙箱；readonly 不证明 server 无副作用。
SIGKILL 留下残缺工具批次时恢复仍严格失败；未实现跨重启等待回答或恢复一次性授权。
