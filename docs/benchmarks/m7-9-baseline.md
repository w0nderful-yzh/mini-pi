# M7.9.3 真实任务基线与规模测量

2026-09-28，基线提交 `51e1500`。任务基线与规模测量在该提交的行为上运行；工具开销复核随后修正了估算常数（见下节，改动落在 `mini_pi/context/request.py`）。

## 环境与运行方式

| 项 | 值 |
| --- | --- |
| Provider / 模型 | deepseek / `deepseek-flash` |
| Python | 3.12.13 |
| openai SDK | 3.16.2 |
| 平台 | macOS 26.5.2 (arm64) |
| 任务夹具 | `tests/fixtures/sample_project`（`add` 实现为 `a - b`，测试期望 5） |

```bash
uv run pytest tests/integration/test_task_baseline.py                      # 离线基线，可重复
uv run python docs/benchmarks/m7-9-baseline.py real --max-steps 12          # 真实任务基线
uv run python docs/benchmarks/m7-9-baseline.py scale                        # Session 规模
uv run python docs/benchmarks/m7-9-baseline.py tools                        # 工具模式开销复核
```

真实任务在临时 workspace 与临时 HOME 中运行，不写仓库文件、真实 Session 或输入历史；只记录计数、耗时、内存与 Provider 实测 usage，不记录模型回复原文与 API Key。任务是否完成由**任务结束后重跑 pytest 的真实结果**判定，不采信模型总结。四条任务的提示词定义在驱动器 `TASK_PROMPTS` 中；`--only <name>` 可重跑单条。

## 离线基线（FakeLLM，可重复）

同一组任务形态用脚本化模型走生产装配路径（真实工具、真实 Workspace、真实 JSONL）：

| 任务 | 形态 | 请求 | 工具调用 | 终止原因 | `modified_files` | 任务后 pytest |
| --- | --- | ---: | ---: | --- | --- | --- |
| read_only | 只读解释 | 2 | 1 | completed | 无 | 仍失败（未修改） |
| locate | 跨文件定位 | 4 | 3 | completed | 无 | 仍失败（未修改） |
| modify_and_verify | 修改后验证 | 5 | 4 | completed | `calculator.py` | 通过 |
| recover_from_failure | 命令失败后换命令并修复 | 6 | 5 | completed | `calculator.py` | 通过 |

这组数字由 `tests/integration/test_task_baseline.py` 断言，脚本被完全消费（请求数与脚本长度不一致即失败），因此可作为真实基线的对照基准。

## 真实 Provider 基线

四条固定任务各跑一次，`--max-steps 12`：

| 任务 | 退出码 | 终止原因 | 请求 | 工具调用 | 实测 input | 实测 output | 最后一次请求 input | 耗时 | 工具分布 | 任务后 pytest |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| read_only | 0 | completed | 3 | 4 | 6,180 | 366 | 2,231 | 3.0s | bash×1 read×2 search×1 | 仍失败（未修改） |
| locate | 0 | completed | 3 | 4 | 6,259 | 372 | 2,271 | 2.8s | bash×1 read×2 search×1 | 仍失败（未修改） |
| modify_and_verify | 0 | completed | 5 | 6 | 11,768 | 449 | 2,651 | 5.0s | bash×3 edit×1 read×2 | 通过 |
| recover_from_failure | 0 | completed | 5 | 6 | 11,482 | 400 | 2,565 | 4.3s | bash×3 edit×1 read×2 | 通过 |
| **合计** | — | 4/4 completed | 16 | 20 | 35,689 | 1,587 | — | 15.1s | — | 2/2 修复任务真实通过 |

观察：

- 只读与定位任务各 3 次请求；两个修复任务各 5 次请求，都没有触及 `--max-steps 12`。真实模型两次都先跑 pytest 看到失败再改，`modify_and_verify` 与 `recover_from_failure` 的差别只体现在离线脚本里（脚本先执行一条错误路径的命令）；真实运行没有浪费步骤，因此**本轮没有样本落在 `step_limit` / `budget_limit` / `error` 上**，那几条退出语义只由 M7.9.1 的离线用例覆盖。
- 修复任务把 `calculator.py` 改对并由 pytest 复验通过，`modified_files` 与真实改动一致；只读任务没有改动文件。
- 最后一次请求 input（2.2k–2.7k）只占该任务累计 input 的三分之一左右（如只读任务 6,180 累计中的 2,231），再次说明累计消耗不能当窗口占用。按 Provider 返回的 usage 记录，不换算费用（费率不在本项目协议内）。
- 样本量是每条任务 1 次，不能据此判断模型方差。

## Session 规模

`resume` 用 `AgentSession.resume`（注入不请求模型的占位客户端）测量，`list` 用 `list_session_summaries` / `latest_session_path` 测量。内存列是 `tracemalloc` 的 Python 分配峰值，不是进程 RSS。

| 场景 | JSONL | 耗时 | 分配峰值 |
| --- | ---: | ---: | ---: |
| resume，100 条消息 | 34.3 KB | 8.2 ms | 0.42 MB |
| resume，400 条消息 | 137.5 KB | 32.5 ms | 1.72 MB |
| resume，1600 条消息 | 551.7 KB | 128.9 ms | 6.87 MB |
| `/sessions`，10 个候选 × 200 条 | — | 313 ms | 1.63 MB |
| `--continue`（同 10 个候选） | — | 322 ms | 1.30 MB |
| `/sessions`，40 个候选 × 200 条 | — | 1,291 ms | 2.11 MB |
| `--continue`（同 40 个候选） | — | 1,287 ms | 1.75 MB |

