# M7.8 最终验收

2026-09-28，在 `96c6279` 基线上收口 M7.8。交付提交依次为 `0e5848e`（read 空文件）、`0517a4e`（CI/Ruff）、`4fbedde`（RequestSnapshot）、`e19182b`（Token 估算）、`2bc65ee`（窗口策略）、`96c6279`（RunContext/钩子）。

## 本地检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 锁定依赖 | `uv sync --locked` | 通过，35 个包解析、33 个包检查 |
| 完整离线回归 | `env -u NO_COLOR TERM=xterm-256color uv run pytest -q` | 566 passed、5 deselected |
| 不变量专项 | `env -u NO_COLOR TERM=xterm-256color uv run pytest -q tests/integration/test_auto_compaction.py tests/session/test_budget_resume.py tests/session/test_compaction_transaction.py tests/cli/test_session_create.py tests/agent/test_cancellation.py tests/agent/test_run_budget.py tests/session/test_context_window_config.py tests/context/test_request.py` | 41 passed |
| 静态检查 | `uv run ruff check .` | 通过 |
| 编译检查 | `uv run python -m compileall -q mini_pi` | 通过 |
| 差异检查 | `git diff --check` | 通过 |

专项覆盖纯内存模式、JSONL 追加与恢复、工具调用/结果配对、任务预算、自动压缩事务及自定义窗口。新增能力未改变默认预算关闭、未知窗口不自动压缩和原始 Session 消息保留的行为。CI workflow 已配置与上述离线命令等价的步骤；这些提交尚未推送，远端 CI 尚无运行结果。

## DeepSeek 输入用量复核

运行 `uv run python docs/benchmarks/m7_8_token_estimation.py`，模型 `deepseek-flash`，以 Provider `usage.input_tokens` 为实际值。固定夹具、系数与初次校准见 [M7.8.2 记录](m7-8-token-estimation.md)。

| 样本 | 当前估算 | 实际 input | 偏差 |
| --- | ---: | ---: | ---: |
| 中文 | 292 | 288 | +1.4% |
| 英文 | 210 | 189 | +11.1% |
| 混合 | 294 | 282 | +4.3% |
| 工具 schema | 360 | 372 | −3.2% |

四组均在 M7.8.2 声明的 ±15% 固定样本容差内，中文未低估。当前请求预测仍是 `estimated`。OpenAI、真实长会话及大量工具时的误差未对拍；自定义小窗口自动压缩只有离线夹具验证，远端 CI 需推送后查看。
