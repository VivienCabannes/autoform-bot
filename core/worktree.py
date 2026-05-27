# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Git worktree utilities — standalone functions for worktree operations.

Pure functions for creating, syncing, merging, and cleaning up git worktrees.
Agents USE these functions but don't subclass anything.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from typing import Any
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_DIFF_TRUNCATION = 2000
_LOCAL_TIMEOUT = 300
_NETWORK_TIMEOUT = 120


def _noninteractive_env() -> dict[str, str]:
    """Current process env with editor/prompt suppression for headless git."""
    return {**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"}


def create_worktree(
    repo_root: Path,
    branch_name: str,
    *,
    worktrees_dir: Path | None = None,
) -> Path:
    """Create a git worktree with detached HEAD.

    If the worktree directory already exists, validates it. If broken,
    removes and recreates it.

    Args:
        repo_root: Path to the main git repository.
        branch_name: Name for the worktree directory.
        worktrees_dir: Parent directory for the worktree. Defaults to
            ``repo_root/../worktrees/`` when not provided.

    Returns:
        Path to the created worktree.

    Raises:
        RuntimeError: If worktree creation fails.
    """
    if worktrees_dir is None:
        worktrees_dir = repo_root.parent / "worktrees"
    worktree_path = worktrees_dir / branch_name

    if worktree_path.exists():
        # Validate — check if git recognizes it as a valid worktree
        check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            return worktree_path
        # Broken — remove and recreate
        logger.warning("Worktree %s is corrupted — recreating", worktree_path)
        cleanup_worktree(worktree_path, repo_root)

        if worktree_path.exists():
            # cleanup_worktree couldn't fully remove (e.g. NFS locks) —
            # move it aside so git can create a fresh worktree
            stale = worktree_path.with_name(worktree_path.name + ".stale")
            if stale.exists():
                shutil.rmtree(stale, ignore_errors=True)
            try:
                worktree_path.rename(stale)
                logger.warning("Moved undeletable worktree to %s", stale)
            except OSError:
                # Last resort — nuke everything we can
                for child in worktree_path.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except OSError:
                            pass

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "--force", "--force", "--detach", str(worktree_path), "main"],
        capture_output=True,
        text=True,
        timeout=_LOCAL_TIMEOUT,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create worktree at {worktree_path}:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    if not worktree_path.exists():
        raise RuntimeError(f"Worktree directory {worktree_path} does not exist after git worktree add")

    return worktree_path


def sync_to_main(worktree_path: Path, repo_root: Path) -> None:
    """Reset worktree to latest main (before task starts).

    Fetches latest main, detaches HEAD, deletes all local branches,
    hard-resets to main, and cleans untracked files. Raises on failure.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the main git repository.

    Raises:
        RuntimeError: If any git operation fails.
    """

    def _run(cmd: list[str], timeout: int = _NETWORK_TIMEOUT) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"sync_to_main failed in {worktree_path}:\n  cmd: {cmd}\n  stderr: {result.stderr.strip()}"
            )
        return result

    # Abort any in-progress rebase/merge
    subprocess.run(["git", "rebase", "--abort"], capture_output=True, cwd=worktree_path)
    subprocess.run(["git", "merge", "--abort"], capture_output=True, cwd=worktree_path)

    # Fetch latest main
    _run(["git", "fetch", str(repo_root), "main"])

    # Discard any uncommitted changes before switching HEAD
    _run(["git", "reset", "--hard", "HEAD"], timeout=_LOCAL_TIMEOUT)
    _run(["git", "clean", "-fd"], timeout=_LOCAL_TIMEOUT)

    # Detach HEAD and reset to main
    _run(["git", "checkout", "--detach", "FETCH_HEAD"], timeout=_LOCAL_TIMEOUT)
    _run(["git", "reset", "--hard", "FETCH_HEAD"], timeout=_LOCAL_TIMEOUT)
    _run(["git", "clean", "-fd"], timeout=_LOCAL_TIMEOUT)

    # Delete all local branches to prevent stale branch contamination
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        cwd=worktree_path,
        timeout=_LOCAL_TIMEOUT,
    )
    for branch in branches.stdout.strip().splitlines():
        branch = branch.strip()
        if branch and branch != "main":
            logger.debug("Deleting stale branch %s in %s", branch, worktree_path)
            subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True,
                cwd=worktree_path,
                timeout=_LOCAL_TIMEOUT,
            )


