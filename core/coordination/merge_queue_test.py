# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for MergeQueue — bors-style batch merge train."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from core.coordination.merge_queue import MergeQueue, MergeStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed:\n{result.stderr}"
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a bare-bones git repo with one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _make_worktree(repo: Path, name: str) -> Path:
    """Create a worktree with detached HEAD at main."""
    wt = repo.parent / "worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(wt), "main")
    _git(wt, "config", "user.email", "test@test.com")
    _git(wt, "config", "user.name", "Test")
    return wt


def _commit_file(wt: Path, filename: str, content: str, msg: str) -> str:
    """Write a file, commit, return the hash."""
    (wt / filename).write_text(content)
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", msg)
    return _git(wt, "rev-parse", "HEAD")


async def _always_pass(staging_path: Path) -> tuple[bool, str]:
    return True, ""


async def _always_fail(staging_path: Path) -> tuple[bool, str]:
    return False, "build broke"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMergeQueueBasic:
    """Single-agent and happy-path batch tests."""

    @pytest.mark.asyncio
    async def test_single_agent_merges(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, "agent-0")
        _commit_file(wt, "a.txt", "hello\n", "add a")

        queue = MergeQueue(repo, _always_pass, batch_size=1, batch_timeout=0.1)
        task = asyncio.create_task(queue.run())

        result = await queue.submit("agent-0", wt)
        queue.stop()
        await task

        assert result.status == MergeStatus.MERGED
        assert result.pre_hash is not None
        assert result.post_hash is not None
        assert result.pre_hash != result.post_hash
        # File should be on main
        assert (repo / "a.txt").read_text() == "hello\n"

    @pytest.mark.asyncio
    async def test_batch_of_three_independent(self, tmp_path: Path) -> None:
        """Three agents modifying different files — all should merge in one build."""
        repo = _init_repo(tmp_path)
        build_count = 0

        async def counting_build(path: Path) -> tuple[bool, str]:
            nonlocal build_count
            build_count += 1
            return True, ""

        wts = []
        for i in range(3):
            wt = _make_worktree(repo, f"agent-{i}")
            _commit_file(wt, f"file_{i}.txt", f"content {i}\n", f"add file_{i}")
            wts.append(wt)

        queue = MergeQueue(repo, counting_build, batch_size=3, batch_timeout=1.0)
        task = asyncio.create_task(queue.run())

        results = await asyncio.gather(
            queue.submit("agent-0", wts[0]),
            queue.submit("agent-1", wts[1]),
            queue.submit("agent-2", wts[2]),
        )
        queue.stop()
        await task

        assert all(r.status == MergeStatus.MERGED for r in results)
        # All share the same pre/post hash (one batch)
        assert results[0].post_hash == results[1].post_hash == results[2].post_hash
        # Only ONE build for the whole batch
        assert build_count == 1
        # All files on main
        for i in range(3):
            assert (repo / f"file_{i}.txt").read_text() == f"content {i}\n"


