# M7.9.4 CLI 视觉验收

2026-09-28，基线 `ea4d22a`。运行
`uv run python docs/benchmarks/m7-9-4-tty-driver.py <out_dir>` 可重放：真实 pty、脚本化
模型、临时 HOME，不联网，不触碰真实凭据、Session 与输入历史。本次运行 `failed checks: none`。

覆盖：80 列启动页/折叠任务流/`/last`/状态页、40 列窄屏、`TERM=dumb`、`NO_COLOR` +
`--no-banner`、非 tty 一次性输出、任务中 Ctrl+C。全部状态标记（ToolError、shell 非零、
超时、截断、预算、`step_limit`、取消）的展示契约由 `tests/cli/test_visual_layout.py` 与
`tests/test_console.py` 覆盖；本记录只证明真实终端形态下的布局与降级行为。

## 两种工具展示模式

| 终端 | 行为 |
| --- | --- |
| tty（默认） | 单行活动区显示动画帧 + 当前动作 + 已折叠计数；**成功的工具调用不占滚动区**，失败照旧逐条保留；run 结束给出 `/last for the N collapsed tool calls` |
| tty + `--verbose` | 活动区保留，同时逐行打印参数与有界输出 |
| `TERM=dumb` / 非 tty | 不跑活动区也不折叠：逐行事件是日志唯一能保证的形态 |

`/last [full]` 从 Session 活动链（纯内存模式则从消息）重建最近一次 run 的工具调用：标题按
assistant 的 `tool_call` 参数还原，结果按 `ToolMessage` 的 `content`/`is_error`/`modified_files`
还原（`details` 不落盘，因此不假装有它；shell 非零从 observation 首行读回）。折叠不丢事实：
完整清单在 Session JSONL 与 `/last` 里。

转录说明：`⟨activity⟩ …` 是同一行反复重绘的最终状态（帧每秒刷 8 次，原文会淹没转录）；
`✗ Run grep · shell exited 1` 是失败调用，永久留在滚动区。

## 思考指示器的硬约束

指示器必须是**单行、帧宽恒定**：Live 区域清除时按渲染高度回退光标，高度为 1 时正好清掉那一行。
此前用的 18 行 ASCII 图案（`assets/thinking.txt`，最宽 27 列）会触发 `vertical_overflow`
裁剪，且停止前会以 `visible` 再渲染一次全高，光标回退量与屏幕内容不一致，图案就留在滚动区、
每轮叠一次（用户截图里反复出现的 `db db / d88 88 / …` 即此）。因此改为 `style.THINKING_FRAMES`
的 4 帧单格宽序列（`◜ ◝ ◞ ◟`，8 fps，`vertical_overflow="crop"`），并用
`test_thinking_frames_cycle_at_constant_width` 钉住「帧宽守恒 + 每个字形单格宽」。

## 80 列：折叠视图与 /last 展开

