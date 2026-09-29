# M8.1 CLI 终端验收

使用 FakeLLM 与真实 pty 运行 [`m8-1-tty-driver.py`](m8-1-tty-driver.py)，命令：

```bash
env -u NO_COLOR TERM=xterm-256color uv run python docs/benchmarks/m8-1-tty-driver.py
```

本次运行退出码为 0，`failed checks: none`。覆盖 40×12、80×8、120×24、220×24、`NO_COLOR`、`TERM=dumb` 和非 tty。检查默认不输出完整 Art、身份栏、`/last` 序号及有界详情提示、`/last 1 full` 的末行、凭据脱敏、TTY Markdown 与非 TTY 原文、Ctrl+C 取消。离线单元测试另覆盖分片 Markdown、代码围栏、中文折行、空白 Enter 和展示内容与模型/JSONL 边界。

这只证明固定事件脚本的终端行为；它不测真实 Provider 的模型质量，也不推断 Token 或费用收益。`/last N full` 只能显示 Tool 层已经捕获并保存的 observation，无法恢复工具截断前丢弃的字节。
