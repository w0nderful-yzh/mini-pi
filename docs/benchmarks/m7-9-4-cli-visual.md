# M7.9.4 CLI 视觉验收

2026-09-28，基线 `672af3e`。运行
`uv run python docs/benchmarks/m7-9-4-tty-driver.py <out_dir>` 可重放：真实 pty、脚本化
模型、临时 HOME，不联网，不触碰真实凭据、Session 与输入历史。本次运行 `failed checks: none`。

覆盖：80 列任务流与状态页、40 列窄屏、`TERM=dumb`、`NO_COLOR` + `--no-banner`、非 tty
一次性输出、任务中 Ctrl+C。全部状态标记（ToolError、shell 非零、超时、截断、预算、
`step_limit`、取消）的展示契约由 `tests/cli/test_visual_layout.py` 覆盖；本记录只证明
真实终端形态下的布局与降级行为。转录为原始 pty 输出，已去掉重复的提示符重绘行。

## 80 列：任务流与状态页

```text
mini-pi 0.1.0 · openai/gpt-5.6-terra
work · session 42d82df8                                       /help for commands


 › inspect and fix the failing test
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

 › /status
Model           openai/gpt-5.6-terra
Workspace       work
Session         42d82df8
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

Compaction      window threshold 1,041,808 tokens (window 1,050,000 - reserve 8,192)
                cost-aware early compaction for old tool results is enabled

 › /tools
read        Read a UTF-8 text file inside the workspace. Supports offset/limit paging and reports a continuation hint when truncated.
write       Create or fully rewrite a file inside the workspace. Parent directories are created automatically and writes are atomic.
edit        Apply exact, unique text replacements to an existing file. All edits match the original content and must not overlap. Returns a unified diff.
search      Search text or regex across workspace files and return file:line:text matches. Skips .git/.venv/node_modules and binary files.
bash        Run a shell command in the workspace root. Returns exit code, stdout and stderr. Non-zero exit codes are reported, not raised.
git_diff    Show uncommitted changes in the workspace git repository as a unified diff.
git_status  Show the git working tree status of the workspace: branch plus staged, unstaged, untracked and conflicted paths. Read-only; use git_diff to inspect the content of those changes.

 › /sessions
Saved sessions for work:
* 42d82df8 · 2026-09-28 04:58:59Z · openai/gpt-5.6-terra · summary no
* current session
--continue resumes the latest validated session; --resume <session.jsonl>
resumes an exact file

 › run a long command
● Run sleep
^C✗ Run sleep · failed: Tool execution was cancelled by the user (Ctrl+C). The
command may have been terminated; re-run it if the result is still needed.
✗ Task cancelled by user. Committed messages and file changes were kept.
Cancelled · 1 tools · 1 requests · provider usage unavailable · 0.0s

 › /exit
```

两次 Ctrl+C 之间还可能插入 `✗ Run sleep · failed: Tool execution was cancelled ...`，那是被中断工具的
真实 observation 预览，不是额外状态。

展示改动只落在 `mini_pi/cli/`（`style.py`、`banner.py`、`console.py`、`status.py`、`sessions.py`、`input.py`、
`app.py` 的 Console 构造），未触碰 `mini_pi/agent`、`session`、`context`、`tools`，因此 `ToolMessage`、
JSONL 与模型请求不随展示变化；全量 612 passed / 5 deselected 覆盖事实链未变。

## 40 列：窄屏

```text
mini-pi 0.1.0
model openai/gpt-5.6-terra
project work
session 9fdd8299
/help for commands


 › inspect the failing test
● Read app.py
✓ Read app.py · completed
● Run grep
✗ Run grep · shell exited 1
● Edit app.py
✓ Edit app.py · completed · 1 file(s)
changed
● Run python3
✓ Run python3 · shell exited 0
add 现在返回 a + b，tests 通过。
Completed · 4 tools · 5 requests ·
provider usage unavailable · 0.2s

 › /exit
```

## TERM=dumb 与 NO_COLOR

```text
mini-pi 0.1.0
model openai/gpt-5.6-terra
project work
session 9ff9ad6e
/help for commands

/exit

--- NO_COLOR + --no-banner ---

mini-pi 0.1.0 · openai/gpt-5.6-terra
work · session edb1c211                                       /help for commands


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
