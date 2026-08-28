"""Lean REPL backend: one session managing a ``lake exe repl`` subprocess.

Provides LeanRepl with non-blocking I/O, a preloaded import environment,
memory monitoring, automatic restart, and multi-snippet chaining.
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

logger = getLogger(__name__)

DEFAULT_MAX_DIAGNOSTICS = 10
DEFAULT_SMOKE_TEST_TIMEOUT = 10
DEFAULT_REPL_STARTUP_TIMEOUT = 180.0

ALLOWED_IMPORTS = frozenset({"Mathlib", "Aesop", "Batteries", "LeanSearchClient"})
WARMUP_IMPORTS = frozenset({"Mathlib"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_process_memory_gb(process: subprocess.Popen | None) -> float:
    """Return memory usage of a process and its children in GB."""
    if process is None or process.poll() is not None:
        return 0.0
    try:
        import psutil

        parent = psutil.Process(process.pid)
        total = parent.memory_info().rss
        for child in parent.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total / (1024**3)
    except Exception:
        return 0.0


def _kill_subprocesses(process: subprocess.Popen) -> None:
    """Kill a process and all its children."""
    try:
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        parent.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass


def _inherit_clean_env() -> dict[str, str]:
    """Return a copy of the current environment without PYTHONPATH noise."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _split_imports_and_body(code: str) -> tuple[list[str], str, int]:
    """Split Lean code into import statements and body.

    Returns (import_names, body, header_line_count).
    """
    lines = code.split("\n")
    imports: list[str] = []
    body_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped[7:].strip())
            body_start = i + 1
        elif stripped == "" or stripped.startswith("--"):
            if imports:
                body_start = i + 1
        else:
            break

    body = "\n".join(lines[body_start:])
    return imports, body, body_start


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LeanReplConfig:
    """Configuration for a Lean REPL instance."""

    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)

    request_timeout: float = 30.0
    startup_timeout: float = DEFAULT_REPL_STARTUP_TIMEOUT
    chunk_size: int = 4096

    instance_mem_limit_gb: int = 16
    mem_interval_check: float = 1.0
    max_retries: int = 1

    allowed_imports: frozenset[str] = ALLOWED_IMPORTS
    warmup_imports: frozenset[str] = WARMUP_IMPORTS

    repl_command: list[str] = field(default_factory=lambda: ["lake", "exe", "repl"])

    # stdout is capped per response. stderr has no protocol framing, so its
    # ceiling applies to the entire process generation and resets on restart.
    max_buffer_bytes: int = 10 * 1024 * 1024
    mem_restart_ratio: float = 0.9
    validate_imports: bool = True


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


def _adjust_line_numbers(resp: dict, offset: int) -> None:
    """Offset all pos.line values so they map back to original source."""
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
    """Format one REPL message: ``"3:5: error: unknown identifier"``."""
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
    """Parse a raw REPL response and format it as readable diagnostics."""
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


class ReplProcessExited(RuntimeError):
    """Raised when the REPL process dies unexpectedly."""


class ReplProcessRestarted(RuntimeError):
    """Raised when the REPL restarts and env_id state is lost."""


class ReplOutcomeUnknown(ReplProcessRestarted):
    """Raised when stderr poisoning leaves a sent command's outcome unknown."""


class ReplStderrBacklog(RuntimeError):
    """Raised when a response was captured but process stderr is no longer safe.

    The response data is valid and travels on ``response`` so a caller need not
    recompute it, but any ``env`` belongs to the process being retired and must
    not escape. stderr is unframed process output rather than command output, so
    an over-budget or undrainable process must not serve another request.
    """

    def __init__(self, message: str, response: dict[str, Any]) -> None:
        super().__init__(message)
        self.response = response


