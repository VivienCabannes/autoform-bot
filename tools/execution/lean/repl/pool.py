# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean REPL pool — thread pool of REPL instances with queue-based load balancing."""

from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from typing import Any

from core.process import RpcSessionPool

from .core import LeanRepl, LeanReplConfig
from .exceptions import ReplProcessRestarted

logger = getLogger(__name__)

DEFAULT_PORT = 8990
DEFAULT_RAM_FRACTION = 0.5
DEFAULT_STARTUP_STAGGER_SECONDS = 2.0


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of Lean REPL instances."""

    # Number of parallel REPL instances (None = auto-detect from system RAM)
    num_repls: int | None = None
    # Delay in seconds between worker cold starts to reduce startup contention.
    startup_stagger: float = DEFAULT_STARTUP_STAGGER_SECONDS
    # Host and port for the HTTP server
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        if self.num_repls is None:
            import psutil

            total_gb = psutil.virtual_memory().total / (1024**3)
            self.num_repls = max(1, int(total_gb * DEFAULT_RAM_FRACTION / self.instance_mem_limit_gb))


class LeanReplPool(RpcSessionPool[LeanReplConfig]):
    """Pool of Lean REPL instances with queue-based load balancing.

    Each worker thread owns its own LeanRepl subprocess. Tasks are
    distributed to idle workers via a FIFO queue. Import caching
    is per-instance.

    Handles ``ReplProcessRestarted`` by retrying transparently — the
    worker's REPL has already been restarted, so the task is simply
    re-submitted.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        super().__init__(
            args=config,
            session_factory=LeanRepl,
            capacity=config.num_repls,
            startup_stagger=config.startup_stagger,
        )

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Run a command, retrying once on ``ReplProcessRestarted``.

        When a REPL dies and is restarted, the worker is still usable —
        just the env_id-based state was lost.  For standard (no env_id)
        calls, retry transparently.
        """
        try:
            return super().run(*args, **kwargs)
        except ReplProcessRestarted:
            logger.info("REPL was restarted, retrying command...")
            return super().run(*args, **kwargs)

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        """Submit a command to the pool.

        env_id must not be provided — each worker has its own REPL
        with independent environment IDs.
        """
        assert "env_id" not in kwargs or kwargs["env_id"] is None, (
            "env_id should not be provided when using LeanReplPool"
        )
        return super().submit(*args, **kwargs)
