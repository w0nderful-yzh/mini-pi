"""固定参考 MCP 两工具，比较实际 Provider input usage 与同批请求估算。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mini_pi.auth import resolve_api_key
from mini_pi.config import ServerConfig, UserConfig
from mini_pi.context.request import estimate_request
from mini_pi.interaction import ApprovalResponse
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import to_openai_tools
from mini_pi.llm.types import SystemMessage, UserMessage
from mini_pi.security import SecurityPolicy
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace


def main() -> None:
    """仅人工显式运行时调用 DeepSeek，不自动安装 server 或打印凭据。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    key = resolve_api_key("deepseek", env_var="DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DeepSeek API key is not configured")
    binary = args.server.resolve()
    version = json.loads((binary.parents[1] / "package.json").read_text())["version"]
    if version != "2026.8.31":
        raise RuntimeError("expected filesystem server 2026.8.31")
    messages = [
        SystemMessage(content="Reply with the single word READY. Do not call tools."),
        UserMessage(content="READY"),
    ]
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="mini-pi-m11-input-") as folder:
        root = Path(folder)
        config = UserConfig(
            servers={
                "files": ServerConfig(
                    kind="mcp",
                    argv=(str(binary), str(root)),
                    tools=("read_file", "list_directory"),
                    readonly_tools=("read_file", "list_directory"),
                    timeout=20,
                )
            }
        )
        active = build_default_registry(
            Workspace(root),
            security=SecurityPolicy(
                cwd=root, config=config, approval=lambda request: ApprovalResponse("allow")
            ),
        )
        llm = DeepSeekClient(api_key=key)
        try:
            for variant in ("default", "mcp_two"):
                if variant == "mcp_two":
                    active.prepare_mcp()
                tools = active.schemas()
                prediction = estimate_request(messages, tools, provider="deepseek", model=llm.model)
                result = llm.complete(messages, tools=tools)
                if result.usage is None or result.stop_reason != "stop" or result.tool_calls:
                    raise RuntimeError("expected one measured no-tool completion")
                measured = result.usage.input_tokens
                row = {
                    "variant": variant,
                    "tool_count": len(tools),
                    "tool_wire_bytes": len(
                        json.dumps(to_openai_tools(tools), ensure_ascii=False).encode()
                    ),
                    "estimated_input_tokens": prediction.input_tokens,
                    "measured_input_tokens": measured,
                    "error_percent": round(
                        (prediction.input_tokens - measured) / measured * 100, 2
                    ),
                    "output_tokens": result.usage.output_tokens,
                }
                rows.append(row)
                print(json.dumps(row))
        finally:
            active.close()
            llm._client.close()
    args.output.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "provider": "deepseek",
                "model": llm.model,
                "sdk": "mcp 1.26.0",
                "reference_server": f"filesystem {version}",
                "requests": rows,
                "scope": "same short messages; no tool calls; one sample per catalog; not a savings claim",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
