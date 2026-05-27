# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Auto-generate markdown documentation from the tool registry."""

from __future__ import annotations

import logging
from pathlib import Path

from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def generate_tool_docs(registry: ToolRegistry, output_dir: str | Path) -> list[Path]:
    """Write per-server markdown files from the tool registry.

    Creates one file per server at ``output_dir/<server_key>.md`` containing
    the server description and full documentation for each tool.

    Returns:
        List of paths to generated files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    for entry in sorted(registry.servers.values(), key=lambda e: e.key):
        content = registry.format_server_detail(entry.key)
        path = output_dir / f"{entry.key}.md"
        path.write_text(content, encoding="utf-8")
        generated.append(path)
        logger.debug("Generated tool docs: %s", path)

    return generated
