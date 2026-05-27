# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Step tracing — lightweight decorator-based tracing for hardcoded pipeline logic.

Records function calls (build, merge, review, etc.) to an ambient step log
via contextvars. No manual logging inside function bodies — just add @traced.

Usage:
    from core.trace.step_trace import traced, step_trace_context

    # At the pipeline entry point — set once:
    with step_trace_context(trace_store=store) as step_ctx:
        await run_pipeline(...)
    # steps are saved incrementally after each @traced call

    # On any function you want to monitor:
    @traced
    async def build(agent, task):
        ...  # existing code, unchanged
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Any

_current_step_ctx: contextvars.ContextVar[step_trace_context | None] = contextvars.ContextVar("step_ctx", default=None)


@dataclass
class StepRecord:
    """A single recorded function call."""

    function: str
    timestamp: float
    duration_ms: float
    success: bool
    error: str | None = None
    args_summary: dict[str, Any] = field(default_factory=dict)
    result_summary: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _summarize_arg(value: Any) -> Any:
    """Produce a JSON-safe summary of a function argument."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "agent_id"):
        return f"<{type(value).__name__} {value.agent_id}>"
    if hasattr(value, "id"):
        return f"<{type(value).__name__} {value.id}>"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    return f"<{type(value).__name__}>"


def _summarize_result(value: Any) -> Any:
    """Produce a JSON-safe summary of a return value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return tuple(_summarize_result(v) for v in value)
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    return f"<{type(value).__name__}>"


def _infer_success(result: Any) -> bool:
    """Infer success from a return value.

    Handles bare bools (e.g. rebuild_staging_and_test returns bool)
    and tuples starting with a bool (e.g. build returns (bool, str)).
    Otherwise assumes success since the function didn't raise.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, tuple) and len(result) > 0 and isinstance(result[0], bool):
        return result[0]
    return True


def _build_args_summary(fn, args, kwargs) -> dict[str, Any]:
    """Build a JSON-safe summary of function arguments."""
    import inspect

    arg_names = list(inspect.signature(fn).parameters.keys())
    summary = {}
    for i, val in enumerate(args):
        name = arg_names[i] if i < len(arg_names) else f"arg{i}"
        if name == "self":
            continue
        summary[name] = _summarize_arg(val)
    for k, v in kwargs.items():
        summary[k] = _summarize_arg(v)
    return summary


def traced(fn):
    """Decorator that records function calls to the current step log.

    Works with both sync and async functions. If no step log is active
    (no step_trace_context), the function runs normally with zero overhead.
    """
    if asyncio.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            ctx = _current_step_ctx.get(None)
            if ctx is None:
                return await fn(*args, **kwargs)

            args_summary = _build_args_summary(fn, args, kwargs)
            start = time.perf_counter()
            ts = time.time()
            try:
                result = await fn(*args, **kwargs)
                success = _infer_success(result)
                ctx._record(
                    StepRecord(
                        function=fn.__qualname__,
                        timestamp=ts,
                        duration_ms=(time.perf_counter() - start) * 1000,
                        success=success,
                        args_summary=args_summary,
                        result_summary=_summarize_result(result),
                    )
                )
                return result
            except Exception as e:
                ctx._record(
                    StepRecord(
                        function=fn.__qualname__,
                        timestamp=ts,
                        duration_ms=(time.perf_counter() - start) * 1000,
                        success=False,
                        error=str(e),
                        args_summary=args_summary,
                    )
                )
                raise

        return async_wrapper
    else:

        @wraps(fn)
        def sync_wrapper(*args, **kwargs):
            ctx = _current_step_ctx.get(None)
            if ctx is None:
                return fn(*args, **kwargs)

            args_summary = _build_args_summary(fn, args, kwargs)
            start = time.perf_counter()
            ts = time.time()
            try:
                result = fn(*args, **kwargs)
                success = _infer_success(result)
                ctx._record(
                    StepRecord(
                        function=fn.__qualname__,
                        timestamp=ts,
                        duration_ms=(time.perf_counter() - start) * 1000,
                        success=success,
                        args_summary=args_summary,
                        result_summary=_summarize_result(result),
                    )
                )
                return result
            except Exception as e:
                ctx._record(
                    StepRecord(
                        function=fn.__qualname__,
                        timestamp=ts,
                        duration_ms=(time.perf_counter() - start) * 1000,
                        success=False,
                        error=str(e),
                        args_summary=args_summary,
                    )
                )
                raise

        return sync_wrapper


class step_trace_context:
    """Context manager that activates step tracing for the current scope.

    Conforms to the TraceStore.save() protocol (trace_id + to_dict()).
    If trace_store is provided, saves incrementally after each @traced call.

    Usage:
        with step_trace_context(trace_id="t1/attempt_1/steps", trace_store=store) as ctx:
            await run_pipeline(...)
        ctx.winner_id = "agent-0"
        ctx.final_status = "success"
        # steps are saved incrementally; final save happens automatically
    """

    def __init__(
        self,
        trace_id: str = "steps",
        trace_store: Any | None = None,
    ) -> None:
        self.trace_id = trace_id
        self.steps: list[StepRecord] = []
        self.winner_id: str | None = None
        self.final_status: str = "running"
        self._trace_store = trace_store
        self._token: contextvars.Token | None = None

    def _record(self, step: StepRecord) -> None:
        """Append a step and flush to store if available."""
        self.steps.append(step)
        if self._trace_store:
            self._trace_store.save(self)

    def __enter__(self) -> step_trace_context:
        self._token = _current_step_ctx.set(self)
        return self

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            _current_step_ctx.reset(self._token)
        # Final save
        if self._trace_store:
            self._trace_store.save(self)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "winner_id": self.winner_id,
            "final_status": self.final_status,
            "steps": [s.to_dict() for s in self.steps],
        }


def get_current_step_log() -> list[StepRecord] | None:
    """Get the current step log, or None if not in a traced context."""
    ctx = _current_step_ctx.get(None)
    return ctx.steps if ctx else None
