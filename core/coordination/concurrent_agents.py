# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Concurrent agents coordination — race N agents on a task with build/review/merge.

Reusable composition built on core primitives.
Uses core/worktree.py for isolation.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO

from core.agent import Agent
from core.task import Task
from core.trace import AgentTrace, TraceStore, traced
from core.trace.step_trace import step_trace_context
from core import worktree

logger = logging.getLogger(__name__)

NO_COMMITS_FEEDBACK = "You haven't made any commits yet. Please make your changes and commit them using the git tools."


def _flock_nonblocking(lock_path: Path, lock_file: IO[str]) -> str | None:
    """Acquire an exclusive flock using non-blocking retries.

    Polls with LOCK_NB + short sleeps instead of a blocking LOCK_EX so the
    process is never stuck in an uninterruptible kernel call on NFS.

    Returns the identity of the previous lock holder (read from the lock
    file on first contention), or None if acquired immediately.
    """
    holder: str | None = None
    attempts = 0
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return holder
        except OSError:
            attempts += 1
            if attempts == 1:
                try:
                    holder = lock_path.read_text().strip() or None
                except Exception:
                    pass
                logger.info("Merge lock contention (held by %s), polling...", holder or "unknown")
            time.sleep(0.5)


class FailureCause(StrEnum):
    """Why a task failed — distinguishes agent-level from infrastructure failures."""

    TASK = "task"
    """Agents ran but could not solve the task (trace analysis is useful)."""

    INFRASTRUCTURE = "infrastructure"
    """Failure unrelated to the task: worker died, no agents available, etc.
    Trace analysis should be skipped — there is no meaningful trace to inspect."""


@dataclass
class ConcurrentResult:
    """Result of concurrent agents executing a task."""

    success: bool
    winner_id: str | None = None
    error: str | None = None
    failure_cause: FailureCause | None = None
    pre_merge_hash: str | None = None
    post_merge_hash: str | None = None