def has_commits(worktree_path: Path, repo_root: Path) -> bool:
    """Check if there are commits on top of main.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the main git repository.

    Returns:
        True if there are commits beyond main.
    """
    main_hash = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "main"],
        capture_output=True,
        text=True,
        timeout=_LOCAL_TIMEOUT,
    ).stdout.strip()

    result = subprocess.run(
        ["git", "log", f"{main_hash}..HEAD", "--oneline"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=_LOCAL_TIMEOUT,
    )
    return bool(result.stdout.strip())


def git_run(
    cmd: list[str], cwd: Path, timeout: int = _LOCAL_TIMEOUT, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run a git command, decoding output as UTF-8 with replacement for bad bytes."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, **kwargs)
    r.stdout = r.stdout.decode("utf-8", errors="replace") if isinstance(r.stdout, bytes) else r.stdout
    r.stderr = r.stderr.decode("utf-8", errors="replace") if isinstance(r.stderr, bytes) else r.stderr
    return r


def rebase_onto_main(
    worktree_path: Path,
    repo_root: Path,
    *,
    diff_truncation: int = DEFAULT_DIFF_TRUNCATION,
) -> tuple[bool, str]:
    """Rebase worktree's commits onto latest main.

    Call before review to catch conflicts cheaply (no LLM cost).

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the main git repository.

    Returns:
        (success, error_message) — error_message includes conflict details if any.
    """
    try:
        fetch = git_run(
            ["git", "fetch", str(repo_root), "main"],
            cwd=worktree_path,
            timeout=_NETWORK_TIMEOUT,
        )
        if fetch.returncode != 0:
            return False, f"Failed to fetch main: {fetch.stderr}"

        result = git_run(
            ["git", "rebase", "FETCH_HEAD"],
            cwd=worktree_path,
            timeout=_NETWORK_TIMEOUT,
            env=_noninteractive_env(),
        )
    except subprocess.TimeoutExpired:
        return False, f"rebase_onto_main timed out after {_NETWORK_TIMEOUT}s"

    if result.returncode != 0:
        error_msg = result.stderr + result.stdout

        conflicts_result = git_run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=worktree_path,
        )
        conflicted_files = conflicts_result.stdout.strip()

        if conflicted_files:
            diff_result = git_run(["git", "diff"], cwd=worktree_path)
            conflict_diff = diff_result.stdout[:diff_truncation]
            error_msg += f"\n\nConflicted files:\n{conflicted_files}\n\nConflict diff:\n{conflict_diff}"

        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_path,
            capture_output=True,
            timeout=_LOCAL_TIMEOUT,
        )
        return False, error_msg

    return True, ""


