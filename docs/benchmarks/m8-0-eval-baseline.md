# M8.0 固定任务成本与效率基线

2026-09-29；当前 Runtime/CLI 基线来自 Git `72a504c65da9cd6d4350accada0a8a6f2515d653`。执行驱动器为 [`m8-0-eval.py`](m8-0-eval.py)，八条逐次原始指标在 [`m8-0-runs.jsonl`](m8-0-runs.jsonl)。原始记录不包含 API Key、模型正文或工具原文。

## 固定任务与判定

每次把初态复制到独立临时 workspace，HOME 也独立。CLI 使用 `deepseek/deepseek-flash`、`NO_COLOR=1`、`TERM=dumb`；外部判定器用当前 Python 解释器执行 pytest，并固定 `TERM=xterm-256color`、移除宿主 `NO_COLOR`，避免终端测试因调用者环境失真。逐次记录 Python/平台、模型、`max_steps`、初态内容与提示词/判定命令合成的 `fixture_id`。任务结束后再运行判定器，比较初态与最终源文件指纹；修改测试或其他不允许文件即失败。缓存目录与 `.pyc` 不计入源文件改动。

| 任务 | 初态和固定提示 | 外部判定与允许改动 |
| --- | --- | --- |
| `multi_file` | [`fees.py`](../../tests/fixtures/m8_eval/multi_file/fees.py) 的百分比除数与 [`invoice.py`](../../tests/fixtures/m8_eval/multi_file/invoice.py) 的折扣方向均错误；提示要求定位、修复两处并运行测试 | 工作区 `python -m pytest -q` 通过，且只允许 `fees.py`、`invoice.py` 改动 |
| `failure_recovery` | [`parser.py`](../../tests/fixtures/m8_eval/recovery/parser.py) 在空字段上抛错；提示要求依据测试失败修复并复验 | 默认 pytest 通过，且只允许 `parser.py` 改动 |
| `config_ci` | [`pyproject.toml`](../../tests/fixtures/m8_eval/config_ci/pyproject.toml) 的 `addopts` 误筛全部用例，初态默认 pytest 退出 5；提示要求恢复默认测试发现 | 默认 pytest 通过，且只允许 `pyproject.toml` 改动 |
| `real_repo` | 从固定提交导出完整 mini-pi 仓库，再把 `mini_pi/cli/style.py` 的 `count()` 千位分隔实现替换为 `str(value)`；提示要求修复 CLI 数字显示并跑相关测试 | `tests/cli/test_visual_layout.py` 通过，且只允许 `mini_pi/cli/style.py` 改动 |

每条任务都由离线测试先证明初态判定失败、按允许范围修复后通过；测试文件被篡改即使 pytest 通过也判失败。真实仓库样本用固定提交的 Git archive 构造，不依赖当前工作树里的文档或未提交改动。三条小任务 `max_steps=12`，真实仓库任务 `max_steps=20`；以后对照候选必须逐任务保持相同上限与初态。

`success` 同时要求 CLI `completed`、外部判定通过、存在允许的源文件改动且没有越界文件。`external_pass` 单独记录任务后判定，不能把 `step_limit` 算完成。`repeated_exact_calls` 只数同一 run 中工具名与完整参数完全相同的再次调用，不判定它是否多余；`verification_commands` 只数 shell 参数中出现 pytest/ruff/compileall/gradle 的调用，不能等同“全量测试”。Provider usage 只汇总实际报告的请求，逐条保留 `measured_requests/requests`。

复现：

```bash
env -u NO_COLOR TERM=xterm-256color uv run pytest -q tests/integration/test_m8_eval.py
uv run python docs/benchmarks/m8-0-eval.py --repeats 2 --max-steps 12 --only multi_file --only failure_recovery --only config_ci --jsonl /tmp/m8-small.jsonl
uv run python docs/benchmarks/m8-0-eval.py --repeats 2 --max-steps 20 --only real_repo --jsonl /tmp/m8-repo.jsonl
```

## 真实 Provider 逐次结果

八次任务运行中的全部模型请求均有 Provider usage；表内 Token 是一次任务的实测累计值。耗时为 CLI 子进程墙钟时间，不含初态复制与任务后判定。详细工具分布、初态指纹、允许范围检查和每次判定耗时见 JSONL。

| 任务 / 次数 | 任务成功 | 终止 | 请求 | 工具 | input / output | 秒 | 相同参数重复 | 验证命令 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| multi_file 1 | 是 | completed | 5 | 8 | 11,820 / 687 | 6.23 | 0 | 1 |
| multi_file 2 | 是 | completed | 5 | 8 | 12,413 / 660 | 6.23 | 0 | 1 |
| failure_recovery 1 | 是 | completed | 6 | 7 | 14,830 / 549 | 6.53 | 0 | 2 |
| failure_recovery 2 | 是 | completed | 6 | 7 | 15,202 / 670 | 7.72 | 0 | 2 |
| config_ci 1 | 是 | completed | 6 | 7 | 14,470 / 501 | 6.92 | 0 | 2 |
| config_ci 2 | 是 | completed | 5 | 5 | 12,426 / 489 | 6.16 | 0 | 3 |
| real_repo 1 | 否 | completed | 16 | 22 | 141,796 / 2,878 | 85.77 | 0 | 8 |
| real_repo 2 | 是 | completed | 18 | 20 | 191,480 / 2,549 | 36.47 | 2 | 7 |

真实仓库第 1 次外部 pytest 通过，但额外改动了 `mini_pi/cli/console.py`，所以任务失败；第 2 次只改允许文件并通过。两次 input 差 49,684，墙钟时间方向相反，说明不能用请求/Token 数直接推断耗时；目前没有逐请求网络计时来分解原因。两次样本也不足以估计稳定成功率或尾延迟。此前使用 12 步的真实仓库试跑在外部测试通过后触及 `step_limit`，明确不计入这八条正式样本，也不计为成功。

## 验证与使用边界

离线固定任务/判定回归 7 passed；全量离线测试 630 passed / 5 deselected；`uv run ruff check .`、`uv run python -m compileall -q mini_pi`、`git diff --check` 通过。真实 DeepSeek 样本为 8 次，6 条小任务全部完成，真实仓库任务 1/2 完成。未对拍 OpenAI、不同模型、不同系统提示或候选优化，因此本记录只建立当前版本的比较起点，不宣称节省费用或稳定改进。