```text
   /                       \
 /X/                       \X\
|XX\         _____         /XX|
|XXX\     _/       \_     /XXX|___________
 \XXXXXXX             XXXXXXX/            \\\
   \XXXX    /     \    XXXXX/                \\\
        |   0     0   |                         \
         |           |                           \
          \         /                            |______//
           \       /                             |
            | O_O | \                            |
             \ _ /   \________________           |
                        | |  | |      \         /
  No Bullshit,          / |  / |       \______/
   Please...            \ |  \ |        \ |  \ |
                      __| |__| |      __| |__| |
                      |___||___|      |___||___|
  牛人，就用牛的 coding agent！

mini-pi 0.1.0 · openai/gpt-5.6-terra
work · session 8b0dd804                                       /help for commands


 › inspect and fix the failing test
⟨activity⟩  Thinking…
⟨activity⟩  Read app.py
⟨activity⟩  Thinking… · 1 tools done
⟨activity⟩  Run grep · 1 tools done
✗ Run grep · shell exited 1
⟨activity⟩  Thinking… · 1 tools done
⟨activity⟩  Edit app.py · 1 tools done
⟨activity⟩  Thinking… · 2 tools done
⟨activity⟩  Run python3 · 2 tools done
⟨activity⟩  Thinking… · 3 tools done
add 现在返回 a + b，tests 通过。
Completed · 4 tools · 5 requests · provider usage unavailable · 0.2s
/last for the 3 collapsed tool calls

 › /last
Last run tool calls (4):
✓ Read app.py · completed
✗ Run grep · exit_code: 1
✓ Edit app.py · 1 file(s) changed
✓ Run python3 · exit_code: 0

 › /status
Model           openai/gpt-5.6-terra
Workspace       work
Session         8b0dd804
Context         ~2,080 / 1,050,000 tokens (0.2%)
Last run        unavailable (0/5 requests reported usage)
Budget          disabled
Compaction      auto at 1,041,808 tokens (window 1,050,000)

 › /context
System / rules  ~388 tokens
AGENTS.md       ~0 tokens
Conversation    ~87 tokens
Tool results    ~56 tokens
Summaries       ~0 tokens
Tool schemas    ~1,365 tokens
Total           ~1,896 / 1,050,000 tokens (0.2%)

Next request    ~2,080 tokens
Last request    unavailable (provider returned no usage)
Last run        unavailable (0/5 requests reported usage)

Compaction      window threshold 1,041,808 tokens (window 1,050,000 - reserve
8,192)
                cost-aware early compaction for old tool results is enabled

 › /tools
read        Read a UTF-8 text file inside the workspace. Supports offset/limit
paging and reports a continuation hint when truncated.
write       Create or fully rewrite a file inside the workspace. Parent
directories are created automatically and writes are atomic.
edit        Apply exact, unique text replacements to an existing file. All edits
match the original content and must not overlap. Returns a unified diff.
search      Search text or regex across workspace files and return
file:line:text matches. Skips .git/.venv/node_modules and binary files.
bash        Run a shell command in the workspace root. Returns exit code, stdout
and stderr. Non-zero exit codes are reported, not raised.
git_diff    Show uncommitted changes in the workspace git repository as a
unified diff.
git_status  Show the git working tree status of the workspace: branch plus
staged, unstaged, untracked and conflicted paths. Read-only; use git_diff to
inspect the content of those changes.

 › /sessions
Saved sessions for work:
* 8b0dd804 · 2026-09-28 05:36:24Z · openai/gpt-5.6-terra · summary no
* current session
--continue resumes the latest validated session; --resume <session.jsonl>
resumes an exact file

 › run a long command
⟨activity⟩  Thinking…
⟨activity⟩  Run sleep
✗ Run sleep · failed: Tool execution was cancelled by the user (Ctrl+C). The
command may have been terminated; re-run it if the result is still needed.
✗ Task cancelled by user. Committed messages and file changes were kept.
Cancelled · 1 tools · 1 requests · provider usage unavailable · 0.9s

 › /exit
```

## 40 列：窄屏

```text
mini-pi 0.1.0
model openai/gpt-5.6-terra
project work
session 78d3e9f2
/help for commands


 › inspect the failing test
⟨activity⟩  Thinking…
⟨activity⟩  Read app.py
⟨activity⟩  Thinking… · 1 tools done
⟨activity⟩  Run grep · 1 tools done
✗ Run grep · shell exited 1
⟨activity⟩  Thinking… · 1 tools done
⟨activity⟩  Edit app.py · 1 tools done
⟨activity⟩  Thinking… · 2 tools done
⟨activity⟩  Run python3 · 2 tools done
⟨activity⟩  Thinking… · 3 tools done
add 现在返回 a + b，tests 通过。
Completed · 4 tools · 5 requests ·
provider usage unavailable · 0.2s
/last for the 3 collapsed tool calls

 › /exit
```

## TERM=dumb 与 NO_COLOR

```text
mini-pi 0.1.0
model openai/gpt-5.6-terra
project work
session 879d8296
/help for commands

/exit

--- NO_COLOR + --no-banner ---

mini-pi 0.1.0 · openai/gpt-5.6-terra
work · session 7f10d2f3                                       /help for commands


 › inspect the failing test
⟨activity⟩  Thinking…
⟨activity⟩  Read app.py
⟨activity⟩  Thinking… · 1 tools done
⟨activity⟩  Run grep · 1 tools done
✗ Run grep · shell exited 1
⟨activity⟩  Thinking… · 1 tools done
⟨activity⟩  Edit app.py · 1 tools done
⟨activity⟩  Thinking… · 2 tools done
⟨activity⟩  Run python3 · 2 tools done
⟨activity⟩  Thinking… · 3 tools done
add 现在返回 a + b，tests 通过。
Completed · 4 tools · 5 requests · provider usage unavailable · 0.2s
/last for the 3 collapsed tool calls

 › /exit
```

## 非 tty 一次性输出

```text
● Read app.py
✓ Read app.py · completed
● Run grep
✗ Run grep · shell exited 1
● Edit app.py
✓ Edit app.py · completed · 1 file(s) changed
● Run python3
✓ Run python3 · shell exited 0
add 现在返回 a + b，tests 通过。
Completed · 4 tools · 5 requests · provider usage unavailable · 0.2s
```
