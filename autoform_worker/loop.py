"""``autoform work --loop`` — rounds forever, with honest backoff.

Each round runs in a *subprocess* in its own session, hard-killed at the round
timeout, so a wedged agent or build can never wedge the loop (TauCeti's
``_round`` discipline). Exit codes: 0 progress, 75 no-progress (idle poll),
anything else errors into exponential backoff.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from .constants import BACKOFF_BASE_S, BACKOFF_MAX_S, IDLE_POLL_S, INTERROUND_S, ROUND_TIMEOUT_S
from .errors import EX_NOPROGRESS, EX_TERM


class _LoopTerminated(Exception):
    pass


def _round_argv(passthrough: list[str]) -> list[str]:
    return [sys.executable, "-m", "autoform_worker", "_round", *passthrough]


_CURRENT_ROUND: subprocess.Popen | None = None


def _kill_round(proc: subprocess.Popen) -> None:
    """SIGTERM the round's process group (giving its cleanup handlers a chance
    to restore the worktree), escalate to SIGKILL after a grace period."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=30)
    except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_round_subprocess(passthrough: list[str], timeout: int = ROUND_TIMEOUT_S) -> int:
    global _CURRENT_ROUND
    proc = subprocess.Popen(_round_argv(passthrough), start_new_session=True)
    _CURRENT_ROUND = proc
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_round(proc)
        return 124
    finally:
        _CURRENT_ROUND = None


def cmd_loop(passthrough: list[str]) -> int:
    def _terminate(signum, frame):
        raise _LoopTerminated()

    signal.signal(signal.SIGTERM, _terminate)
    streak = 0
    try:
        while True:
            rc = run_round_subprocess(passthrough)
            if rc == 0:
                streak = 0
                print(f"[loop] round progressed; next in {INTERROUND_S}s", flush=True)
                time.sleep(INTERROUND_S)
            elif rc == EX_NOPROGRESS:
                streak = 0
                print(f"[loop] nothing actionable; polling again in {IDLE_POLL_S}s", flush=True)
                time.sleep(IDLE_POLL_S)
            else:
                streak += 1
                delay = min(BACKOFF_BASE_S * (2 ** min(streak, 5)), BACKOFF_MAX_S)
                label = "timed out" if rc == 124 else f"failed rc={rc}"
                print(f"[loop] round {label}; backoff {delay}s (streak {streak})", flush=True)
                time.sleep(delay)
    except _LoopTerminated:
        if _CURRENT_ROUND is not None:
            _kill_round(_CURRENT_ROUND)  # never leave a detached round pushing after "stop"
        return EX_TERM
    except KeyboardInterrupt:
        if _CURRENT_ROUND is not None:
            _kill_round(_CURRENT_ROUND)
        return 130