class LeanRepl:
    """Lean REPL process manager.

    Manages a ``lake exe repl`` subprocess with non-blocking I/O,
    a preloaded import environment, and automatic restart on failure.
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

        self._process_lock = threading.Lock()
        # stderr has no command boundary. Account for it monotonically across one
        # process generation and retain only a bounded tail for diagnostics.
        self._stderr_bytes = 0
        self._stderr_tail = bytearray()

        self._allowed_import_roots: frozenset[str] | None = None
        if config.validate_imports and config.allowed_imports:
            self._allowed_import_roots = config.allowed_imports

    def start(self, startup_timeout: float | None = None) -> None:
        """Start and warm the Lean REPL within one startup deadline."""
        timeout = self.config.startup_timeout if startup_timeout is None else min(
            self.config.startup_timeout,
            startup_timeout,
        )
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"REPL startup timed out after {timeout:g} seconds")
            return value

        env = _inherit_clean_env()
        env.update(self.config.env)

        self.process = subprocess.Popen(
            self.config.repl_command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._stderr_bytes = 0
        self._stderr_tail.clear()

        try:
            if self.config.warmup_imports:
                header = "\n".join(f"import {root}" for root in self.config.warmup_imports)
                logger.info("Loading imports at startup: %s", self.config.warmup_imports)
                resp = self._run(code=header, env_id=None, timeout=remaining())
                if "env" not in resp:
                    raise RuntimeError(f"Failed to preload imports: {resp}")

                errors = [m for m in resp.get("messages", []) if isinstance(m, dict) and m.get("severity") == "error"]
                if errors:
                    error_details = "\n".join(m.get("data", str(m)) for m in errors)
                    raise RuntimeError(f"Import preloading failed:\n{error_details}")

                self._base_env_id = resp["env"]

                smoke = self._run(
                    code="#check Nat",
                    env_id=self._base_env_id,
                    timeout=min(DEFAULT_SMOKE_TEST_TIMEOUT, remaining()),
                )
                smoke_errors = [
                    m for m in smoke.get("messages", []) if isinstance(m, dict) and m.get("severity") == "error"
                ]
                if smoke_errors:
                    error_details = "; ".join(m.get("data", str(m)) for m in smoke_errors)
                    raise RuntimeError(
                        f"REPL smoke test failed — LEAN_PATH may be misconfigured. Errors: {error_details}"
                    )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the Lean REPL process."""
        try:
            if not self.process or self.process.poll() is not None:
                return
            _kill_subprocesses(self.process)
        finally:
            self.process = None
            self._base_env_id = None
            self._stderr_bytes = 0
            self._stderr_tail.clear()

    def restart(self, timeout: float | None = None) -> None:
        """Restart the Lean REPL process within an optional total timeout."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        self.close()
        if deadline is None:
            self.start()
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"REPL restart timed out after {timeout:g} seconds")
        self.start(startup_timeout=remaining)

    def is_alive(self) -> bool:
        """Check if the REPL process is alive."""
        return self.process is not None and self.process.poll() is None

    def get_memory_usage(self) -> float:
        """Return memory usage in GB."""
        return _get_process_memory_gb(self.process)

    def run(self, code: str, env_id: int | None = None, timeout: float | None = None) -> dict[str, Any]:
        """Send code to the REPL within one deadline across recovery attempts."""
        timeout = self.request_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"REPL command timed out after {timeout:g} seconds")
            return value

        run_from_env = env_id is not None
        max_retries = 0 if run_from_env else self.max_retries

        header_line_count = 0
        if not run_from_env:
            imports, code, header_line_count = _split_imports_and_body(code)

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

        last_exception: Exception | None = None
        with self._process_lock:
            if run_from_env and not self.is_alive():
                self.close()
                raise ReplProcessRestarted(
                    "REPL process restarted before the request; environment state was lost"
                )

            process_before_memory_check = self.process
            try:
                if not self.is_alive():
                    self.restart(timeout=remaining())
                self._check_memory_and_maybe_restart(timeout=remaining())
            except (TimeoutError, RuntimeError) as error:
                self.close()
                if run_from_env:
                    raise ReplProcessRestarted(str(error)) from error
                return {"repl_error": str(error)}

            if run_from_env and self.process is not process_before_memory_check:
                raise ReplProcessRestarted(
                    "REPL process restarted before the request; environment state was lost"
                )

            for i in range(max_retries + 1):
                try:
                    dispatch_env_id = env_id if run_from_env else self._base_env_id
                    resp = self._run(
                        code=code,
                        env_id=dispatch_env_id,
                        timeout=remaining(),
                    )
                    _adjust_line_numbers(resp, header_line_count)
                    return resp
                except ReplStderrBacklog as e:
                    # _run() already retired the process, so nothing can inherit the
                    # undrained stderr; close() here is an idempotent assertion of
                    # that. The response is valid, so a plain request still receives
                    # it. An env-scoped request cannot transparently outlive the
                    # process that held its environment, so it is told loudly.
                    logger.error("%s", e)
                    self.close()
                    if run_from_env:
                        raise ReplProcessRestarted(str(e)) from e
                    # The command's diagnostics remain valid, but any environment
                    # identifier belongs to the process _run() just retired.
                    response = dict(e.response)
                    response.pop("env", None)
                    _adjust_line_numbers(response, header_line_count)
                    return response
                except ReplOutcomeUnknown as e:
                    # The request was fully written, so replay could execute it
                    # twice. Retire the process and report the unknown outcome
                    # without entering the ordinary retry path.
                    logger.error("%s", e)
                    self.close()
                    if run_from_env:
                        raise
                    return {"repl_error": str(e), "outcome_unknown": True}
                except ReplProcessExited as e:
                    last_exception = e
                    logger.error("REPL process exited: %s. Attempt %d/%d.", e, i + 1, max_retries + 1)
                    if run_from_env:
                        self.close()
                        raise ReplProcessRestarted(str(e)) from e
                except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                    last_exception = e
                    logger.error("Error running command: %s. Attempt %d/%d.", e, i + 1, max_retries + 1)
                    if run_from_env:
                        self.close()
                        raise ReplProcessRestarted(str(e)) from e

                if i >= max_retries:
                    self.close()
                    break

                backoff = min(2**i, 30) + random.uniform(0, 1)
                try:
                    if backoff >= remaining():
                        raise TimeoutError(
                            f"REPL command timed out after {timeout:g} seconds"
                        )
                    time.sleep(backoff)
                    self.restart(timeout=remaining())
                except (TimeoutError, RuntimeError) as error:
                    last_exception = error
                    self.close()
                    break
            logger.error("Exceeded maximum retries for Lean REPL command")
            return {"repl_error": str(last_exception)}

    def _check_memory_and_maybe_restart(self, timeout: float | None = None) -> None:
        """Proactively restart if memory usage is near the limit."""
        if self.mem_limit_gb <= 0 or self.config.mem_restart_ratio <= 0:
            return
        try:
            usage_gb = self.get_memory_usage()
            threshold_gb = self.mem_limit_gb * self.config.mem_restart_ratio
            if usage_gb >= threshold_gb:
                logger.info("REPL memory %.2fGB >= threshold %.2fGB, restarting...", usage_gb, threshold_gb)
                self.restart(timeout=timeout)
        except (TimeoutError, RuntimeError):
            raise
        except Exception:
            logger.warning("Memory check failed, continuing", exc_info=True)

    def _run(self, code: str, env_id: int | None, timeout: float) -> dict[str, Any]:
        """Send code to the REPL via stdin JSON-RPC, read response via non-blocking I/O."""
        cmd_obj: dict[str, Any] = {"cmd": code}
        if env_id is not None:
            cmd_obj["env"] = env_id
        command = json.dumps(cmd_obj) + "\n\n"

        if (
            self.process is None
            or self.process.poll() is not None
            or self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            raise ReplProcessExited("REPL process is not running.")

        end_time = time.monotonic() + timeout
        stdin_fd = self.process.stdin.fileno()
        stdout_fd = self.process.stdout.fileno()
        stderr_fd = self.process.stderr.fileno()
        os.set_blocking(stdin_fd, False)
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        response_buffer = bytearray()
        max_buffer = self.config.max_buffer_bytes
        stderr_drained = True
        stderr_open = True
        stderr_poison_reason: str | None = None

        def stderr_details() -> tuple[int, str]:
            stderr_bytes = self._stderr_bytes
            stderr_tail = bytes(self._stderr_tail[-200:]).decode("utf-8", errors="replace")
            return stderr_bytes, stderr_tail

        def raise_unknown_stderr_outcome() -> None:
            stderr_bytes, stderr_tail = stderr_details()
            reason = stderr_poison_reason or "stderr could not be drained"
            self.close()
            raise ReplOutcomeUnknown(
                f"REPL process-generation stderr became unsafe after the request "
                f"was sent ({reason}; {stderr_bytes} bytes observed); "
                f"the execution outcome is unknown and was not retried. Tail: {stderr_tail!r}"
            )

        def retire_before_request() -> None:
            stderr_bytes, stderr_tail = stderr_details()
            reason = stderr_poison_reason or "stderr became unsafe"
            self.close()
            raise ReplProcessExited(
                f"REPL process-generation stderr became unsafe before the request "
                f"frame was fully sent ({reason}; {stderr_bytes} bytes observed); "
                f"the process was recycled. Tail: {stderr_tail!r}"
            )

        def drain_stderr(*, max_reads: int | None = None, after_response: bool = False) -> bool:
            """Drain process stderr fairly while retaining a bounded tail.

            ``max_reads`` bounds a single fairness cycle so a process that writes
            diagnostics continuously cannot starve stdout.

            ``after_response`` marks the drain that runs once the response frame is
            complete. It is unbounded in reads because no stdout read is left to
            starve, but the deadline and process-generation stderr ceiling stop it
            without destroying a response already captured.

            Returns whether stderr is currently empty and the process generation
            remains within budget. EAGAIN is never treated as a command boundary;
            the byte count and tail persist until the process is replaced.
            """
            nonlocal stderr_open, stderr_poison_reason

            if not stderr_open:
                return False

            reads = 0
            while max_reads is None or reads < max_reads:
                if after_response:
                    if stderr_poison_reason is not None:
                        return False
                    if time.monotonic() >= end_time:
                        readable, _, _ = select.select([stderr_fd], [], [], 0)
                        if not readable:
                            return True
                        stderr_poison_reason = "stderr remained readable at the command deadline"
                        return False
                if max_reads is None and not after_response and time.monotonic() >= end_time:
                    if stderr_poison_reason is not None:
                        raise_unknown_stderr_outcome()
                    raise TimeoutError(f"REPL command timed out after {timeout} seconds while reading stderr")
                try:
                    chunk = os.read(stderr_fd, self.chunk_size)
                except BlockingIOError:
                    return stderr_poison_reason is None
                except OSError as error:
                    stderr_open = False
                    stderr_poison_reason = f"stderr read failed: {error}"
                    return False
                if not chunk:
                    stderr_open = False
                    stderr_poison_reason = "stderr closed unexpectedly"
                    return False
                self._stderr_bytes += len(chunk)
                tail_limit = max(0, max_buffer)
                if tail_limit:
                    if len(chunk) >= tail_limit:
                        self._stderr_tail[:] = chunk[-tail_limit:]
                    else:
                        overflow = len(self._stderr_tail) + len(chunk) - tail_limit
                        if overflow > 0:
                            del self._stderr_tail[:overflow]
                        self._stderr_tail.extend(chunk)
                reads += 1
                logger.debug(
                    "Lean REPL stderr: %s",
                    chunk.decode("utf-8", errors="replace").rstrip(),
                )
                if self._stderr_bytes > max_buffer and stderr_poison_reason is None:
                    stderr_poison_reason = (
                        f"stderr exceeded the {max_buffer}-byte process-generation ceiling"
                    )
                if after_response and stderr_poison_reason is not None:
                    return False
            return stderr_poison_reason is None

        # stdout and stderr are independent pipes. A child blocked on a full
        # stderr pipe may be unable to read its stdin, so service stderr fairly
        # while writing instead of waiting on stdin alone. Any stderr observed
        # here remains process-scoped; it is never assigned to this command.
        payload = memoryview(command.encode("utf-8"))
        offset = 0
        while offset < len(payload):
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                if stderr_poison_reason is not None:
                    retire_before_request()
                raise TimeoutError(
                    f"REPL command timed out after {timeout} seconds while writing"
                )
            readable, writable, _ = select.select(
                [stderr_fd] if stderr_open else [],
                [stdin_fd],
                [],
                remaining,
            )
            if not readable and not writable:
                if stderr_poison_reason is not None:
                    retire_before_request()
                raise TimeoutError(
                    f"REPL command timed out after {timeout} seconds while writing"
                )
            if stderr_fd in readable:
                drain_stderr(max_reads=1)
                if stderr_poison_reason is not None:
                    retire_before_request()
            if stdin_fd not in writable:
                continue
            try:
                written = os.write(stdin_fd, payload[offset:])
            except BlockingIOError:
                continue
            except OSError as error:
                raise ReplProcessExited(
                    f"REPL process closed stdin while writing: {error}"
                ) from error
            if written <= 0:
                raise ReplProcessExited("REPL process closed stdin while writing")
            offset += written

        while True:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                if stderr_poison_reason is not None:
                    raise_unknown_stderr_outcome()
                raise TimeoutError(f"REPL command timed out after {timeout} seconds")

            readable_fds = [stdout_fd]
            if stderr_open:
                readable_fds.append(stderr_fd)
            ready, _, _ = select.select(readable_fds, [], [], remaining)
            if not ready:
                if stderr_poison_reason is not None:
                    raise_unknown_stderr_outcome()
                raise TimeoutError(f"REPL command timed out after {timeout} seconds")

            # Drain diagnostics before handling stdout EOF so a crashing Lean
            # process cannot lose stderr that became readable at the same time.
            if stderr_fd in ready:
                drain_stderr(max_reads=1)

            if stdout_fd in ready:
                try:
                    chunk = os.read(stdout_fd, self.chunk_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    if stderr_open:
                        drain_stderr()
                    if stderr_poison_reason is not None:
                        raise_unknown_stderr_outcome()
                    stderr_text = self._stderr_tail.decode("utf-8", errors="replace")
                    raise ReplProcessExited(f"REPL process exited. stderr: {stderr_text}")
                response_buffer.extend(chunk)

                if len(response_buffer) > max_buffer:
                    if stderr_poison_reason is not None:
                        raise_unknown_stderr_outcome()
                    tail = bytes(response_buffer[-200:]).decode(
                        "utf-8",
                        errors="replace",
                    )
                    raise RuntimeError(
                        f"REPL response exceeded {max_buffer} bytes. Tail: {tail!r}"
                    )

                separator = response_buffer.find(b"\n\n")
                if separator >= 0:
                    response_bytes = bytes(response_buffer[:separator]).strip()
                    # The frame is complete, so this command's remaining queued
                    # stderr can be drained without starving stdout. Leaving it in
                    # the pipe would let a command exceed the stderr ceiling
                    # unnoticed, misattribute diagnostics to the next command, and
                    # eventually block the child on a full stderr pipe.
                    stderr_drained = drain_stderr(after_response=True)
                    break

        if not stderr_drained:
            stderr_bytes = self._stderr_bytes
            stderr_tail = bytes(self._stderr_tail[-200:]).decode("utf-8", errors="replace")
            stderr_reason = stderr_poison_reason or "stderr could not be drained"
            # stderr is accounted to the process generation, never to whichever
            # command happened to observe it. Once that generation exceeds its
            # quota or cannot be drained, retire it before another request.
            self.close()

        # Retire a desynchronized process before parsing. Malformed JSON must not
        # bypass the stream-safety invariant and leave stale stderr reusable.
        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except json.JSONDecodeError as error:
            if not stderr_drained:
                raise ReplOutcomeUnknown(
                    f"REPL process-generation stderr became unsafe after the request "
                    f"was sent ({stderr_reason}; {stderr_bytes} bytes observed), and "
                    "the response frame was malformed; the execution outcome is "
                    f"unknown and was not retried. Tail: {stderr_tail!r}"
                ) from error
            raise
        if not stderr_drained:
            raise ReplStderrBacklog(
                f"REPL process-generation stderr became unsafe ({stderr_reason}; "
                f"{stderr_bytes} bytes observed); "
                f"the process was recycled. Tail: {stderr_tail!r}",
                response,
            )
        return response
