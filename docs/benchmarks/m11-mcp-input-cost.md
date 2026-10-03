# M11：MCP 工具目录的输入估算基线

2026-10-03 使用真实 DeepSeek `deepseek-flash`，固定 `mcp==1.26.0` 与官方
`@modelcontextprotocol/server-filesystem@2026.8.31`。两个请求使用相同短消息，要求回复
READY，不调用工具；每个目录只采样一次。原始结果见 [JSON](m11-mcp-input-cost.json)，
复现脚本见 [Python](m11-mcp-input-cost.py)。

| 目录 | 工具数 | 工具 wire 字节 | estimated input | Provider input | 偏差 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认工具 | 8 | 5351 | 1591 | 1611 | −1.24% |
| 默认 + read_file/list_directory | 10 | 6648 | 1914 | 1925 | −0.57% |

这里验证新增目录进入当前请求估算；未修改估算参数。样本不证明真实任务节省成本，
也不能推广为 8–32 工具、LSP、长上下文或其他 Provider 的完整校准。OpenAI 未配置，
本次只做离线 wire 兼容测试。MCP 真实参考 server 的读/列目录/写入审批与关闭测试
使用 FakeLLM，真实模型跨能力闭环在 M12 验收。

手动安装固定 server 后复现；脚本会发送两次真实 API 请求，密钥从既有环境/用户
认证文件读取，不输出或保存凭据。临时前缀和输出位置可自行选择。

```sh
npm install --prefix /tmp/mini-pi-m11-reference --no-audit --no-fund @modelcontextprotocol/server-filesystem@2026.8.31
uv run python docs/benchmarks/m11-mcp-input-cost.py --server /tmp/mini-pi-m11-reference/node_modules/.bin/mcp-server-filesystem --output /tmp/m11-mcp-input-cost.json
env -u NO_COLOR TERM=xterm-256color MINI_PI_TEST_MCP_FILESYSTEM=/tmp/mini-pi-m11-reference/node_modules/.bin/mcp-server-filesystem uv run pytest -q tests/mcp
```
