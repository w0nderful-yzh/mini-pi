"""白名单目录、原始 Schema、宿主权限和有界输出的行为验收。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mini_pi.config import ServerConfig, UserConfig
from mini_pi.errors import ToolError
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.mcp.catalog import tool_alias
from mini_pi.mcp.client import MCPError
from mini_pi.mcp.schema import compile_schema, validate_arguments
from mini_pi.security import SecurityPolicy
from mini_pi.tools import build_default_registry
from mini_pi.tools.registry import ToolRegistry
from mini_pi.workspace.workspace import Workspace
from tests.mcp.test_client import config, events


def test_percent_encoded_local_ref_uses_json_schema_resolution() -> None:
    """宿主安全检查和实际验证按同一百分号解码规则解析本地引用。"""
    schema = {
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/%61"}},
        "$defs": {"a": {"type": "integer"}},
    }
    assert validate_arguments(compile_schema(schema, parameters=True), {"value": 2}) == {"value": 2}
    with pytest.raises(MCPError):
        compile_schema(
            {"type": "object", "$ref": "#/$defs/%61", "$defs": {"%61": {"type": "object"}}},
            parameters=True,
        )


def registry(
    tmp_path: Path,
    mode: str = "normal",
    *,
    permissions: str = "workspace",
    decisions: list[ApprovalRequest] | None = None,
    deny_call: bool = False,
) -> ToolRegistry:
    """记录 startup 与工具审批，server 声明只读不会影响 mutate 的宿主分类。"""

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """精确拒绝工具调用时仍允许显式目录启动。"""
        if decisions is not None:
            decisions.append(request)
        return ApprovalResponse(
            "deny" if deny_call and request.identity.startswith('["mcp"') else "allow"
        )

    configured = config(tmp_path, mode).model_copy(update={"permissions": permissions})
    return build_default_registry(
        Workspace(tmp_path),
        security=SecurityPolicy(cwd=tmp_path, config=configured, approval=approve),
    )


def test_catalog_is_lazy_whitelisted_and_identity_bound(tmp_path: Path) -> None:
    """注册阶段仍为默认八工具，发现后只有白名单增加且原始 refs/required 保留。"""
    approvals: list[ApprovalRequest] = []
    active = registry(tmp_path, decisions=approvals)
    assert len(active.schemas()) == 8 and not (tmp_path / "events.jsonl").exists()
    try:
        active.prepare_mcp()
        assert len(active.schemas()) == 10
        alias = tool_alias("local", "calc.add")
        schema = next(item for item in active.schemas() if item.name == alias)
        assert (
            schema.parameters["required"] == ["value"]
            and schema.parameters["properties"]["value"]["$ref"] == "#/$defs/number"
        )
        assert "local/calc.add" in schema.description and "Untrusted server" in schema.description
        result = json.loads(active.execute(alias, {"value": 2}).content)
        assert result["structuredContent"] == {"answer": 3} and result["source"] == {
            "server": "local",
            "tool": "calc.add",
        }
        assert len(approvals) == 1
        active.execute(tool_alias("local", "mutate"), {"value": 3})
        assert len(approvals) == 2 and json.loads(approvals[-1].identity) == [
            "mcp",
            "local",
            "mutate",
        ]
        assert [
            item["params"]["name"]
            for item in events(tmp_path)
            if item.get("method") == "tools/call"
        ] == ["calc.add", "mutate"]
    finally:
        active.close()


@pytest.mark.parametrize("failure", ["missing", "description", "schema", "tasks"])
def test_unsupported_directory_never_activates(failure: str, tmp_path: Path) -> None:
    """缺白名单、目录超限和必须采用 tasks 时关闭候选连接且不安装任何工具。"""
    listed = [{"name": name, "inputSchema": {"type": "object"}} for name in ("calc.add", "mutate")]
    if failure == "missing":
        listed.pop()
    elif failure == "description":
        listed[0]["description"] = "x" * 4097
    elif failure == "schema":
        listed[0]["inputSchema"]["description"] = "x" * 32768
    else:
        listed[0]["execution"] = {"taskSupport": "required"}
    (tmp_path / "catalog.json").write_text(json.dumps(listed))
    active = registry(tmp_path)
    try:
        with pytest.raises(MCPError):
            active.prepare_mcp()
        assert len(active.schemas()) == 8 and active._mcp.clients == {}
        assert not any(item.get("method") == "tools/call" for item in events(tmp_path))
    finally:
        active.close()


@pytest.mark.parametrize(
    "arguments", [{}, {"value": True}, {"value": "2"}, {"value": 2, "extra": 3}]
)
def test_argument_failure_precedes_approval_and_call(arguments: dict, tmp_path: Path) -> None:
    """缺必填、bool、隐式转换与额外字段都在权限检查前明确拒绝。"""
    approvals: list[ApprovalRequest] = []
    active = registry(tmp_path, decisions=approvals)
    try:
        active.prepare_mcp()
        with pytest.raises(ToolError):
            active.execute(tool_alias("local", "mutate"), arguments)
        assert len(approvals) == 1 and not (tmp_path / "calls.txt").exists()
    finally:
        active.close()


@pytest.mark.parametrize("permissions,deny_call", [("readonly", False), ("workspace", True)])
def test_annotations_never_grant_access(permissions: str, deny_call: bool, tmp_path: Path) -> None:
    """readonly 与单次拒绝均阻止真实 tools/call，server readOnlyHint 不授权。"""
    active = registry(tmp_path, permissions=permissions, deny_call=deny_call)
    try:
        active.prepare_mcp()
        with pytest.raises(ToolError):
            active.execute(tool_alias("local", "mutate"), {"value": 1})
        assert not (tmp_path / "calls.txt").exists()
        active.execute(tool_alias("local", "calc.add"), {"value": 1})
        assert (tmp_path / "calls.txt").read_text().splitlines() == ["calc.add"]
    finally:
        active.close()


@pytest.mark.parametrize(
    "mode",
    ["bad_output", "no_structured", "image", "audio", "resource", "is_error", "invalid_result"],
)
def test_invalid_unsupported_and_is_error_outputs_fail(mode: str, tmp_path: Path) -> None:
    """输出不符约束、缺结构、图片和 isError 都成为错误观察，调用仅一次。"""
    active = registry(tmp_path, mode)
    try:
        active.prepare_mcp()
        with pytest.raises(ToolError):
            active.execute(tool_alias("local", "calc.add"), {"value": 1})
        assert (tmp_path / "calls.txt").read_text().splitlines() == ["calc.add"]
    finally:
        active.close()


def test_huge_output_is_complete_bounded_json(tmp_path: Path) -> None:
    """超大结果不能破坏 JSON 或声称保留完整结构化输出。"""
    active = registry(tmp_path, "huge")
    try:
        active.prepare_mcp()
        result = active.execute(tool_alias("local", "calc.add"), {"value": 1})
        parsed = json.loads(result.content)
        assert parsed["truncated"] and parsed["structuredContent"] is None and parsed["text"]
        assert len(result.content.encode()) <= 50_000 and result.modified_files == []
    finally:
        active.close()


def test_changed_notification_freezes_run_and_reload_is_explicit(tmp_path: Path) -> None:
    """通知不能动态增加 schema，运行中刷新拒绝，空闲刷新才更新白名单。"""
    active = registry(tmp_path, "changed")
    try:
        active.begin_run()
        snapshot = active.schemas()
        active.execute(tool_alias("local", "calc.add"), {"value": 1})
        assert active.schemas() == snapshot and "needs /mcp reload" in active.mcp_status
        with pytest.raises(ValueError, match="idle"):
            active.reload_mcp(UserConfig())
        active.end_run()
        active.reload_mcp(UserConfig())
        assert len(active.schemas()) == 8
        with pytest.raises(ToolError):
            active.execute(tool_alias("local", "calc.add"), {"value": 1})
    finally:
        active.end_run()
        active.close()


def test_long_unicode_names_and_collision_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点号、超长、Unicode 名称稳定编码；同别名冲突明确失败而非猜测路由。"""
    names = ["calc.add", "x" * 800, "中文.查询"]
    aliases = [tool_alias("local", name) for name in names]
    assert len(set(aliases)) == 3 and all(
        re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) for alias in aliases
    )
    listed = [{"name": name, "inputSchema": {"type": "object"}} for name in names]
    (tmp_path / "catalog.json").write_text(json.dumps(listed))
    configured = config(tmp_path)
    server = configured.servers["local"].model_copy(
        update={"tools": tuple(names), "readonly_tools": tuple(names)}
    )
    active = build_default_registry(
        Workspace(tmp_path),
        security=SecurityPolicy(
            cwd=tmp_path,
            config=UserConfig(servers={"local": server}),
            approval=lambda request: ApprovalResponse("allow"),
        ),
    )
    try:
        active.prepare_mcp()
        assert set(active._mcp.tools) == set(aliases)
        assert [active._mcp.tools[alias].original_name for alias in aliases] == names
        active.close()
        monkeypatch.setattr("mini_pi.mcp.catalog.tool_alias", lambda *args: "mcp_collision")
        with pytest.raises(MCPError, match="collision"):
            active.prepare_mcp()
        assert len(active.schemas()) == 8
    finally:
        active.close()


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$ref": "https://example.com/schema"},
        {"type": "object", "$ref": "#/$defs/self", "$defs": {"self": {"$ref": "#/$defs/self"}}},
        {"type": "object", "$id": "https://example.com/root", "properties": {}},
        {"type": "object", "required": ["missing"], "properties": {}, "format": "custom"},
        {"type": "array"},
        {"type": "object", "$schema": "https://example.com/unknown"},
    ],
)
def test_unsupported_schema_is_never_weakened(schema: dict) -> None:
    """外部/循环引用、未知关键词、非参数 object 和未知方言都明确失败。"""
    with pytest.raises(MCPError):
        compile_schema(schema, parameters=True)


def test_draft7_local_ref_preserves_constraints() -> None:
    """参考 filesystem server 使用 draft-07，同样原样验证本地 required 和类型。"""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"x": {"$ref": "#/definitions/integer"}},
        "definitions": {"integer": {"type": "integer", "minimum": 3}},
        "required": ["x"],
    }
    validator = compile_schema(schema, parameters=True)
    assert validate_arguments(validator, {"x": 3}) == {"x": 3}
    with pytest.raises(ToolError):
        validate_arguments(validator, {"x": 2})


def test_configuration_rejects_bad_whitelist() -> None:
    """可信只读必须在白名单内，重复和总量超限不能隐式裁剪。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ServerConfig(kind="mcp", argv=("server",), readonly_tools=("unlisted",))
    with pytest.raises(ValidationError):
        ServerConfig(kind="mcp", argv=("server",), tools=("same", "same"))
    with pytest.raises(ValidationError):
        UserConfig(
            servers={str(index): ServerConfig(kind="mcp", argv=("server",)) for index in range(5)}
        )
