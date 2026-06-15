"""Lean REPL pool — thread pool of REPL instances with queue-based load balancing.

Stub module. See examples/servers/repl/pool.py for a full implementation
with queue-based dispatch, staggered startup, and memory monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import LeanReplConfig


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of Lean REPL instances."""

    num_repls: int | None = None
    startup_stagger: float = 2.0


class LeanReplPool:
    """Pool of Lean REPL instances with queue-based load balancing.

    Each worker thread owns its own LeanRepl subprocess. Tasks are
    distributed to idle workers via a FIFO queue.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        raise NotImplementedError("See examples/servers/repl/pool.py for reference implementation.")

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL, retrying once on restart."""
        raise NotImplementedError("See examples/servers/repl/pool.py for reference implementation.")

    def shutdown(self) -> None:
        """Shut down all REPL instances."""
        raise NotImplementedError("See examples/servers/repl/pool.py for reference implementation.")
