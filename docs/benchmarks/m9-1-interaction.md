# M9.1：关键问询验收

日期：2026-09-30。范围为 `ask_user`、CLI 回答与同一 run 续跑；权限审批尚未实现。

## 离线边界

- `tests/agent/test_question_loop.py`：通过阻塞的宿主函数确认等待期间只有一次请求、没有文件修改；回答进入下一请求后才执行 write。问询在混合批次首位、末位或重复出现时，整批零执行，模型可纠正为独占批次。
- `tests/test_ask_user.py` / `tests/cli/test_question_input.py`：严格参数、无处理函数、编号与自由文本、空答案与超长非法序号本地重问、非 TTY、EOF、取消、终端脱敏与 Live 收起。新建、内存、REPL、恢复装配均覆盖；展示文本不进入回答。
- `tests/session/test_question_resume.py`：正常回答和取消后严格 replay/resume 保留原始事实，旧问询不重放；真实子进程等待时被 SIGKILL，恢复明确报告缺失 tool result，原 JSONL 不改写。
- 提问前 assistant 提交失败不调用宿主；回答 ToolMessage 提交失败不安装答案或请求下一轮。Prompt 只在实际注册 `ask_user` 时加入关键决策规则。

验证命令：

```sh
env -u NO_COLOR TERM=xterm-256color uv run pytest -q tests/agent/test_prompt.py tests/agent/test_question_loop.py tests/cli/test_question_input.py tests/session/test_question_resume.py tests/test_ask_user.py
env -u NO_COLOR TERM=xterm-256color uv run pytest
uv run ruff check .
uv run python -m compileall -q mini_pi
git diff --check
```

定向 44 项通过；全量 693 项通过、5 项真实 API integration 用例按默认配置未执行。Ruff、compileall 与 diff 检查通过。TTY 视觉测试要求解除环境中的 `NO_COLOR` 并配置 TERM；默认环境首次全量执行的显示断言失败不作为产品行为通过依据。

## 真实 Provider 与终端

使用 DeepSeek / `deepseek-flash`、默认八工具注册表、真实 CLI 和 `pty.fork()` 的 80×24 终端。仅把会话/认证写入位置隔离至临时目录，不替换模型、工具、输入处理或 Loop。回答由脚本写入 PTY，取消由脚本发送 SIGINT；这是自动终端验收，未声称真人手工输入。

固定小任务：`round_price` 原实现为 `int(amount)`，任务要求首先询问四舍五入还是银行家舍入；收到决定后仅改 `rounding.py`，执行 `python -m unittest -v`，不得修改测试。测试覆盖非负数 2.5→3、2.4→2、0.5→1、3.0→3。脚本在 `Answer ›` 后暂停 0.5 秒，确认源码和 JSONL 字节保持不变，再提交自由文本或 SIGINT。

| 场景 | 退出码 | 模型请求 / 工具调用 | 实测 input / output tokens | 外部结论 |
| --- | --- | --- | --- | --- |
| 回答「采用四舍五入（0.5 进一），仅处理非负金额。」 | 0 | 7 / 6 | 20,646 / 1,257 | 一次独占问询；回答前零修改；之后完成修复；模型执行和宿主独立执行 unittest 均通过；测试文件不变；严格 replay 通过 |
| 问询等待时 SIGINT | 130 | 1 / 1 | 2,222 / 318 | 不再请求模型；源码不变；问询有配对的 cancelled observation；严格 replay 通过 |

成功场景耗时 10.04 秒，取消场景 2.82 秒。成功场景中的一次目录探查因不存在 `tests/` 返回 exit 1，按真实 observation 回传，模型改读存在的测试文件并继续；没有掩盖该 shell 结果。取消场景保持原错误实现，其外部 unittest 仍失败是预期结果。

首轮临时驱动使用 `openpty + Popen(start_new_session=True)`，未建立控制终端：问答和修改已发生，但后续 Python 子命令报标准流 bad file descriptor，CLI 退出 120，未计为通过。改用仓库既有终端驱动所采用的 `pty.fork()` 后完整验收通过；这一调整没有修改产品代码。失败轮记录的 input/output 为 13,642/782 tokens。以上是 JSONL 已提交 usage 的实测值，不是计费或节省结论，也不是默认八工具的完整估算校准。

## 能力限制

宿主只保证调用 `ask_user` 后等待真实回答；模型仍可能遗漏关键决策。问询不强制审批、不收窄 bash 的无沙箱权限，硬权限控制属于 M9.2。正常回答或取消可恢复；等待时崩溃留下不完整工具批次会严格报错，未实现跨重启继续等待。回答是普通 ToolMessage，压缩继续遵守现有消息配对与安全切点规则。
