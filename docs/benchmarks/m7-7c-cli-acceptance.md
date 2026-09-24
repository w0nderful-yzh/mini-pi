# M7.7c 人工 CLI 验收记录（真实 pty，离线 Fake 模型）

执行日期：2026-09-24（本地）。全部交互发生在真实 pty 上（stdin/stdout 都是 tty，走 `prompt_toolkit` 分支），模型由脚本化 Fake 替换，**不联网、不使用凭据**；`HOME` 指向临时目录，不触碰真实 Session 与输入历史。

## 复现

```bash
.venv/bin/python docs/benchmarks/m7-7c-cli-driver.py /tmp/m7-7c.json
```

驱动器：`docs/benchmarks/m7-7c-cli-driver.py`。阶段 1/2 使用未知窗口模型 `mini-pi-test-model`，从而关闭自动压缩，只观察手动 `/compact` 路径；自动压缩由 `tests/integration/test_auto_compaction.py` 覆盖。

## 结果

| 阶段 | 覆盖项 | 观察结果 |
| --- | --- | --- |
| 1 create | 新建会话 → 两次真实 `bash` 工具调用 → `/status` `/status full` `/context` `/sessions` → `/exit` | 事件顺序 system/user/assistant/tool/assistant/tool/assistant；工具事件标题 `Run python3`、`shell exited 0`、`output truncated` 均为确定性展示；`/sessions` 标出 `* current session` |
| 2 resume | `--resume` → `/compact` → `/context` → 继续任务 → `/status` → `/new` → 新会话任务 → 退出 | `Compaction: summarized …`；压缩 entry `tokensBefore=25442`、summary 158 字符；阶段 1 的 entry id 全部保留（append-only）；`/new` 后 workspace 下出现第 2 个会话文件 |
| 3 budget | `--max-run-input-tokens 5000` → 真实大输出工具 → `/exit` | 工具真实执行后在下一次请求前停止，输出 `input budget would be exceeded`；`kind=budget_limit`，`tool_event=true`（已执行结果保留） |
| 4 窄屏 + 中断 | 40×24 窄屏启动 → 输入任务 → 空闲 Ctrl+C → 再按 Ctrl+C | Banner 降级为 `牛人，就用牛的 coding agent` 单行；启动信息按 `model …` / `project …` 分行；首次 Ctrl+C 提示 `Press Ctrl+C again to exit.`，第二次 `exiting mini-pi.`；输入历史含 `+narrow task` |
| 5 非 tty | 管道输入 `/help` `/exit` | 退出码 0，输出包含 `Input: Enter submits`；未创建 `~/.mini-pi/history`，确认回退内建单行 `read` 而非行编辑 |

脱敏后的驱动输出（节选）：

```json
{
  "phase1_create": {"message_roles": ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]},
  "phase2_resume": {
    "compaction_entry": {"tokensBefore": 25442, "summaryChars": 158},
    "session_count": 2,
    "append_only": true
  },
  "phase3_budget": {"kind": "budget_limit", "tool_event": true},
  "phase4_narrow_interrupt": {"banner_fallback": true, "history_has_task": true, "interrupt_exit": true},
  "phase5_non_tty": {"exit_code": 0, "plain_input_fallback": true, "history_created": false}
}
```

## 边界说明

- 该记录只使用 Fake 模型，**不构成**真实 Provider 的费用或质量结论；真实验收见 [M7.7b 记录](m7-7b-real-compaction.md)。
- Ctrl+C 在任务中取消的完整语义（工具配对、进程组清理）见 [M7.D2 记录](m7-d2-tty-input-cancel.md)。
- 驱动器只写临时目录，输出不含凭据、真实路径或真实 Session 内容。
