"""Signal polling — async loop that checks for messages and dispatches to a handler."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Awaitable, Callable

from .core import SignalClient

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 5
DEFAULT_TIMEOUT_S = 120


async def poll(
    client: SignalClient,
    on_message: Callable[[str], Awaitable[None]],
    *,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float | None = None,
    reset_on_message: bool = True,
) -> None:
    """Poll Signal for messages and dispatch each batch to on_message.

    Args:
        client: SignalClient to poll.
        on_message: Async callback receiving the raw message text.
        interval_s: Seconds between polls.
        timeout_s: Stop after this many seconds of silence. None = run forever
                   (cancel the task to stop).
        reset_on_message: If True, reset the timeout deadline whenever a
                          message arrives (active engagement pattern).
    """
    # Drain stale messages before starting
    try:
        await client.receive_messages()
    except Exception:
        pass

    deadline = _time.time() + timeout_s if timeout_s else None

    if timeout_s:
        logger.info("Polling Signal (timeout %ds)...", timeout_s)
    else:
        logger.info("Polling Signal (indefinite)...")

    while True:
        if deadline and _time.time() >= deadline:
            logger.info("Signal poll timed out — proceeding.")
            return

        await asyncio.sleep(interval_s)

        try:
            raw = await client.receive_messages()
        except Exception as e:
            logger.debug("Signal poll error: %s", e)
            continue

        if not raw or raw == "No new messages.":
            continue

        logger.info("Signal message received: %s", raw[:200])
        await on_message(raw)

        if deadline and reset_on_message:
            deadline = _time.time() + timeout_s