def _reset_repo(repo_root: Path) -> None:
    """Reset repo working tree and index to HEAD, remove untracked files."""
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_LOCAL_TIMEOUT,
    )
    subprocess.run(
        ["git", "clean", "-fd", "-e", ".*_lock"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_LOCAL_TIMEOUT,
    )


def merge_to_main(
    worktree_path: Path,
    repo_root: Path,
    *,
    ff_only: bool = False,
    diff_truncation: int = DEFAULT_DIFF_TRUNCATION,
    lock_held: bool = False,
) -> tuple[bool, str]:
    """Merge worktree's changes to main.

    Acquires an exclusive file lock on ``repo_root/.merge_lock`` to prevent
    concurrent merges from corrupting the index. Resets the working tree
    before and after merge to ensure clean state.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the main git repository.
        ff_only: If True, only allow fast-forward merges (no merge commits).
            Use after rebase_onto_main for a clean linear history.
        diff_truncation: Max characters of conflict diff to include.
        lock_held: If True, the caller already holds the merge lock —
            skip internal lock acquisition.

    Returns:
        (success, error_message) — error_message includes conflict details if any.
    """
    if lock_held:
        return _merge_to_main_locked(
            worktree_path,
            repo_root,
            ff_only=ff_only,
            diff_truncation=diff_truncation,
        )
    lock_path = repo_root / ".merge_lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return _merge_to_main_locked(
            worktree_path,
            repo_root,
            ff_only=ff_only,
            diff_truncation=diff_truncation,
        )


def _merge_to_main_locked(
    worktree_path: Path,
    repo_root: Path,
    *,
    ff_only: bool = False,
    diff_truncation: int = DEFAULT_DIFF_TRUNCATION,
) -> tuple[bool, str]:
    """Inner merge logic, called with the merge lock held."""
    merge_flag = "--ff-only" if ff_only else "--no-edit"

    # Ensure clean working tree before merge — previous failed merges
    # from concurrent callers may have left dirty index/working tree.
    _reset_repo(repo_root)

    try:
        fetch = subprocess.run(
            ["git", "fetch", str(worktree_path), "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_NETWORK_TIMEOUT,
            env=_noninteractive_env(),
        )
        if fetch.returncode != 0:
            return False, f"Failed to fetch from worktree: {fetch.stderr}"

        result = subprocess.run(
            ["git", "merge", merge_flag, "FETCH_HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
            env=_noninteractive_env(),
        )
    except subprocess.TimeoutExpired:
        _reset_repo(repo_root)
        return False, f"merge_to_main timed out after {_NETWORK_TIMEOUT}s"

    if result.returncode != 0:
        error_msg = result.stderr + result.stdout

        # ff-only failures don't leave merge state — no conflicts to inspect
        if not ff_only:
            conflicts_result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=_LOCAL_TIMEOUT,
            )
            conflicted_files = conflicts_result.stdout.strip()

            if conflicted_files:
                diff_result = subprocess.run(
                    ["git", "diff"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=_LOCAL_TIMEOUT,
                )
                conflict_diff = diff_result.stdout[:diff_truncation]
                error_msg += f"\n\nConflicted files:\n{conflicted_files}\n\nConflict diff:\n{conflict_diff}"

            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_root,
                capture_output=True,
                timeout=_LOCAL_TIMEOUT,
            )

        # Always reset after failed merge to prevent dirty state leaking.
        _reset_repo(repo_root)
        return False, error_msg

    # Pack loose objects when they accumulate past git's threshold.
    subprocess.run(
        ["git", "gc", "--auto"],
        cwd=repo_root,
        capture_output=True,
        timeout=_LOCAL_TIMEOUT,
    )

    return True, ""


def cleanup_worktree(worktree_path: Path, repo_root: Path) -> None:
    """Remove a worktree.

    Tries git worktree remove first. If that fails (corrupted .git),
    removes the directory manually and prunes stale references.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the main git repository.
    """
    import shutil

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("cleanup_worktree timed out after %ds", _LOCAL_TIMEOUT)
        result = None

    if (result is None or result.returncode != 0) and worktree_path.exists():
        # git worktree remove failed — remove manually
        logger.warning("git worktree remove failed for %s, removing manually", worktree_path)
        # Remove git's internal worktree reference
        internal = repo_root / ".git" / "worktrees" / worktree_path.name
        if internal.exists():
            shutil.rmtree(internal, ignore_errors=True)
        # Remove the worktree directory itself
        shutil.rmtree(worktree_path, ignore_errors=True)
        # Prune stale references
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True,
            timeout=_LOCAL_TIMEOUT,
        )
