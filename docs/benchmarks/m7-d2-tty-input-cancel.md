# M7.D2 tty 输入与取消记录（离线驱动，无真实模型）

本记录不调用任何真实 Provider，也不读取或改写真实凭据。所有交互都发生在一个真实 **pty** 上，因此 stdin/stdout 都是 tty，走的是 `prompt_toolkit` 分支，而不是测试里的 `CliRunner` 管道。

## 隔离方式

| 项 | 做法 |
| --- | --- |
| 模型 | 驱动脚本把 `mini_pi.cli.app.create_llm` 替换为脚本化 Fake：第一次任务直接回答，第二次任务发出一个 `bash` 工具调用 |
| 凭据 | 子进程 `HOME` 指向临时目录；同时替换 `save_last_connection`，不写真实 `~/.mini-pi` |
| workspace | 临时目录，`bash` 的 cwd 与 Session 都在其中 |
| 终端 | `pty.fork()` + `TERM=xterm-256color`；伪终端不回应 CPR 查询，子进程设置 `PROMPT_TOOLKIT_NO_CPR=1`（真实终端不需要） |

## 复现

```bash
.venv/bin/python docs/benchmarks/m7-d2-tty-driver.py /tmp/m7-d2-tty-transcript.md
```

驱动程序：`docs/benchmarks/m7-d2-tty-driver.py`。

## 记录

| 场景 | 操作 | 观察结果 |
| --- | --- | --- |
| 进入行编辑 | 启动后等待提示符 | 命中 `prompt_toolkit` 分支；`/help` 显示 `Enter submits, Ctrl+J or Alt+Enter starts a new line, Ctrl+L clears the screen` |
| 多行提交 | 输入 `line one`、Ctrl+J、`line two`、Enter | 模型收到的 user 消息只有一条，内容为 `line one\nline two`（`llm_user_messages[0] == ["line one\nline two"]`） |
| 输入历史 | 同上 | `$HOME/.mini-pi/history` 含 `+line one` 与 `+line two` 两条续行（`history_contains_multiline == true`） |
| 空闲 Ctrl+C | 在一次已完成任务后按一次 | 打印 `Press Ctrl+C again to exit.`，REPL 继续 |
| 任务中 Ctrl+C | `bash` 正在 `sleep 60` 时按 Ctrl+C | 打印 `Task cancelled by user. Committed messages and file changes were kept.`；`bash` 进程组组长在 5 秒内消失（`child_alive_after_cancel == false`） |
| 连续两次退出 | 取消任务后紧接着再按一次 Ctrl+C | 打印 `exiting mini-pi.` 并退出进程 |
| 事实保留 | 取消后读取 Session JSONL | `system` / `user` / `assistant` / `user` / `assistant(toolCalls)` / `tool(is_error=true, tool_call_id="c1")` 全部保留；被取消的 `bash` 调用有配对的 error observation，没有半截 assistant 正文 |

取消后的 Session entry（脱敏，节选；assistant 的 `tool_calls` 数组省略为 `[c1]`）：

```json
[
  {"type": "message", "role": "system"},
  {"type": "message", "role": "user", "content": "line one\nline two"},
  {"type": "message", "role": "assistant", "content": "acknowledged"},
  {"type": "message", "role": "user", "content": "run the sleep command"},
  {"type": "message", "role": "assistant", "content": "", "tool_calls": "[c1]"},
  {"type": "message", "role": "tool", "is_error": true, "tool_call_id": "c1",
   "content": "Tool execution was cancelled by the user (Ctrl+C). The command may have been terminated; re-run it if the result is still needed."}
]
```

## 边界说明

- 该记录只证明 tty 路径与取消语义；非 tty 回退、输入库缺失、Loop 事件配对与写盘顺序由 `tests/cli/test_input_reader.py`、`tests/cli/test_repl_cancellation.py`、`tests/agent/test_cancellation.py`、`tests/test_process.py` 覆盖。
- 伪终端下的按键时机与真实终端不同：等待提示符重新出现后再输入，否则按键会先进入 canonical 模式缓冲区，被行规程改写（`\r` → `\n`）。真实终端不会在提示符之间输入。
