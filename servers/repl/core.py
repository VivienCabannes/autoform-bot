"""Lean REPL — single session managing a ``lake exe repl`` subprocess.

Stub module. See examples/servers/repl/core.py for a full implementation
with non-blocking I/O, import caching, memory monitoring, and auto-restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LeanReplConfig:
    """Configuration for a Lean REPL instance."""

    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    request_timeout: float = 30.0
    startup_timeout: float = 180.0
    repl_command: list[str] = field(default_factory=lambda: ["lake", "exe", "repl"])


def format_repl_response(response: dict[str, Any]) -> str:
    """Parse a raw REPL response and format it as readable diagnostics.

    Args:
        response: Raw JSON response from the Lean REPL subprocess.

    Returns:
        Human-readable diagnostic string (errors, warnings, sorries).
    """
    raise NotImplementedError("See examples/servers/repl/core.py for reference implementation.")
