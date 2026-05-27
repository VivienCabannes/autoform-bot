# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean REPL — single session managing a ``lake exe repl`` subprocess.

Provides LeanRepl (RpcSession subclass) with non-blocking I/O,
a single preloaded import environment, memory monitoring, automatic
restart, import validation, line number adjustment, and multi-snippet
chaining.

Features include buffer size limits, proactive memory-based restart,
and structured error helpers.
"""

from __future__ import annotations

import json
import os
import random
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

from core.process import (
    MemoryMonitor,
    MemoryUnit,
    RpcSession,
    get_process_memory_usage,
    inherit_clean_env,
    kill_subprocesses,
)
from tools.execution.lean.constant import ALLOWED_IMPORTS, WARMUP_IMPORTS
from tools.execution.lean.parsing import split_imports_and_body

from .exceptions import ReplProcessExited, ReplProcessRestarted

logger = getLogger(__name__)

DEFAULT_MAX_DIAGNOSTICS = 10
DEFAULT_SMOKE_TEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LeanReplConfig:
    """Configuration for a Lean REPL instance."""

    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)

    request_timeout: float = 30.0
    startup_timeout: float = 180.0
    chunk_size: int = 4096

    # Memory limit per instance in GB (0 = no limit)
    instance_mem_limit_gb: int = 16
    # Seconds between memory checks
    mem_interval_check: float = 1.0

    max_retries: int = 1

    # Import roots to load at startup (e.g. frozenset({"Mathlib"})).
    allowed_imports: frozenset[str] = ALLOWED_IMPORTS

    # Import roots preloaded at REPL startup. Subset of allowed_imports —
    # only what's needed to bootstrap; the rest comes transitively.
    warmup_imports: frozenset[str] = WARMUP_IMPORTS

    # Command to start the REPL. Defaults to ["lake", "exe", "repl"].
    repl_command: list[str] = field(default_factory=lambda: ["lake", "exe", "repl"])

    # Cap on REPL response buffer in bytes.  Prevents runaway memory
    # usage from pathological REPL output.
    max_buffer_bytes: int = 10 * 1024 * 1024

    # Proactive restart threshold: restart the REPL when memory usage
    # exceeds ``mem_restart_ratio * instance_mem_limit_gb``.
    mem_restart_ratio: float = 0.9

    # Validate submitted imports against ``allowed_imports`` roots.
    validate_imports: bool = True


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


def _adjust_line_numbers(resp: dict, offset: int) -> None:
    """Offset all ``pos.line`` values in messages so they map back to original source.

    ``offset`` is the number of header lines that were stripped before
    sending the body to the REPL.  Each message's ``pos.line`` is
    incremented by ``offset`` to report positions relative to the
    original submitted code.
    """
    if offset == 0:
        return
    for msg in resp.get("messages", []):
        pos = msg.get("pos")
        if pos and isinstance(pos, dict) and "line" in pos:
            pos["line"] = pos["line"] + offset
        end_pos = msg.get("endPos")
        if end_pos and isinstance(end_pos, dict) and "line" in end_pos:
            end_pos["line"] = end_pos["line"] + offset
    for sorry in resp.get("sorries", []):
        pos = sorry.get("pos")
        if pos and isinstance(pos, dict) and "line" in pos:
            pos["line"] = pos["line"] + offset
        end_pos = sorry.get("endPos")
        if end_pos and isinstance(end_pos, dict) and "line" in end_pos:
            end_pos["line"] = end_pos["line"] + offset


def format_message(msg: dict) -> str:
    """Format one REPL message: ``"3:5: error: unknown identifier"``.

    Falls back to ``"error: data"`` when ``pos`` is absent.
    """
    severity = msg.get("severity", "info")
    data = msg.get("data", "")
    pos = msg.get("pos")

    if pos and isinstance(pos, dict):
        line = pos.get("line")
        column = pos.get("column")
        if line is not None:
            if column is not None:
                return f"{line}:{column}: {severity}: {data}"
            return f"{line}: {severity}: {data}"

    return f"{severity}: {data}"


def format_repl_response(response: dict[str, Any]) -> str:
    """Parse a raw REPL response and format it as readable diagnostics.

    Classifies the result as success, error, or has-sorry, and formats
    errors, warnings, and sorry goals into a structured string.

    Args:
        response: Raw dict from LeanRepl.run().

    Returns:
        Formatted diagnostic string.
    """
    if response.get("repl_error") is not None:
        return f"REPL error: {response['repl_error']}"

    messages = response.get("messages", [])
    sorries_raw = response.get("sorries", [])

    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sev = msg.get("severity", "")
        if sev == "error":
            errors.append(format_message(msg))
        elif sev == "warning":
            warnings.append(format_message(msg))
        elif sev == "info":
            infos.append(format_message(msg))

    sorries: list[dict[str, Any]] = []
    for s in sorries_raw:
        if not isinstance(s, dict):
            continue
        pos = s.get("pos", {})
        sorries.append(
            {
                "line": pos.get("line", 0) if isinstance(pos, dict) else 0,
                "goal": s.get("goal", ""),
            }
        )

    parts: list[str] = []

    # Priority cascade: errors > warnings > infos
    if errors:
        parts.append(f"Compilation Errors ({len(errors)})")
        for e in errors:
            parts.append(f"  - {e}")
    elif warnings:
        parts.append("Compiles successfully")
        parts.append(f"\nWarnings ({len(warnings)})")
        for w in warnings[:DEFAULT_MAX_DIAGNOSTICS]:
            parts.append(f"  - {w}")
        if len(warnings) > DEFAULT_MAX_DIAGNOSTICS:
            parts.append(f"  ... and {len(warnings) - DEFAULT_MAX_DIAGNOSTICS} more")
    elif infos:
        parts.append("Compiles successfully")
        parts.append(f"\nOutput ({len(infos)})")
        for i in infos[:DEFAULT_MAX_DIAGNOSTICS]:
            parts.append(f"  - {i}")
        if len(infos) > DEFAULT_MAX_DIAGNOSTICS:
            parts.append(f"  ... and {len(infos) - DEFAULT_MAX_DIAGNOSTICS} more")
    else:
        parts.append("Compiles successfully")

    if sorries:
        parts.append(f"\nSorries ({len(sorries)})")
        for s in sorries:
            parts.append(f"  - Line {s['line']}: {s['goal']}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LeanRepl
# ---------------------------------------------------------------------------


class LeanRepl(RpcSession[LeanReplConfig]):
    """Lean REPL process manager.

    Manages a ``lake exe repl`` subprocess with non-blocking I/O,
    a single preloaded import environment, and automatic restart on failure.
    """

    def __init__(self, config: LeanReplConfig) -> None:
        self.config = config
        self.cwd = config.cwd
        self.process: subprocess.Popen | None = None

        self.request_timeout = config.request_timeout
        self.max_retries = config.max_retries

        self._base_env_id: int | None = None
        self.chunk_size: int = config.chunk_size

        self.mem_limit_gb: int = config.instance_mem_limit_gb
        self._memory_monitor: MemoryMonitor | None = None

        self._process_lock = threading.Lock()

        # Precompute allowed import roots for validation
        self._allowed_import_roots: frozenset[str] | None = None
        if config.validate_imports and config.allowed_imports:
            self._allowed_import_roots = config.allowed_imports

    def start(self) -> None:
        """Start the Lean REPL process."""
        env = inherit_clean_env()
        env.update(self.config.env)

        self.process = subprocess.Popen(
            self.config.repl_command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        try:
            if self.mem_limit_gb > 0:
                self._memory_monitor = MemoryMonitor(
                    self.process,
                    self.mem_limit_gb,
                    self.config.mem_interval_check,
                )
                self._memory_monitor.start()

            # Pre-load warmup imports so agents don't pay the cold-start cost
            if self.config.warmup_imports:
                header = "\n".join(f"import {root}" for root in self.config.warmup_imports)
                logger.info("Loading imports at startup: %s", self.config.warmup_imports)
                resp = self._run(code=header, env_id=None, timeout=self.config.startup_timeout)
                if "env" not in resp:
                    raise RuntimeError(f"Failed to preload imports: {resp}")

                # Check for errors in the preload response — the REPL may return an env
                # even when imports fail, resulting in a broken base environment.
                errors = [m for m in resp.get("messages", []) if isinstance(m, dict) and m.get("severity") == "error"]
                if errors:
                    error_details = "\n".join(m.get("data", str(m)) for m in errors)
                    raise RuntimeError(f"Import preloading failed:\n{error_details}")

                self._base_env_id = resp["env"]

                # Smoke test: verify the base environment has the standard library loaded.
                # The REPL can return an env even when LEAN_PATH is wrong, producing an
                # environment with only kernel primitives (Prop, Type, axiom).
                smoke = self._run(code="#check Nat", env_id=self._base_env_id, timeout=DEFAULT_SMOKE_TEST_TIMEOUT)
                smoke_errors = [
                    m for m in smoke.get("messages", []) if isinstance(m, dict) and m.get("severity") == "error"
                ]
                if smoke_errors:
                    error_details = "; ".join(m.get("data", str(m)) for m in smoke_errors)
                    raise RuntimeError(
                        f"REPL environment smoke test failed — imports loaded but standard "
                        f"library is not available. This usually means LEAN_PATH is "
                        f"misconfigured or the project's .lake cache is stale. "
                        f"Errors: {error_details}"
                    )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the Lean REPL process and clean up resources."""
        try:
            if self._memory_monitor is not None:
                self._memory_monitor.stop()
                self._memory_monitor = None

            if not self.process or self.process.poll() is not None:
                return
            kill_subprocesses(self.process)
        finally:
            self.process = None
            self._base_env_id = None

    def restart(self) -> None:
        """Restart the Lean REPL process."""
        self.close()
        self.start()

    def is_alive(self) -> bool:
        """Check if the REPL process is alive."""
        return self.process is not None and self.process.poll() is None

    def get_memory_usage(self, unit: MemoryUnit = "GB") -> float:
        """Return memory usage of the REPL process and its children."""
        return get_process_memory_usage(self.process, unit=unit)

    def run(self, code: str, env_id: int | None = None, timeout: float | None = None) -> dict[str, Any]:
        """Send code to the REPL and get the response.

        If no env is provided, strips imports from the code and runs
        against the preloaded base environment.  When ``validate_imports``
        is enabled, submitted imports are checked against the allowed roots.
        Line numbers in the response are adjusted to match the original source.
        """
        timeout = timeout or self.request_timeout
        run_from_env = env_id is not None
        max_retries = 0 if run_from_env else self.max_retries

        header_line_count = 0
        if not run_from_env:
            imports, code, header_line_count = split_imports_and_body(code)

            # Import validation
            if self.config.validate_imports and self._allowed_import_roots is not None:
                submitted_roots = {stmt.split(".")[0] for stmt in imports}
                disallowed = submitted_roots - self._allowed_import_roots
                if disallowed:
                    return {
                        "repl_error": (
                            f"Disallowed imports: {', '.join(sorted(disallowed))}. "
                            f"Allowed roots: {', '.join(sorted(self._allowed_import_roots))}."
                        )
                    }

            env_id = self._base_env_id

        last_exception: Exception | None = None
        with self._process_lock:
            self._check_memory_and_maybe_restart()
            for i in range(max_retries + 1):
                try:
                    resp = self._run(code=code, env_id=env_id, timeout=timeout)
                    _adjust_line_numbers(resp, header_line_count)
                    return resp
                except ReplProcessExited as e:
                    last_exception = e
                    logger.error(
                        "REPL process exited: %s. Attempt %d/%d.",
                        e,
                        i + 1,
                        max_retries + 1,
                    )
                    if not run_from_env and i < max_retries:
                        # Backoff before restarting to reduce contention
                        backoff = min(2**i, 30) + random.uniform(0, 1)
                        time.sleep(backoff)
                    self.restart()
                    if run_from_env:
                        # Caller holds state via env_id — they must retry
                        raise ReplProcessRestarted(str(e)) from e
                except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                    last_exception = e
                    logger.error(
                        "Error running command: %s. Attempt %d/%d.",
                        e,
                        i + 1,
                        max_retries + 1,
                    )
                    if not run_from_env and i < max_retries:
                        # Backoff before restarting to reduce contention
                        backoff = min(2**i, 30) + random.uniform(0, 1)
                        time.sleep(backoff)
                    self.restart()
                    if run_from_env:
                        raise ReplProcessRestarted(str(e)) from e
            logger.error("Exceeded maximum retries for Lean REPL command")
            return {"repl_error": str(last_exception)}

    def run_steps(self, snippets: list[str], timeout: float | None = None) -> list[dict]:
        """Chain REPL envs sequentially across snippets.

        Short-circuits on first error. Returns list of raw response dicts.

        Args:
            snippets: List of Lean code snippets to run in sequence.
            timeout: Timeout per snippet in seconds.

        Returns:
            List of raw response dicts, one per executed snippet.
        """
        results: list[dict] = []
        env_id: int | None = None

        for snippet in snippets:
            if not snippet.strip():
                continue

            kwargs: dict[str, Any] = {}
            if env_id is not None:
                kwargs["env_id"] = env_id
            if timeout is not None:
                kwargs["timeout"] = timeout

            resp = self.run(snippet, **kwargs)
            results.append(resp)

            # Short-circuit on error
            if resp.get("repl_error"):
                break
            has_errors = any(
                isinstance(msg, dict) and msg.get("severity") == "error" for msg in resp.get("messages", [])
            )
            if has_errors:
                break

            env_id = resp.get("env", env_id)

        return results

    def _check_memory_and_maybe_restart(self) -> None:
        """Proactively restart the REPL if memory usage is near the limit.

        Best-effort: failures are logged and swallowed.
        """
        if self.mem_limit_gb <= 0 or self.config.mem_restart_ratio <= 0:
            return
        try:
            usage_gb = self.get_memory_usage(unit="GB")
            threshold_gb = self.mem_limit_gb * self.config.mem_restart_ratio
            if usage_gb >= threshold_gb:
                logger.info(
                    "REPL memory %.2fGB >= threshold %.2fGB, proactively restarting...",
                    usage_gb,
                    threshold_gb,
                )
                self.restart()
        except Exception:
            logger.warning("Memory check failed, continuing", exc_info=True)

    def _run(self, code: str, env_id: int | None, timeout: float) -> dict[str, Any]:
        """Send code to the REPL and get the response.

        Uses non-blocking I/O via select() to avoid deadlocks.
        Raises ``ReplProcessExited`` when the process dies and
        ``RuntimeError`` when the response buffer exceeds the configured
        limit.
        """
        cmd_obj: dict[str, Any] = {"cmd": code}
        if env_id is not None:
            cmd_obj["env"] = env_id
        command = json.dumps(cmd_obj) + "\n\n"

        if self.process is None or self.process.poll() is not None:
            raise self._process_termination_error("")

        self.process.stdin.write(command.encode("utf-8"))
        self.process.stdin.flush()

        stdout_fd = self.process.stdout.fileno()
        stderr_fd = self.process.stderr.fileno()

        response_buffer = ""
        stderr_buffer = ""
        end_time = time.monotonic() + timeout
        max_buffer = self.config.max_buffer_bytes

        while True:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"REPL command timed out after {timeout} seconds")

            ready, _, _ = select.select([stdout_fd, stderr_fd], [], [], remaining)
            if not ready:
                raise TimeoutError(f"REPL command timed out after {timeout} seconds")

            if stdout_fd in ready:
                chunk_bytes = os.read(stdout_fd, self.chunk_size)
                if not chunk_bytes:
                    raise self._process_termination_error(stderr_buffer)
                chunk = chunk_bytes.decode("utf-8", errors="replace")
                response_buffer += chunk

                # Buffer size guard
                if len(response_buffer) > max_buffer:
                    raise RuntimeError(
                        f"REPL response exceeded {max_buffer} bytes "
                        f"without producing valid JSON. Tail: {response_buffer[-200:]!r}"
                    )

                if "\n\n" in response_buffer:
                    response_str, _ = response_buffer.split("\n\n", 1)
                    response_str = response_str.strip()
                    break

            if stderr_fd in ready:
                err_chunk_bytes = os.read(stderr_fd, self.chunk_size)
                if err_chunk_bytes:
                    err_chunk = err_chunk_bytes.decode("utf-8", errors="replace")
                    stderr_buffer += err_chunk
                    logger.debug("Lean REPL stderr: %s", err_chunk.rstrip())

        return json.loads(response_str)

    def _process_termination_error(self, stderr_buffer: str) -> ReplProcessExited:
        """Build a descriptive error when the REPL process dies unexpectedly."""
        returncode = None
        if self.process:
            returncode = self.process.poll()
            if returncode is None:
                try:
                    self.process.wait(timeout=0.01)
                    returncode = self.process.returncode
                except subprocess.TimeoutExpired:
                    pass

        if self._memory_monitor and self._memory_monitor.exceeded:
            msg = f"Lean REPL killed for exceeding memory limit ({self.mem_limit_gb}GB)."
        else:
            msg = "Lean REPL process terminated unexpectedly."

        msg += f" Exit code: {returncode}, stderr: {stderr_buffer}"
        return ReplProcessExited(msg)
