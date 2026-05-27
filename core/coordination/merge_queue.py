# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Merge queue — bors-style batch merge train.

Collects merge requests from concurrent agents, batches them, builds once,
and merges the batch to main. Agents submit and block until their merge is
resolved (merged or rejected).

Batch processing flow:
1. Collect up to ``batch_size`` requests (or wait ``batch_timeout`` seconds).
2. Rebase each agent's worktree onto current main — reject on conflict.
3. Create a staging worktree and cherry-pick each agent's commits sequentially.
   Inter-agent conflicts get deferred to the next batch.
4. Run the build function on the staging worktree.
5. On success: fast-forward main to staging, resolve all futures as MERGED.
6. On failure: bisect to identify the culprit, merge non-culprits, reject culprit.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import subprocess
import time
from collections.abc import Callable, Awaitable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from core import worktree
from core.trace.step_trace import step_trace_context, traced
from core.worktree import git_run

logger = logging.getLogger(__name__)

_MERGE_PORT_OFFSET = 100  # merge ZMQ port = task ZMQ port + offset

_LOCAL_TIMEOUT = 300
_GIT_ENV = {**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"}


class MergeStatus(StrEnum):
    MERGED = "merged"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MergeResult:
    status: MergeStatus
    pre_hash: str | None = None
    post_hash: str | None = None
    error: str | None = None


@dataclass
class _MergeRequest:
    agent_id: str
    worktree_path: Path
    future: asyncio.Future[MergeResult]


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _rev_parse(path: Path, rev: str = "HEAD") -> str | None:
    result = git_run(["git", "rev-parse", rev], cwd=path, timeout=30)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _cherry_pick_range(
    staging_path: Path,
    source_worktree: Path,
    base_ref: str,
) -> tuple[bool, str]:
    """Cherry-pick commits ``base_ref..HEAD`` from *source_worktree* onto staging.

    Returns (success, error_message).
    """
    fetch = git_run(
        ["git", "fetch", str(source_worktree), "HEAD"],
        cwd=staging_path,
        env=_GIT_ENV,
    )
    if fetch.returncode != 0:
        return False, f"fetch failed: {fetch.stderr}"

    log = git_run(
        ["git", "log", "--reverse", "--format=%H", f"{base_ref}..FETCH_HEAD"],
        cwd=staging_path,
    )
    commits = log.stdout.strip().splitlines()
    if not commits:
        return True, ""  # nothing to pick

    result = git_run(
        ["git", "cherry-pick", *commits],
        cwd=staging_path,
        env=_GIT_ENV,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "cherry-pick", "--abort"],
            cwd=staging_path,
            capture_output=True,
            timeout=_LOCAL_TIMEOUT,
        )
        return False, result.stderr + result.stdout

    return True, ""


# ---------------------------------------------------------------------------
# MergeQueue
# ---------------------------------------------------------------------------


