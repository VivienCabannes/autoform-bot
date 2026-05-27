#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Garbage-collect stale worktree run directories.

Scans ``<project_dir>/worktrees/`` for ``run-*`` subdirectories and deletes
those older than a configurable age threshold.  Also removes legacy flat
worktree directories (pre-migration format) and the old
``merge_eval_worktrees/`` directory if present.

After deletion, runs ``git worktree prune`` on the code repo to clean up
git's internal ``.git/worktrees/`` references.

Usage:
    python scripts/gc_worktrees.py /path/to/algebraic_topology_I
    python scripts/gc_worktrees.py /path/to/algebraic_topology_I --max-age-hours 12
    python scripts/gc_worktrees.py /path/to/algebraic_topology_I --dry-run
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

_MAX_PARALLEL_DELETES = 50

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _age_hours(path: Path) -> float:
    """Return the age of *path* in hours based on its mtime."""
    return (time.time() - path.stat().st_mtime) / 3600


def gc_worktrees(
    project_dir: Path,
    *,
    max_age_hours: float = 24.0,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Garbage-collect old worktree run directories.

    Returns a dict with ``deleted`` and ``failed`` lists of directory names.
    """
    code_dir = project_dir / "code"
    repo_root = code_dir if code_dir.is_dir() else None
    worktrees_dir = project_dir / "worktrees"
    results: dict[str, list[str]] = {"deleted": [], "failed": [], "skipped": []}

    dirs_to_scan: list[Path] = []

    # Collect all directories under worktrees/
    if worktrees_dir.is_dir():
        for entry in sorted(worktrees_dir.iterdir()):
            if entry.is_dir():
                dirs_to_scan.append(entry)

    # Also collect legacy merge_eval_worktrees/ directory
    legacy_merge_eval = project_dir / "merge_eval_worktrees"
    if legacy_merge_eval.is_dir():
        dirs_to_scan.append(legacy_merge_eval)

    if not dirs_to_scan:
        logger.info("No worktree directories found in %s", project_dir)
        # Still prune — orphaned .git/worktrees/ entries may exist
        if not dry_run and repo_root is not None:
            logger.info("Running git worktree prune...")
            subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "prune"],
                capture_output=True,
            )
        return results

    to_delete: list[Path] = []
    for d in dirs_to_scan:
        try:
            age = _age_hours(d)
        except OSError:
            logger.warning("Cannot stat %s, skipping", d)
            results["skipped"].append(d.name)
            continue

        if age < max_age_hours:
            logger.info("  KEEP  %s (%.1fh old)", d.name, age)
            results["skipped"].append(d.name)
            continue

        if dry_run:
            logger.info("  WOULD DELETE  %s (%.1fh old)", d.name, age)
            results["deleted"].append(d.name)
            continue

        logger.info("  DELETE  %s (%.1fh old)", d.name, age)
        to_delete.append(d)

    # Delete in parallel — two passes to avoid parent/child race on NFS.
    # Pass 1: delete individual worktrees within each run dir concurrently.
    children: list[tuple[Path, str]] = []
    parents: list[Path] = []
    for d in to_delete:
        child_dirs = [c for c in d.iterdir() if c.is_dir()] if d.is_dir() else []
        if child_dirs:
            for child in child_dirs:
                children.append((child, f"{d.name}/{child.name}"))
            parents.append(d)
        else:
            parents.append(d)

    for batch_start in range(0, len(children), _MAX_PARALLEL_DELETES):
        batch = children[batch_start : batch_start + _MAX_PARALLEL_DELETES]
        for _, label in batch:
            logger.info("    rm -rf %s", label)
        procs = [subprocess.Popen(["rm", "-rf", str(path)]) for path, _ in batch]
        for (_, label), proc in zip(batch, procs):
            proc.wait()
            logger.info("    done    %s", label)

    # Pass 2: remove now-empty parent directories.
    for d in parents:
        logger.info("    rm -rf %s (parent)", d.name)
        subprocess.run(["rm", "-rf", str(d)], capture_output=True)
        logger.info("    done    %s (parent)", d.name)

    for d in to_delete:
        if not d.exists():
            results["deleted"].append(d.name)
        else:
            logger.error("  FAILED to delete %s", d.name)
            results["failed"].append(d.name)

    # Prune git's internal worktree references
    if not dry_run and repo_root is not None:
        # Unlock orphaned worktrees so prune can remove them.
        git_wt_dir = repo_root / ".git" / "worktrees"
        if git_wt_dir.is_dir():
            for wt in git_wt_dir.iterdir():
                lock_file = wt / "locked"
                if lock_file.exists():
                    lock_file.unlink()
                    logger.info("Unlocked orphaned worktree %s", wt.name)
        logger.info("Running git worktree prune...")
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True,
        )

    deleted_count = len(results["deleted"])
    failed_count = len(results["failed"])
    action = "Would delete" if dry_run else "Deleted"
    logger.info(
        "%s %d dir(s), %d failed, %d kept",
        action,
        deleted_count,
        failed_count,
        len(results["skipped"]),
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Garbage-collect stale worktree directories.")
    parser.add_argument("project_dir", type=Path, help="Project directory (e.g. algebraic_topology_I/)")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Delete run directories older than this many hours (default: 24)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be deleted without actually deleting",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        logger.error("Project directory does not exist: %s", project_dir)
        sys.exit(1)

    results = gc_worktrees(project_dir, max_age_hours=args.max_age_hours, dry_run=args.dry_run)
    if results["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
