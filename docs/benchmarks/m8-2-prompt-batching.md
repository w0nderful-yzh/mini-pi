# M8.2 已知独立检查的合批 Prompt 对照

2026-09-30。**结论：完成本批对照试验，候选未进入默认 Prompt。** 两种规则各 8/8 通过外部判定；候选总请求 68→67，但累计 input 439,833→461,802，工具错误 1→4，同参重复读取 0→2。收益方向随任务和重复次数改变，没有足够证据按 Phase 3 的准入条件采纳。

## 初态、变量与复现

运行时为 M8.1 提交 `30aeb23da230c4ef9b0fa6c893b60cb96a8401aa`，Provider/模型为 `deepseek/deepseek-flash`。复用 [M8.0 固定初态与外部判定](m8-0-eval-baseline.md)：三类小任务 `max_steps=12`，真实仓库 `max_steps=20`；真实仓库仍从 `72a504c65da9cd6d4350accada0a8a6f2515d653` 导出并注入缺陷。每次独立 workspace 和 HOME，CLI 环境、工具 schema、用户提示、允许改动与判定命令保持一致。

[`m8-2-eval.py`](m8-2-eval.py) 从固定提交解析 baseline 规则，batched 只追加 `BATCH_RULE`：必要只读检查的参数已知且相互不依赖时，同一回复发出多个工具调用；依赖 observation 的后续操作等待结果；工具串行执行，不为合批增加无关检查。替换仅发生在评测子进程，没有增加生产配置。每条任务重复两次，第 1 次按 baseline→batched，第 2 次按 batched→baseline 运行，减少固定顺序偏差。

```bash
uv run python docs/benchmarks/m8-2-eval.py --repeats 2 --jsonl /tmp/m8-2-runs.jsonl
```

本次 16 条原始指标保存在 [`m8-2-runs.jsonl`](m8-2-runs.jsonl)。每种任务全部记录的 `fixture_id` 相同；两种规则的 SHA-256 随记录保存。原始指标不保存 API Key、模型正文或工具原文。

## 实测结果

16 次均以 `completed` 结束，任务后测试通过、只改允许文件，全部 135 次请求都报告 Provider usage。input/output 是任务累计实测值，耗时为 CLI 子进程墙钟时间，不含初态准备和外部判定。

| 任务 / 次数 | 规则 | 请求 | 工具 | input / output | 秒 | 多调用轮 | 工具错误 | 重复读取 | 验证命令 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| multi_file 1 | baseline | 5 | 8 | 12,489 / 846 | 7.29 | 3 | 0 | 0 | 1 |
| multi_file 1 | batched | 5 | 7 | 12,565 / 693 | 5.73 | 2 | 0 | 0 | 1 |
| multi_file 2 | batched | 5 | 7 | 11,812 / 568 | 5.84 | 2 | 0 | 0 | 1 |
| multi_file 2 | baseline | 5 | 8 | 12,220 / 686 | 5.87 | 3 | 0 | 0 | 1 |
| failure_recovery 1 | baseline | 6 | 7 | 15,440 / 609 | 6.67 | 2 | 0 | 0 | 2 |
| failure_recovery 1 | batched | 7 | 8 | 18,827 / 771 | 7.91 | 2 | 1 | 1 | 2 |
| failure_recovery 2 | batched | 6 | 7 | 15,524 / 576 | 6.24 | 2 | 0 | 0 | 2 |
| failure_recovery 2 | baseline | 7 | 7 | 17,838 / 754 | 7.33 | 1 | 0 | 0 | 2 |
| config_ci 1 | baseline | 6 | 7 | 14,779 / 608 | 6.25 | 2 | 1 | 0 | 2 |
| config_ci 1 | batched | 5 | 5 | 12,679 / 515 | 5.85 | 1 | 1 | 0 | 3 |
| config_ci 2 | batched | 5 | 6 | 12,137 / 516 | 5.36 | 2 | 1 | 0 | 2 |
| config_ci 2 | baseline | 6 | 8 | 15,100 / 660 | 7.32 | 3 | 0 | 0 | 2 |
| real_repo 1 | baseline | 17 | 20 | 184,222 / 3,117 | 61.68 | 4 | 0 | 0 | 8 |
| real_repo 1 | batched | 18 | 24 | 236,148 / 6,515 | 91.11 | 7 | 1 | 1 | 7 |
| real_repo 2 | batched | 16 | 19 | 142,110 / 3,765 | 73.68 | 4 | 0 | 0 | 6 |
| real_repo 2 | baseline | 16 | 23 | 167,745 / 4,081 | 72.13 | 8 | 0 | 0 | 10 |

| 总计 | baseline | batched |
| --- | ---: | ---: |
| 任务成功 / 样本 | 8 / 8 | 8 / 8 |
| 请求 / 工具 | 68 / 88 | 67 / 83 |
| input / output | 439,833 / 11,361 | 461,802 / 13,919 |
| 墙钟秒数合计 | 174.54 | 201.72 |
| 多调用轮 / 工具错误 | 26 / 1 | 22 / 4 |
| 失败读取 / 同参重复读取 | 0 / 0 | 0 / 2 |
| 验证命令 | 28 | 24 |

`multi_call_turns` 按一条 assistant 内至少两个 tool call 计，不表示并行或节省。`failed_reads` 只计 `read` 的 `is_error` observation，不能识别所有语义上无关的成功读取；`repeated_exact_reads` 是同一任务中工具名与完整参数完全相同的再次读取，不能据此断言结果未变或读取多余。`tool_errors` 按 `is_error` 计，普通 bash 非零退出不归入它。验证命令沿用 M8.0 的词匹配口径，次数差异不能直接证明验证强度变化。每条记录另保存工具分布和总同参重复数。

## 决策与边界

基线已在多个轮次主动合批。候选在两次配置任务上均少一次请求，多文件任务没有减少请求，失败恢复和真实仓库的方向不一致；总请求只差一次，而 input、output、工具错误和墙钟时间均更高。按“成功率与验证优先、额外调用和错误不能被少请求掩盖”的准入条件保留默认基线，候选文本只留在评测脚本供复现。

离线评测回归 9 passed，Agent/Loop/Session 聚焦测试 28 passed，全量 648 passed / 5 deselected；Ruff、生产包 compileall 与 diff 检查通过。新增恢复测试确认内置规则变化只追加一次 `rules` section patch，旧 JSONL 字节和工具配对保持完整，Provider 看到更新后的规则。

这 16 次不能估计稳定成功率、尾延迟或证明所有合批 Prompt 都无效。未测 OpenAI、其他模型或其他候选；未逐请求分解网络耗时，也没有真实费用计算。后续若试新候选，应重新做相同初态对照，不能把本次总请求略少包装成费用收益。
