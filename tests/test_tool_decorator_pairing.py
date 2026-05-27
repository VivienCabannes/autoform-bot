# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Check that every @server.tool is paired with @ToolSpec.define(autonomy=...).

Parses tools/**/server.py files with ast and fails if any @server.tool-decorated
function is missing a @ToolSpec.define decorator.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.constants import REPO_ROOT


def _is_server_tool(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Attribute):
        return isinstance(decorator.value, ast.Name) and decorator.value.id == "server" and decorator.attr == "tool"
    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
        return (
            isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "server"
            and decorator.func.attr == "tool"
        )
    return False


def _is_toolspec_define(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
        return (
            isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "ToolSpec"
            and decorator.func.attr == "define"
        )
    return False


def _check_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_server_tool(d) for d in node.decorator_list):
            continue
        if not any(_is_toolspec_define(d) for d in node.decorator_list):
            violations.append(f"{path}:{node.lineno} — {node.name}() has @server.tool but no @ToolSpec.define")
    return violations


def test_tool_decorator_pairing() -> None:
    server_files = sorted(REPO_ROOT.glob("tools/**/server.py"))
    assert server_files, "no tools/**/server.py files found"

    violations = [v for path in server_files for v in _check_file(path)]
    assert not violations, "unpaired @server.tool decorators:\n" + "\n".join(violations)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
