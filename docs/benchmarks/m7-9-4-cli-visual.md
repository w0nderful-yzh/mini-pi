# M7.9.4 CLI 视觉验收

2026-09-28，基线 `ea4d22a`。运行
`uv run python docs/benchmarks/m7-9-4-tty-driver.py <out_dir>` 可重放：真实 pty、脚本化
模型、临时 HOME，不联网，不触碰真实凭据、Session 与输入历史。本次运行 `failed checks: none`。

覆盖：80 列启动页/任务流/状态页、40 列窄屏、`TERM=dumb`、`NO_COLOR` + `--no-banner`、
非 tty 一次性输出、任务中 Ctrl+C。全部状态标记（ToolError、shell 非零、超时、截断、预算、
`step_limit`、取消）的展示契约由 `tests/cli/test_visual_layout.py` 覆盖；本记录只证明真实
终端形态下的布局与降级行为。

转录说明：`⟨thinking⟩` 是连续动画帧折叠后的标记（帧每秒刷 8 次，原文会淹没转录）；工具行在
字节流里另起一行，实际终端上指示器是被**同一行覆盖**的——停止时的字节序列为
`…Thinking…\r\n\x1b[?25h\r\x1b[1A\x1b[2K\x1b[36m● Read a.py…`，即光标上移一行、清行、再写工具行。

## 思考指示器的硬约束

指示器必须是**单行、帧宽恒定**：Live 区域清除时按渲染高度回退光标，高度为 1 时正好清掉那一行。
此前用的 18 行 ASCII 图案（`assets/thinking.txt`，最宽 27 列）会触发 `vertical_overflow`
裁剪，且停止前会以 `visible` 再渲染一次全高，光标回退量与屏幕内容不一致，图案就留在滚动区、
每轮叠一次（用户截图里反复出现的 `db db / d88 88 / …` 即此）。因此改为 `style.THINKING_FRAMES`
的 4 帧单格宽序列（`◜ ◝ ◞ ◟`，8 fps，`vertical_overflow="crop"`），并用
`test_thinking_frames_cycle_at_constant_width` 钉住「帧宽守恒 + 每个字形单格宽」。

## 80 列：启动页、任务流与状态页

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
work · session 38478f9c                                       /help for commands


 › inspect and fix the failing test
⟨thinking⟩
● Read app.py
✓ Read app.py · completed
⟨thinking⟩
● Run grep
✗ Run grep · shell exited 1
⟨thinking⟩
● Edit app.py
✓ Edit app.py · completed · 1 file(s) changed
⟨thinking⟩
● Run python3
✓ Run python3 · shell exited 0
⟨thinking⟩
add 现在返回 a + b，tests 通过。
Completed · 4 tools · 5 requests · provider usage unavailable · 0.2s

 › /status
Model           openai/gpt-5.6-terra
Workspace       work
Session         38478f9c
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
* 38478f9c · 2026-09-28 05:25:49Z · openai/gpt-5.6-terra · summary no
* current session
--continue resumes the latest validated session; --resume <session.jsonl>
resumes an exact file

 › run a long command
⟨thinking⟩
● Run sleep
^C✗ Run sleep · failed: Tool execution was cancelled by the user (Ctrl+C). The
command may have been terminated; re-run it if the result is still needed.
✗ Task cancelled by user. Committed messages and file changes were kept.
Cancelled · 1 tools · 1 requests · provider usage unavailable · 0.0s

 › /exit
```

## 40 列：窄屏

```text
mini-pi 0.1.0
model openai/gpt-5.6-terra
project work
session 451f7829
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
session 36ef1f59
/help for commands

/exit

--- NO_COLOR + --no-banner ---

mini-pi 0.1.0 · openai/gpt-5.6-terra
work · session e730a731                                       /help for commands


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