class ConcurrentAgents:
    """Races (agent, reviewer) pairs on a task. First to succeed wins.

    Subclass and override build() for domain-specific build checks.
    """

    def __init__(self, repo_root: Path | None = None, max_review_cycles: int = 0):
        self.repo_root = repo_root
        self.max_review_cycles = max_review_cycles
        self._merge_lock = asyncio.Lock()

    @traced
    async def build(self, agent: Agent, task: Task) -> tuple[bool, str]:
        """Check if code builds. Override in subclass.

        Returns:
            (success, feedback) — feedback contains error details if failed.
        """
        return True, ""

    def _build_review_prompt(self, agent: Agent, task: Task) -> str:
        """Build the review prompt. Override in subclass for domain-specific prompts."""
        wt = getattr(agent, "worktree_path", "unknown")
        return (
            f"Review the changes in: {wt}\n"
            f"Task: {task.description}\n"
            f"Check that the code is correct and complete.\n\n"
            f"Respond with APPROVED or REJECTED with feedback."
        )

    @traced
    async def review(
        self,
        agent: Agent,
        reviewer: Agent | None,
        task: Task,
    ) -> tuple[bool, str]:
        """LLM review using the reviewer agent."""
        if not reviewer:
            return True, ""

        prompt = self._build_review_prompt(agent, task)
        answer = await reviewer.call(prompt)

        if not answer:
            return False, "Review timed out (max turns reached)"

        approved = "APPROVED" in answer.upper()
        return approved, answer

    @traced
    def rebase(self, agent: Agent) -> tuple[bool, str]:
        """Rebase agent's worktree onto main (pre-review conflict check)."""
        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path or not self.repo_root:
            return True, ""
        return worktree.rebase_onto_main(Path(wt_path), self.repo_root)

    @traced
    def merge(self, agent: Agent, *, lock_held: bool = False) -> tuple[bool, str]:
        """Fast-forward main to agent's rebased HEAD."""
        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path or not self.repo_root:
            return True, ""
        return worktree.merge_to_main(Path(wt_path), self.repo_root, ff_only=True, lock_held=lock_held)

    def _has_commits(self, agent: Agent) -> bool:
        """Check if agent has commits."""
        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path or not self.repo_root:
            return True
        return worktree.has_commits(Path(wt_path), self.repo_root)

    def _is_worktree_clean(self, agent: Agent) -> bool:
        """Check if agent's worktree has no uncommitted changes."""
        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path:
            return True
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=wt_path,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() == ""

    def _rev_parse(self, path: Path, rev: str = "HEAD") -> str | None:
        """Return the hash of a git revision, or None on failure."""
        result = subprocess.run(
            ["git", "rev-parse", rev],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @traced
    def _sync_agent(self, agent: Agent) -> bool:
        """Sync agent's worktree to main. Returns False if sync fails."""
        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path or not self.repo_root:
            return True
        try:
            worktree.sync_to_main(Path(wt_path), self.repo_root)
            return True
        except RuntimeError as e:
            logger.error("Failed to sync agent %s: %s", agent.id, e)
            return False

    @traced
    async def _attempt_merge(self, agent: Agent) -> tuple[bool, str | None, str | None, str | None]:
        """Attempt to merge agent's work into main.

        Only one agent can attempt merge at a time (serialized by
        ``_merge_lock``). This prevents concurrent agents racing on the
        same task from flooding the merge queue with duplicate requests.

        Override in subclass to redirect merges to a queue.

        Returns:
            (landed, pre_hash, post_hash, failure_msg)
        """
        async with self._merge_lock:
            return await self._do_merge(agent)

    async def _do_merge(self, agent: Agent) -> tuple[bool, str | None, str | None, str | None]:
        """Inner merge logic, called with _merge_lock held."""
        wt = Path(getattr(agent, "worktree_path", ""))
        lock_path = self.repo_root / ".merge_lock" if self.repo_root else None
        lock_file: IO[str] | None = None
        try:
            if lock_path:
                lock_path.touch(exist_ok=True)
                lock_file = open(lock_path, "r+")
                logger.info("Agent %s: waiting for merge lock...", agent.id)
                blocked_by = await asyncio.to_thread(
                    _flock_nonblocking,
                    lock_path,
                    lock_file,
                )
                if blocked_by:
                    logger.info("Agent %s: merge lock acquired (was held by %s)", agent.id, blocked_by)
                else:
                    logger.info("Agent %s: merge lock acquired", agent.id)
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(agent.id)
                lock_file.flush()

            candidate_pre = self._rev_parse(self.repo_root, "HEAD") if self.repo_root else None
            merge_ok, merge_error = self.merge(agent, lock_held=lock_file is not None)
            if not merge_ok:
                # main advanced — rebase under lock, skip rebuild if clean
                rebase_ok2, rebase_error2 = self.rebase(agent)
                if not rebase_ok2:
                    return (
                        False,
                        None,
                        None,
                        f"Merge failed, and rebase onto updated main also failed:\n{rebase_error2}\n\n"
                        "Please fix the conflicts and commit.",
                    )
                # Clean rebase: agent's source files are unchanged
                # from the pre-lock build, so skip rebuild.
                candidate_pre = self._rev_parse(self.repo_root, "HEAD") if self.repo_root else None
                merge_ok, merge_error = self.merge(agent, lock_held=lock_file is not None)
                if not merge_ok:
                    logger.error(
                        "Agent %s: merge failed even under exclusive lock: %s",
                        agent.id,
                        merge_error,
                    )
                    return False, None, None, f"Merge failed unexpectedly:\n{merge_error}"

            if merge_ok:
                pre_hash = candidate_pre
                # Read from repo_root (not worktree) — merge() may
                # add follow-up commits (e.g. root import updates).
                post_hash = self._rev_parse(self.repo_root, "HEAD") if self.repo_root else self._rev_parse(wt, "HEAD")
                return True, pre_hash, post_hash, None
            return False, None, None, None
        finally:
            if lock_file:
                lock_file.close()
                logger.info("Agent %s: merge lock released", agent.id)

    async def run_task(
        self,
        task: Task,
        agents: list[Agent],
        get_reviewer: Callable[[str], Agent | None] | None = None,
        *,
        attempt_number: int = 1,
        trace_store: TraceStore | None = None,
    ) -> ConcurrentResult:
        """Run a task with the given agents. First to succeed wins."""
        attempt_prefix = f"tasks/{task.id}/attempt_{attempt_number}"
        step_ctx = step_trace_context(
            trace_id=f"{attempt_prefix}/steps",
            trace_store=trace_store,
        )

        async def agent_workflow(agent: Agent) -> tuple[str, bool, str | None, str | None]:
            reviewer = get_reviewer(agent.id) if get_reviewer else None
            if not self._sync_agent(agent):
                logger.error("Agent %s: worktree sync failed, skipping task", agent.id)
                return agent.id, False, None, None

            agent_trace = AgentTrace(id=agent.id, task_id=task.id)
            agent_trace.trace_id = f"{attempt_prefix}/{agent.id}"
            agent.set_trace(agent_trace)
            agent.reset()

            reviewer_trace: AgentTrace | None = None
            if reviewer:
                reviewer_trace = AgentTrace(id=reviewer.id, task_id=task.id)
                reviewer_trace.trace_id = f"{attempt_prefix}/{reviewer.id}"
                reviewer.set_trace(reviewer_trace)
                reviewer.reset()

            def _save_trace(t: AgentTrace) -> None:
                if trace_store:
                    trace_store.save(t)

            def _finalize_failed(error: str) -> None:
                agent_trace.finalize(
                    status="failed",
                    total_turns=agent.total_turns,
                    messages=agent.messages,
                    error=error,
                )
                _save_trace(agent_trace)
                if reviewer and reviewer_trace:
                    reviewer_trace.finalize(
                        status="not_invoked",
                        total_turns=0,
                        messages=[],
                    )
                    _save_trace(reviewer_trace)

            try:

                async def _ask_to_fix(prompt: str, error_msg: str = "Turn limit reached") -> bool:
                    """Ask agent to fix an issue. Returns False if agent hit turn limit."""
                    response = await agent.call(prompt)
                    if not response:
                        _finalize_failed(error_msg)
                        return False
                    return True

                response = await agent.call(f"[Task: {task.id}]\n\n{task.description}")
                if not response:
                    _finalize_failed("Agent produced no response on initial call")
                    return agent.id, False, None, None

                review_cycles = 0

                while True:
                    if not self._has_commits(agent):
                        if self._is_worktree_clean(agent):
                            if not await _ask_to_fix(
                                "You haven't written any files or made any commits. "
                                "Write your solution to a .lean file and commit it with git_add + git_commit.",
                                "No commits and no changes — agent produced nothing",
                            ):
                                return agent.id, False, None, None
                            continue

                        if not await _ask_to_fix(NO_COMMITS_FEEDBACK, "Turn limit reached without commits"):
                            return agent.id, False, None, None
                        continue

                    rebase_ok, rebase_error = self.rebase(agent)
                    if not rebase_ok:
                        if not await _ask_to_fix(
                            f"Rebase onto main failed (conflicts):\n{rebase_error}\n\nPlease fix the conflicts and commit."
                        ):
                            return agent.id, False, None, None
                        continue

                    build_ok, build_feedback = await self.build(agent, task)
                    if not build_ok:
                        if not await _ask_to_fix(
                            f"Build failed:\n{build_feedback}\n\nPlease fix the errors and commit."
                        ):
                            return agent.id, False, None, None
                        continue

                    if reviewer:
                        reviewer.reset()
                    review_ok, review_feedback = await self.review(agent, reviewer, task)
                    if not review_ok:
                        review_cycles += 1
                        if self.max_review_cycles and review_cycles >= self.max_review_cycles:
                            logger.warning(
                                "Agent %s: max review cycles (%d) reached for task %s",
                                agent.id,
                                self.max_review_cycles,
                                task.id,
                            )
                            _finalize_failed(f"Max review cycles ({self.max_review_cycles}) reached")
                            return agent.id, False, None, None
                        if not await _ask_to_fix(f"Review failed:\n{review_feedback}\n\nPlease fix and commit."):
                            return agent.id, False, None, None
                        continue

                    # Merge — delegated to _attempt_merge (overridable).
                    merge_landed, pre_hash, post_hash, merge_failure_msg = await self._attempt_merge(agent)

                    if not merge_landed:
                        if merge_failure_msg:
                            if not await _ask_to_fix(merge_failure_msg):
                                return agent.id, False, None, None
                        else:
                            logger.warning(
                                "Agent %s: merge not landed but no failure message — retrying",
                                agent.id,
                            )
                        continue

                    # Success
                    agent_trace.finalize(
                        status="success",
                        total_turns=agent.total_turns,
                        messages=agent.messages,
                    )
                    _save_trace(agent_trace)
                    if reviewer and reviewer_trace:
                        reviewer_trace.finalize(
                            status="completed",
                            total_turns=reviewer.total_turns,
                            messages=reviewer.messages,
                        )
                        _save_trace(reviewer_trace)
                    return agent.id, True, pre_hash, post_hash

            except asyncio.CancelledError:
                agent_trace.finalize(
                    status="cancelled",
                    total_turns=agent.total_turns,
                    messages=agent.messages,
                )
                _save_trace(agent_trace)
                if reviewer and reviewer_trace:
                    reviewer_trace.finalize(
                        status="cancelled",
                        total_turns=reviewer.total_turns,
                        messages=reviewer.messages,
                    )
                    _save_trace(reviewer_trace)
                raise
            except Exception as e:
                _finalize_failed(str(e))
                return agent.id, False, None, None
            finally:
                agent.set_trace(None)
                if reviewer:
                    reviewer.set_trace(None)

        with step_ctx:
            winner_id = None
            pre_merge_hash = None
            post_merge_hash = None
            try:
                futures = [asyncio.create_task(agent_workflow(agent)) for agent in agents]

                for coro in asyncio.as_completed(futures):
                    agent_id, success, pre_hash, post_hash = await coro
                    if success:
                        winner_id = agent_id
                        pre_merge_hash = pre_hash
                        post_merge_hash = post_hash
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        await asyncio.gather(*futures, return_exceptions=True)
                        break
            finally:
                step_ctx.winner_id = winner_id
                step_ctx.final_status = "success" if winner_id else "failed"

        if trace_store:
            trace_store.save(step_ctx)

        return ConcurrentResult(
            success=winner_id is not None,
            winner_id=winner_id,
            error=None if winner_id else "All agents failed",
            failure_cause=None if winner_id else FailureCause.TASK,
            pre_merge_hash=pre_merge_hash,
            post_merge_hash=post_merge_hash,
        )