class MergeQueue:
    """Bors-style merge train.

    Usage::

        queue = MergeQueue(repo_root, build_fn)
        asyncio.create_task(queue.run())

        # From each agent (blocks until resolved):
        result = await queue.submit("agent-0", Path("/path/to/worktree"))
    """

    def __init__(
        self,
        repo_root: Path,
        build_fn: Callable[[Path], Awaitable[tuple[bool, str]]],
        *,
        batch_size: int = 10,
        batch_timeout: float = 120.0,
        on_batch_merged: Callable[[str, str, list[str]], None] | None = None,
        trace_store: Any | None = None,
        on_step: Callable[[str, str, bool, float, str | None], None] | None = None,
    ):
        self._repo_root = repo_root
        self._build_fn = build_fn
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._on_batch_merged = on_batch_merged
        self._trace_store = trace_store
        self._on_step = on_step
        self._queue: asyncio.Queue[_MergeRequest] = asyncio.Queue()
        self._stopped = False

    async def submit(self, agent_id: str, worktree_path: Path) -> MergeResult:
        """Submit a merge request. Blocks until merged or rejected."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MergeResult] = loop.create_future()
        await self._queue.put(_MergeRequest(agent_id, worktree_path, future))
        return await future

    def stop(self) -> None:
        """Signal the run loop to exit after the current batch."""
        self._stopped = True

    async def run(self) -> None:
        """Main loop — collect and process batches until stopped."""
        while not self._stopped:
            batch = await self._collect_batch()
            if batch:
                try:
                    await self._process_batch(batch)
                except Exception:
                    logger.exception("Merge queue batch failed")
                    for req in batch:
                        if not req.future.done():
                            req.future.set_result(
                                MergeResult(
                                    status=MergeStatus.REJECTED,
                                    error="Internal merge queue error",
                                )
                            )

    # -----------------------------------------------------------------------
    # Batch collection
    # -----------------------------------------------------------------------

    async def _collect_batch(self) -> list[_MergeRequest]:
        """Wait for the first request, then drain up to batch_size more."""
        try:
            first = await asyncio.wait_for(
                self._queue.get(),
                timeout=5.0,  # poll interval for stop check
            )
        except asyncio.TimeoutError:
            return []

        batch = [first]
        deadline = asyncio.get_event_loop().time() + self._batch_timeout
        while len(batch) < self._batch_size:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                batch.append(item)
            except asyncio.TimeoutError:
                break

        logger.info("Merge queue: collected batch of %d", len(batch))
        return batch

    # -----------------------------------------------------------------------
    # Batch processing
    # -----------------------------------------------------------------------

    async def _process_batch(self, batch: list[_MergeRequest]) -> None:
        main_ref = _rev_parse(self._repo_root, "main") or _rev_parse(self._repo_root, "HEAD")
        if not main_ref:
            for req in batch:
                req.future.set_result(
                    MergeResult(
                        status=MergeStatus.REJECTED,
                        error="Cannot resolve main HEAD",
                    )
                )
            return

        # Don't pass trace_store yet — we save manually once we know post_hash.
        ctx = step_trace_context(trace_id=f"merge_batches/_pending-{int(time.time())}")
        with ctx:
            try:
                # Phase 1: Rebase each onto current main. Reject real conflicts.
                rebased = await self._rebase_all_onto_main(batch, main_ref)
                if not rebased:
                    return

                # Phase 2: Stage onto a staging worktree via cherry-pick.
                staging_path = await self._create_staging(main_ref)
                try:
                    staged = await self._stage_all(rebased, staging_path, main_ref)
                    if not staged:
                        return

                    # Phase 3: Build.
                    pre_hash = main_ref
                    build_ok, build_error = await self._build(staging_path)

                    merged_ids: list[str] = []
                    if build_ok:
                        await self._land_batch(staged, staging_path, pre_hash, merged_ids)
                    else:
                        await self._bisect_and_land(staged, staging_path, main_ref, build_error, merged_ids)

                    # Fire callback once for the entire batch.
                    if merged_ids and self._on_batch_merged:
                        post_hash = _rev_parse(self._repo_root, "HEAD")
                        try:
                            self._on_batch_merged(pre_hash, post_hash, merged_ids)
                        except Exception:
                            logger.exception("on_batch_merged callback failed")
                finally:
                    await asyncio.to_thread(worktree.cleanup_worktree, staging_path, self._repo_root)
            finally:
                # Always save trace — including early returns from rebase/stage
                # rejection.  Determine folder key from post_hash if something
                # landed, otherwise use a rejected-{pre_hash} fallback.
                post_hash = _rev_parse(self._repo_root, "HEAD")
                if post_hash and post_hash != main_ref:
                    ctx.trace_id = f"merge_batches/{post_hash[:8]}/steps"
                else:
                    ctx.trace_id = f"merge_batches/rejected-{main_ref[:8]}-{int(time.time())}/steps"
                if self._trace_store:
                    self._trace_store.save(ctx)

    @traced
    async def _rebase_all_onto_main(
        self,
        batch: list[_MergeRequest],
        main_ref: str,
    ) -> list[_MergeRequest]:
        """Rebase each agent onto main. Reject those with conflicts."""
        rebased = []
        for req in batch:
            start = time.perf_counter()
            ok, err = await asyncio.to_thread(
                worktree.rebase_onto_main,
                req.worktree_path,
                self._repo_root,
            )
            duration = (time.perf_counter() - start) * 1000
            if self._on_step:
                self._on_step("rebase", req.agent_id, ok, duration, err)
            if ok:
                rebased.append(req)
            else:
                logger.info("Merge queue: rejecting %s (rebase conflict with main)", req.agent_id)
                req.future.set_result(
                    MergeResult(
                        status=MergeStatus.REJECTED,
                        error=f"Rebase onto main failed:\n{err}",
                    )
                )
        return rebased

    async def _create_staging(self, main_ref: str) -> Path:
        """Create a staging worktree at the given ref."""
        lock_path = self._repo_root / ".worktree_lock"
        staging_name = f"merge-staging-{main_ref[:12]}"

        def _create() -> Path:
            with open(lock_path, "w") as lf:
                fcntl.lockf(lf, fcntl.LOCK_EX)
                subprocess.run(
                    ["git", "-C", str(self._repo_root), "worktree", "prune"],
                    capture_output=True,
                )
                return worktree.create_worktree(self._repo_root, staging_name)

        return await asyncio.to_thread(_create)

    @traced
    async def _stage_all(
        self,
        rebased: list[_MergeRequest],
        staging_path: Path,
        main_ref: str,
    ) -> list[_MergeRequest]:
        """Cherry-pick each agent's commits onto staging. Defer conflicts."""
        staged = []
        for req in rebased:
            start = time.perf_counter()
            ok, err = await asyncio.to_thread(
                _cherry_pick_range,
                staging_path,
                req.worktree_path,
                main_ref,
            )
            duration = (time.perf_counter() - start) * 1000
            if ok:
                staged.append(req)
                logger.info("Merge queue: staged %s", req.agent_id)
                if self._on_step:
                    self._on_step("stage", req.agent_id, True, duration, None)
            else:
                # Conflict between agents — defer to next batch.
                logger.info("Merge queue: deferring %s (inter-agent conflict)", req.agent_id)
                if self._on_step:
                    self._on_step("stage_deferred", req.agent_id, False, duration, err)
                await self._queue.put(req)
        return staged

    # -----------------------------------------------------------------------
    # Landing
    # -----------------------------------------------------------------------

    @traced
    async def _build(self, staging_path: Path) -> tuple[bool, str]:
        """Run the build function on the staging worktree."""
        return await self._build_fn(staging_path)

    @traced
    async def _land_batch(
        self,
        staged: list[_MergeRequest],
        staging_path: Path,
        pre_hash: str,
        merged_ids: list[str] | None = None,
    ) -> None:
        """Fast-forward main to staging HEAD and resolve all futures."""
        ok, err = await asyncio.to_thread(
            worktree.merge_to_main,
            staging_path,
            self._repo_root,
        )
        post_hash = _rev_parse(self._repo_root, "HEAD")

        if ok:
            ids = [r.agent_id for r in staged]
            logger.info("Merge queue: landed batch of %d (%s)", len(staged), ", ".join(ids))
            for req in staged:
                req.future.set_result(
                    MergeResult(
                        status=MergeStatus.MERGED,
                        pre_hash=pre_hash,
                        post_hash=post_hash,
                    )
                )
            if merged_ids is not None:
                merged_ids.extend(ids)
        else:
            logger.error("Merge queue: merge to main failed after successful build: %s", err)
            for req in staged:
                req.future.set_result(
                    MergeResult(
                        status=MergeStatus.REJECTED,
                        error=f"Merge to main failed: {err}",
                    )
                )

    # -----------------------------------------------------------------------
    # Bisection
    # -----------------------------------------------------------------------

    @traced
    async def _bisect_and_land(
        self,
        staged: list[_MergeRequest],
        staging_path: Path,
        main_ref: str,
        build_error: str,
        merged_ids: list[str] | None = None,
    ) -> None:
        """Find the culprit via binary search, land the rest, reject culprit."""
        if len(staged) == 1:
            # Single agent — they're the culprit.
            staged[0].future.set_result(
                MergeResult(
                    status=MergeStatus.REJECTED,
                    error=f"Build failed:\n{build_error}",
                )
            )
            return

        mid = len(staged) // 2
        left, right = staged[:mid], staged[mid:]

        # Try building just the left half.
        left_ok = await self._rebuild_staging_and_test(staging_path, left, main_ref)

        if left_ok:
            # Left is clean — culprit is in right half.
            # Land left immediately.
            await self._land_batch(left, staging_path, main_ref, merged_ids)
            # Update main_ref for the right half.
            new_main = _rev_parse(self._repo_root, "HEAD") or main_ref
            # Recurse on right.
            await self._bisect_subset(right, new_main, build_error, merged_ids)
        else:
            # Culprit is in left half. Defer right to next batch.
            for req in right:
                await self._queue.put(req)
            # Recurse on left.
            await self._bisect_subset(left, main_ref, build_error, merged_ids)

    @traced
    async def _bisect_subset(
        self,
        subset: list[_MergeRequest],
        main_ref: str,
        build_error: str,
        merged_ids: list[str] | None = None,
    ) -> None:
        """Recursively bisect a subset to isolate the culprit."""
        if len(subset) == 1:
            subset[0].future.set_result(
                MergeResult(
                    status=MergeStatus.REJECTED,
                    error=f"Build failed (identified by bisect):\n{build_error}",
                )
            )
            return

        staging_path = await self._create_staging(main_ref)
        try:
            mid = len(subset) // 2
            left, right = subset[:mid], subset[mid:]
            left_ok = await self._rebuild_staging_and_test(staging_path, left, main_ref)

            if left_ok:
                await self._land_batch(left, staging_path, main_ref, merged_ids)
                new_main = _rev_parse(self._repo_root, "HEAD") or main_ref
                await self._bisect_subset(right, new_main, build_error, merged_ids)
            else:
                for req in right:
                    await self._queue.put(req)
                await self._bisect_subset(left, main_ref, build_error, merged_ids)
        finally:
            await asyncio.to_thread(worktree.cleanup_worktree, staging_path, self._repo_root)

    @traced
    async def _rebuild_staging_and_test(
        self,
        staging_path: Path,
        subset: list[_MergeRequest],
        main_ref: str,
    ) -> bool:
        """Reset staging to main_ref, cherry-pick subset, build. Returns True if build passes."""
        # Reset staging to main
        await asyncio.to_thread(
            subprocess.run,
            ["git", "reset", "--hard", main_ref],
            cwd=staging_path,
            capture_output=True,
            timeout=_LOCAL_TIMEOUT,
        )

        # Cherry-pick each agent in order
        for req in subset:
            ok, err = await asyncio.to_thread(
                _cherry_pick_range,
                staging_path,
                req.worktree_path,
                main_ref,
            )
            if not ok:
                # This agent can't even cherry-pick cleanly — they're the problem.
                req.future.set_result(
                    MergeResult(
                        status=MergeStatus.REJECTED,
                        error=f"Cherry-pick failed during bisect:\n{err}",
                    )
                )
                subset.remove(req)
                return False

        build_ok, _ = await self._build(staging_path)
        return build_ok