class TestMergeQueueConflicts:
    """Conflict detection and deferral."""

    @pytest.mark.asyncio
    async def test_rebase_conflict_with_main_rejects(self, tmp_path: Path) -> None:
        """Agent conflicts with main → immediate rejection."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, "agent-0")

        # Agent modifies base.txt
        _commit_file(wt, "base.txt", "agent version\n", "modify base")

        # Meanwhile, main also modifies base.txt
        (repo / "base.txt").write_text("main version\n")
        _git(repo, "add", "base.txt")
        _git(repo, "commit", "-m", "main modifies base")

        queue = MergeQueue(repo, _always_pass, batch_size=1, batch_timeout=0.1)
        task = asyncio.create_task(queue.run())

        result = await queue.submit("agent-0", wt)
        queue.stop()
        await task

        assert result.status == MergeStatus.REJECTED
        assert "Rebase onto main failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_inter_agent_conflict_defers_then_lands(self, tmp_path: Path) -> None:
        """Two agents add different files but cherry-pick conflicts on staging.

        Agent-1's cherry-pick onto staging (which has agent-0's changes) conflicts.
        Agent-1 gets deferred to the next batch. After agent-0 merges to main,
        agent-1 rebases onto the new main and lands in batch 2.
        """
        repo = _init_repo(tmp_path)

        # Agent-0 adds a new section to base.txt
        wt0 = _make_worktree(repo, "agent-0")
        _commit_file(wt0, "base.txt", "base\nagent-0 addition\n", "agent-0 extends base")

        # Agent-1 also adds a different section to base.txt — cherry-pick will conflict
        wt1 = _make_worktree(repo, "agent-1")
        _commit_file(wt1, "base.txt", "base\nagent-1 addition\n", "agent-1 extends base")
        # Also add a unique file so agent-1 has something to merge after resolving
        _commit_file(wt1, "only_1.txt", "unique\n", "agent-1 unique file")

        build_count = 0

        async def counting_build(path: Path) -> tuple[bool, str]:
            nonlocal build_count
            build_count += 1
            return True, ""

        # batch_size=2 so both are collected in first batch
        queue = MergeQueue(repo, counting_build, batch_size=2, batch_timeout=1.0)
        task = asyncio.create_task(queue.run())

        r0, r1 = await asyncio.gather(
            queue.submit("agent-0", wt0),
            queue.submit("agent-1", wt1),
        )
        queue.stop()
        await task

        # Agent-0 merges in batch 1
        assert r0.status == MergeStatus.MERGED
        # Agent-1: deferred from batch 1 (cherry-pick conflict), then in batch 2
        # it rebases onto new main (which has agent-0's changes) — this is a
        # real conflict, so it gets rejected.
        assert r1.status == MergeStatus.REJECTED

    @pytest.mark.asyncio
    async def test_inter_agent_no_conflict_defers_then_lands(self, tmp_path: Path) -> None:
        """Two agents touch the same file on staging but not on main.

        Agent-0 adds line to base.txt. Agent-1 adds a DIFFERENT non-overlapping
        change. Cherry-pick onto staging may conflict, but after agent-0 merges
        to main, agent-1 rebases cleanly and lands in batch 2.
        """
        repo = _init_repo(tmp_path)

        # Make base.txt have multiple lines so changes don't overlap
        (repo / "base.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
        _git(repo, "add", "base.txt")
        _git(repo, "commit", "-m", "expand base")

        # Agent-0 modifies top of file
        wt0 = _make_worktree(repo, "agent-0")
        _commit_file(wt0, "a.txt", "agent-0\n", "agent-0 adds a.txt")

        # Agent-1 adds a completely different file
        wt1 = _make_worktree(repo, "agent-1")
        _commit_file(wt1, "b.txt", "agent-1\n", "agent-1 adds b.txt")

        build_count = 0

        async def counting_build(path: Path) -> tuple[bool, str]:
            nonlocal build_count
            build_count += 1
            return True, ""

        queue = MergeQueue(repo, counting_build, batch_size=2, batch_timeout=1.0)
        task = asyncio.create_task(queue.run())

        r0, r1 = await asyncio.gather(
            queue.submit("agent-0", wt0),
            queue.submit("agent-1", wt1),
        )
        queue.stop()
        await task

        # Both should merge — no conflicts at any stage
        assert r0.status == MergeStatus.MERGED
        assert r1.status == MergeStatus.MERGED
        assert (repo / "a.txt").exists()
        assert (repo / "b.txt").exists()
        # Should be ONE build (both stage cleanly in one batch)
        assert build_count == 1


class TestMergeQueueBuildFailure:
    """Build failure and bisection."""

    @pytest.mark.asyncio
    async def test_single_agent_build_failure_rejects(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, "agent-0")
        _commit_file(wt, "bad.txt", "broken\n", "add bad file")

        queue = MergeQueue(repo, _always_fail, batch_size=1, batch_timeout=0.1)
        task = asyncio.create_task(queue.run())

        result = await queue.submit("agent-0", wt)
        queue.stop()
        await task

        assert result.status == MergeStatus.REJECTED
        assert "Build failed" in (result.error or "")
        # main should be unchanged
        assert not (repo / "bad.txt").exists()

    @pytest.mark.asyncio
    async def test_bisect_finds_culprit(self, tmp_path: Path) -> None:
        """Batch of 3, one causes build failure. Bisect should isolate it."""
        repo = _init_repo(tmp_path)

        wts = []
        for i in range(3):
            wt = _make_worktree(repo, f"agent-{i}")
            _commit_file(wt, f"file_{i}.txt", f"content {i}\n", f"add file_{i}")
            wts.append(wt)

        culprit_file = "file_1.txt"  # agent-1 is the culprit

        async def selective_build(path: Path) -> tuple[bool, str]:
            """Fails if the culprit file is present."""
            if (path / culprit_file).exists():
                return False, f"{culprit_file} breaks the build"
            return True, ""

        queue = MergeQueue(repo, selective_build, batch_size=3, batch_timeout=1.0)
        task = asyncio.create_task(queue.run())

        results = await asyncio.gather(
            queue.submit("agent-0", wts[0]),
            queue.submit("agent-1", wts[1]),
            queue.submit("agent-2", wts[2]),
        )
        queue.stop()
        await task

        # agent-1 should be rejected, others merged
        assert results[0].status == MergeStatus.MERGED
        assert results[1].status == MergeStatus.REJECTED
        assert results[2].status == MergeStatus.MERGED
        # Culprit file should NOT be on main
        assert not (repo / culprit_file).exists()
        # Other files should be on main
        assert (repo / "file_0.txt").exists()
        assert (repo / "file_2.txt").exists()


class TestMergeQueueMultipleCommits:
    """Agents with multiple commits per worktree."""

    @pytest.mark.asyncio
    async def test_multiple_commits_per_agent(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, "agent-0")
        _commit_file(wt, "a.txt", "first\n", "commit 1")
        _commit_file(wt, "b.txt", "second\n", "commit 2")
        _commit_file(wt, "c.txt", "third\n", "commit 3")

        queue = MergeQueue(repo, _always_pass, batch_size=1, batch_timeout=0.1)
        task = asyncio.create_task(queue.run())

        result = await queue.submit("agent-0", wt)
        queue.stop()
        await task

        assert result.status == MergeStatus.MERGED
        assert (repo / "a.txt").read_text() == "first\n"
        assert (repo / "b.txt").read_text() == "second\n"
        assert (repo / "c.txt").read_text() == "third\n"
