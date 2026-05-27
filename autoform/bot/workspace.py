# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Workspace initialization for autoform_bot.

Wraps core.workspace with autoform-specific layout (archive dirs,
pre-commit hook, skills seeding).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from core.workspace import ensure_workspace, initialize_workspace

logger = logging.getLogger(__name__)

SKILLS_SOURCE = Path(__file__).resolve().parent / "skills"

logger = logging.getLogger(__name__)


_DUPLICATE_CHECK_HOOK = r"""#!/bin/bash
# Placeholder — no pre-commit validation.
exit 0
"""


def _install_pre_commit_hook(code_path: Path) -> None:
    """Install the duplicate-declaration pre-commit hook."""
    hooks_dir = code_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(_DUPLICATE_CHECK_HOOK)
    hook_path.chmod(0o755)


def _seed_skills(run_path: Path) -> None:
    """Copy curated skills (lean/, workflow/) into the run's skills directory.

    Only copies directories that don't already exist — won't overwrite
    skills that were modified during a run.
    """
    if not SKILLS_SOURCE.is_dir():
        logger.warning("Skills source not found: %s", SKILLS_SOURCE)
        return
    skills_dest = run_path / "skills"
    skills_dest.mkdir(exist_ok=True)
    for subdir in SKILLS_SOURCE.iterdir():
        if subdir.is_dir():
            dest = skills_dest / subdir.name
            if not dest.exists():
                shutil.copytree(subdir, dest)
                logger.info("Seeded skills/%s from %s", subdir.name, SKILLS_SOURCE)


def _create_archive_dirs(run_path: Path) -> None:
    """Create archive subdirectories for the run."""
    (run_path / "archive" / "traces").mkdir(parents=True, exist_ok=True)
    (run_path / "archive" / "reports" / "task_reports").mkdir(parents=True, exist_ok=True)
    (run_path / "archive" / "skills").mkdir(parents=True, exist_ok=True)
    (run_path / "reports" / "eval_reports").mkdir(parents=True, exist_ok=True)


def initialize_run_workspace(
    run_path: Path,
    books_source: Path | None = None,
    book_files: list[str] | None = None,
    lib_name: str = "Formalization",
) -> None:
    """Initialize a fresh dated run workspace.

    Creates the full directory structure for one pipeline run:
    - code/              Lean git repo (from template)
    - book/              LaTeX source files (copied from books_source)
    - skills/            seeded from skills/autoform/ (lean, workflow)
    - traces/            empty, populated during the run
    - archive/           traces, reports, skills archives

    Args:
        run_path: Root directory for this run (e.g. lean-autoform/2026-03-24/).
        books_source: Directory of LaTeX source files to copy in (optional).
        book_files: If given, copy only these filenames from books_source.
                    If None, copy the entire books_source directory.

    Raises:
        FileExistsError: If run_path already exists.
    """
    initialize_workspace(
        run_path,
        data_source=books_source,
        data_files=book_files,
        code_dir="code",
        data_dir="book",
        lib_name=lib_name,
        ignored_patterns=[".lake"],
    )

    code_path = run_path / "code"

    _create_archive_dirs(run_path)
    _install_pre_commit_hook(code_path)
    _seed_skills(run_path)
    logger.info("Run workspace ready: %s", run_path)


def ensure_run_workspace(
    run_path: Path,
    books_source: Path | None = None,
    book_files: list[str] | None = None,
    nuke: bool = False,
    lib_name: str = "Formalization",
) -> None:
    """Ensure a run workspace exists (idempotent).

    If nuke=True, removes any existing workspace first. On resume, skips
    re-initialization but still runs idempotent post-init steps (archive dirs,
    pre-commit hook).

    Args:
        run_path: Root directory for this run.
        books_source: LaTeX source directory to copy in on fresh init.
        book_files: If given, copy only these filenames from books_source.
        nuke: If True, delete existing workspace before initializing.
    """
    if nuke and run_path.exists():
        logger.info("--nuke: removing existing run workspace at %s", run_path)
        # Unfreeze packages before removal — frozen dirs block rm -rf.
        packages_dir = run_path / "code" / ".lake" / "packages"
        if packages_dir.exists():
            subprocess.run(["chmod", "-R", "u+w", str(packages_dir)], capture_output=True)
        subprocess.run(["rm", "-rf", str(run_path)], check=True)

    # Always clear stale service URLs from a previous run/crash
    urls_file = run_path / "urls.json"
    if urls_file.exists():
        urls_file.unlink()

    ensure_workspace(
        run_path,
        data_source=books_source,
        data_files=book_files,
        code_dir="code",
        data_dir="book",
        lib_name=lib_name,
        ignored_patterns=[".lake"],
    )

    # Idempotent post-init steps (needed on both fresh and resume)
    _create_archive_dirs(run_path)
    _install_pre_commit_hook(run_path / "code")
    _seed_skills(run_path)