观察：恢复耗时随消息数线性增长（约 0.08 ms/条）；列表与 `--continue` 都要严格加载**全部**候选（约 32 ms/候选），40 个候选、每个 200 条消息时约 1.3 s。候选损坏时整体失败仍是刻意行为，本轮没有出现瓶颈到需要改存储格式的程度，符合计划里「先测量再优化」的边界。

## 工具模式开销复核（M7.8.2 固定值的重新对拍）

同一条极短 system + user 消息，按默认注册表顺序取前 N 个工具。

首次复核（改动前，只测 0 与 7）：

| 用例 | 工具数 | 估算 | Provider 实测 input | 偏差 |
| --- | ---: | ---: | ---: | ---: |
| no_tools | 0 | 41 | 40 | +2.5% |
| default_registry | 7 | 1,172 | 1,406 | **−16.6%** |

无工具请求准确，7 个工具时低估 16.6%，超出 M7.8.2 为固定样本声明的 ±15% 容差。差额约 235 token 出现在「工具模式」而不是消息文本上：M7.8.2 用 1–2 个工具校准的是 `_TOOLS_OVERHEAD_TOKENS = 210` 这个**固定**项，而实际开销随工具数继续增长。默认工具集从 6 个涨到 7 个（M7.9.2 新增 `git_status`）让缺口显现。

据此做工具数扫描（`--tool-counts 0,1,3,7`）：

| 工具数 | 估算（改动前） | 实测 input | 偏差 | 每多一个工具的实测增量 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 41 | 40 | +2.5% | — |
| 1 | 389 | 429 | −9.3% | 389 |
| 3 | 676 | 787 | −14.1% | 179 |
| 7 | 1,172 | 1,406 | −16.6% | 154.8 |

数据说明：`schema 文本`（估算里已有的部分）每工具约 130 token，而实测每工具约 155–163 token，差额是一个与工具数成正比的**每工具 wire 结构开销**；此外进入工具模式的一次性开销也略高于 210。以 1/3/7 三点最小二乘拟合 `实测 = 41 + wire(n) + base + framing × n` 得 `base = 220.6`、`framing = 32.1`。

**改动（`mini_pi/context/request.py`）：** 保留一次性固定项并取整为 `_TOOLS_OVERHEAD_TOKENS = 220`，新增 `_TOOL_FRAMING_TOKENS = 32` 按工具数累加。`estimate_tools_tokens()` 现在是「schema 文本 + 每工具结构开销 + 一次性模式开销」；`/context` 的工具分类同源受益。

复验（同一探针，含未参与拟合的 2 与 5）：

| 工具数 | 0 | 1 | 2 | 3 | 5 | 7 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 改动前偏差 | +2.5% | −9.3% | — | −14.1% | — | −16.6% |
| 改动后偏差 | +2.5% | +0.5% | +4.1% | −0.6% | −2.5% | 0.0% |

0–7 个工具全部落在 ±5% 内，两个留出点（2、5）分别 +4.1% 与 −2.5%，说明线性结构没有过拟合 1/3/7。拟合与系数只在 `deepseek-flash` 上对拍过，OpenAI 未验证。

附带影响：`--max-run-input-tokens` 的预算夹具对估算敏感，`tests/agent/test_run_budget.py` 与 `tests/session/test_budget_resume.py` 的阈值按新预测重新标定（500→600、750→800），断言语义不变。

## 决策输入

| 待决策 | 本次基线提供的事实 |
| --- | --- |
| 是否接入 LSP / MCP 与更多工具 | 7 个工具的工具模式开销实测约 1.4k input（无工具 40），且每多一个工具约 +163 input（含 schema 文本）；在本次真实任务里最后一请求才 2.2k–2.7k，工具模式已占相当比例，工具数量继续增长会线性抬高每次请求的固定成本。M8.3 的活动工具集过滤有可量化杠杆，`RequestSnapshot` 能直接对比过滤前后 |
| 是否需要 Session 加速 | 40 个候选 × 200 条消息时列表/`--continue` 约 1.3 s，单会话恢复 1600 条约 129 ms；尚无阻塞级瓶颈，先不改存储格式，继续记录候选数增长 |
| M7.9.4 CLI 视觉整理是否有可度量的目标 | 本次记录到 read_only/locate 的 3 次请求与修复任务的 5 次请求都很快结束，终端展示的主要价值在可读性，不在缩短任务 |
| 估算精度 | 消息与无工具请求准确；工具模式已按 1/3/7 工具重新拟合并在 0–7 个工具上复验到 ±5% 内，自动压缩与任务预算不再系统性偏乐观 |

## 未验证

- OpenAI Provider、1M 窗口下的真实长会话、MCP/LSP 等外部工具、并行工具执行与 Windows 都未纳入本次基线。
- 真实任务每条只跑 1 次，没有方差或失败样本；`step_limit` / `budget_limit` / `error` 只由离线用例覆盖。
- 规模测量的内存口径是 Python 分配峰值，不是进程常驻内存；JSONL 生成耗时不计入测量。
- 工具结构开销只在 `deepseek-flash` 与内置注册表的 schema 上拟合；OpenAI 与大规模外部工具的 schema 未对拍。修正只影响「工具模式固定开销」，只把常数改动应用到与消息分离的工具项，`estimate_request` 的 `source` 仍标为 estimated。