# ---------------------------------------------------------------------------
# ZMQ bridge — coordinator side
# ---------------------------------------------------------------------------


class MergeQueueServer:
    """ZMQ front-end for a MergeQueue running on the coordinator.

    Workers send ``merge_request`` messages; this server submits them to the
    local MergeQueue and sends ``merge_response`` back when resolved.

    Usage::

        server = MergeQueueServer(queue, port=29600)
        asyncio.create_task(server.run())
        # ... on shutdown:
        server.stop()
    """

    def __init__(self, queue: MergeQueue, *, port: int) -> None:
        from core.coordination.multinode.zmq_queue import ZmqTaskServer

        self._queue = queue
        self._server = ZmqTaskServer(port=port)
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Listen for merge requests and dispatch to the queue."""
        in_flight: set[asyncio.Task] = set()
        try:
            while not self._stopped:
                result = await asyncio.to_thread(self._server.recv, 200)
                if result is None:
                    continue
                rank, msg = result
                if msg.get("type") == "merge_request":
                    task = asyncio.create_task(self._handle(rank, msg))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
        finally:
            for t in in_flight:
                t.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            self._server.close()

    async def _handle(self, rank: int, msg: dict) -> None:
        request_id = msg["request_id"]
        try:
            result = await self._queue.submit(
                msg["agent_id"],
                Path(msg["worktree_path"]),
            )
            self._server.send(
                rank,
                {
                    "type": "merge_response",
                    "request_id": request_id,
                    "status": result.status.value,
                    "pre_hash": result.pre_hash,
                    "post_hash": result.post_hash,
                    "error": result.error,
                },
            )
        except Exception:
            logger.exception("Failed to handle merge request %s", request_id)
            self._server.send(
                rank,
                {
                    "type": "merge_response",
                    "request_id": request_id,
                    "status": MergeStatus.REJECTED.value,
                    "error": "Internal merge queue error",
                },
            )


# ---------------------------------------------------------------------------
# ZMQ bridge — worker side
# ---------------------------------------------------------------------------


class MergeQueueClient:
    """Worker-side client for submitting merge requests to the coordinator.

    Each worker node creates one client. Multiple agents on the same node
    can call ``submit()`` concurrently — responses are routed by request ID.

    Usage::

        client = MergeQueueClient(host="coordinator", port=29600, rank=1)
        asyncio.create_task(client.run())

        # From any agent coroutine on this node:
        result = await client.submit("agent-0", Path("/path/to/worktree"))
    """

    def __init__(self, host: str, port: int, rank: int) -> None:
        from core.coordination.multinode.zmq_queue import ZmqTaskClient

        self._client = ZmqTaskClient(host=host, port=port, rank=rank)
        self._pending: dict[str, asyncio.Future[MergeResult]] = {}
        self._stopped = False
        self._counter = 0

    def stop(self) -> None:
        self._stopped = True

    async def submit(self, agent_id: str, worktree_path: Path) -> MergeResult:
        """Send a merge request to the coordinator and wait for the response."""
        self._counter += 1
        request_id = f"{agent_id}-{self._counter}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MergeResult] = loop.create_future()
        self._pending[request_id] = future

        await asyncio.to_thread(
            self._client.send,
            {
                "type": "merge_request",
                "rank": self._client._rank,
                "request_id": request_id,
                "agent_id": agent_id,
                "worktree_path": str(worktree_path),
            },
        )

        return await future

    async def run(self) -> None:
        """Background listener — routes responses to waiting futures."""
        try:
            while not self._stopped:
                msg = await asyncio.to_thread(self._client.recv, 200)
                if msg is None:
                    continue
                if msg.get("type") == "merge_response":
                    request_id = msg["request_id"]
                    future = self._pending.pop(request_id, None)
                    if future and not future.done():
                        future.set_result(
                            MergeResult(
                                status=MergeStatus(msg["status"]),
                                pre_hash=msg.get("pre_hash"),
                                post_hash=msg.get("post_hash"),
                                error=msg.get("error"),
                            )
                        )
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            self._client.close()
