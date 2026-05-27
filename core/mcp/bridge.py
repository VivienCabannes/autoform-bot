# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP-to-framework format converters."""

import json
from typing import Any

from mcp import types

from ..inference import ToolSchema


def mcp_tools_to_schemas(mcp_tools: list[types.Tool], *, prefix: str = "") -> list[ToolSchema]:
    """Convert MCP tool schemas to backend-agnostic ToolSchema objects."""
    schemas: list[ToolSchema] = []
    for tool in mcp_tools:
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        tool_name = f"{prefix}{str(tool.name)}" if prefix else str(tool.name)
        schemas.append(
            ToolSchema(
                name=tool_name,
                description=tool.description or "",
                parameters=schema,
            )
        )
    return schemas


def parse_tool_call_arguments(raw_arguments: str | None) -> dict[str, Any]:
    """Parse tool-call arguments JSON with a structured fallback."""
    if not raw_arguments:
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"_error": "invalid_json_arguments", "_raw": raw_arguments}
    if isinstance(parsed, dict):
        return parsed
    return {"_error": "arguments_not_object", "_raw": raw_arguments}


def mcp_call_result_to_tool_content(result: types.CallToolResult) -> str:
    """Normalize MCP CallToolResult into a stable JSON string for tool messages."""
    text_parts: list[str] = []
    raw_parts: list[str] = []

    for block in result.content:
        if isinstance(block, types.TextContent):
            text_parts.append(block.text)
            raw_parts.append(block.text)
        else:
            raw_parts.append(repr(block))

    payload = {
        "ok": not bool(getattr(result, "isError", False)),
        "text": "\n".join(text_parts).strip(),
        "structured": getattr(result, "structuredContent", None),
        "raw": raw_parts,
    }
    return json.dumps(payload, ensure_ascii=False)
