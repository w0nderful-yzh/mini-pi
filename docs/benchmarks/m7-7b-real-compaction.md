# M7.7b 真实 Provider 会话压缩验收记录

执行日期：2026-09-24（本地）。只使用已配置的 DeepSeek 凭据，**不输出任何 Key**；OpenAI 未配置 Key，明确标注为未测。

## 执行命令与结果

```bash
uv run pytest -m integration tests/test_integration_llm.py -q -rs
# 1 passed, 1 skipped   （deepseek 通过；openai skipped: OPENAI_API_KEY not set）

uv run pytest -m integration tests/test_integration_compaction.py -q
# 2 passed
```

新增用例 `tests/test_integration_compaction.py`（带 `integration` marker，默认被 `addopts` 排除）：

- `test_real_session_compacts_then_continues`：真实多工具轮 → 手动压缩 → `AgentSession.resume` 投影一致 → 同一条链继续真实请求。
- `test_real_compaction_keeps_tool_pairing`：真实压缩后 tool call/result 仍成对，工具不因压缩被重放。

## 实测指标（脱敏，`pytest -s` 打印）

```text
[m7.7b] provider/model: deepseek deepseek-flash
        entries_before: 6
        messages: 6 -> 3
        content_tokens: 25560 -> 767
        summary_chars: 1211
        tokens_before(provider): 52063
        summary_usage: (input 2631, output 520)
        continuation_chars: 9   # "continued"
```

| 检查 | 结果 |
| --- | --- |
| Session append-only | 压缩只追加 1 条 `CompactionEntry`，原 message entry 一条不删 |
| 投影规模 | 字符口径 25,560 → 767 tokens（`usage` 锚点仍是压缩前那次真实请求，不用于比较压缩效果） |
| tool 配对 | 保留区 tool call/result 成对；第二次压缩只说明“无新内容/无安全切点”，不改写原始消息 |
| 恢复一致 | `AgentSession.resume` 的投影角色与字符口径 token 与内存完全相同 |
| 压缩后继续 | 新任务在同一 leaf 上真实完成（`stop_reason=stop`） |
| 真实成本 | 摘要调用实测 input 2,631 / output 520 tokens；未做任何计费推断 |

## 边界说明

- 真实模型的窗口都是 1M 级（`KNOWN_CONTEXT_WINDOWS`），填满窗口触发**自动**压缩的成本不可接受，因此本次真实验收走的是手动 `/compact` 同一事务；窗口与成本两种自动触发由离线端到端用例覆盖（`tests/integration/test_auto_compaction.py`、`tests/session/test_cost_aware_compaction.py`）。
- 未测 Provider 明确标注：OpenAI 无 Key，`test_openai_complete` skip，未记录为通过。
- 用例只写 `tmp_path` 下的临时 Session；真实 `~/.mini-pi` 的 Session 与历史未被读写。
