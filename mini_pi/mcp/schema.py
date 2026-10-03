"""受支持 JSON Schema 子集与本地引用校验，不联网解析外部 schema。"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft7Validator, Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from mini_pi.errors import ToolArgumentError
from mini_pi.mcp.client import MCPError

MAX_SCHEMA_BYTES = 32_768
KEYWORDS = frozenset(
    {
        "$schema",
        "$ref",
        "$defs",
        "definitions",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "enum",
        "const",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "title",
        "description",
        "default",
        "examples",
        "minProperties",
        "maxProperties",
    }
)


def compile_schema(
    schema: dict[str, Any], *, parameters: bool = False
) -> Draft202012Validator | Draft7Validator:
    """仅接受 2020-12/draft-07 与无环本地 refs，Provider 参数根必须为 object。"""
    frozen = deepcopy(schema)
    if len(json.dumps(frozen, ensure_ascii=False).encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise MCPError("MCP schema exceeds 32 KB")
    dialect = frozen.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    validators = {
        "https://json-schema.org/draft/2020-12/schema": Draft202012Validator,
        "http://json-schema.org/draft-07/schema#": Draft7Validator,
    }
    if not isinstance(dialect, str) or dialect not in validators:
        raise MCPError("unsupported MCP schema dialect; use JSON Schema 2020-12 or draft-07")
    validator = validators[dialect]
    if parameters and frozen.get("type") != "object":
        raise MCPError("Provider tool parameters must declare an object root")
    try:
        validator.check_schema(frozen)
        _walk(frozen, frozen, (), 0, dialect)
    except (SchemaError, ValueError, re.error, RecursionError) as exc:
        raise MCPError("invalid or unsupported MCP schema") from exc
    return validator(frozen)


def _walk(node: Any, root: dict[str, Any], refs: tuple[str, ...], depth: int, dialect: str) -> None:
    """递归检查 schema 节点而非属性名称，拒绝未知关键词和递归引用。"""
    if depth > 24:
        raise ValueError("schema nesting exceeds limit")
    if isinstance(node, bool):
        return
    if not isinstance(node, dict) or set(node) - KEYWORDS:
        raise ValueError("unsupported schema keyword")
    if "$schema" in node and node["$schema"] != dialect:
        raise ValueError("unsupported nested dialect")
    if dialect.endswith("draft-07/schema#") and "prefixItems" in node:
        raise ValueError("prefixItems is not supported by draft-07")
    if "pattern" in node:
        re.compile(node["pattern"])
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in refs:
            raise ValueError("external or recursive ref is unsupported")
        target: Any = root
        for part in unquote(ref[2:]).split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or key not in target:
                raise ValueError("unresolved local ref")
            target = target[key]
        _walk(target, root, (*refs, ref), depth + 1, dialect)
    for key in ("properties", "$defs", "definitions"):
        for child in node.get(key, {}).values():
            _walk(child, root, refs, depth + 1, dialect)
    for key in ("additionalProperties", "items", "not"):
        if key in node:
            _walk(node[key], root, refs, depth + 1, dialect)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in node.get(key, []):
            _walk(child, root, refs, depth + 1, dialect)


def validate_arguments(
    validator: Draft202012Validator | Draft7Validator, arguments: dict[str, Any]
) -> dict[str, Any]:
    """验证原始 JSON 参数，不把 bool 当数值，不删字段或补默认值。"""
    try:
        raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > 50_000:
            raise ValueError("arguments exceed 50 KB")
        result = json.loads(raw)
        validator.validate(result)
        return result
    except (ValidationError, ValueError, TypeError, RecursionError) as exc:
        # 不回显 jsonschema 的实例正文，防止未审批的秘密被错误日志扩散。
        raise ToolArgumentError("MCP arguments do not match the declared JSON Schema") from exc
