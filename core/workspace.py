# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Workspace initialization from the project template.

Copies the pre-built template/ directory, resolves relative submodule
paths to absolute, and initializes a git repo.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from core.constants import REPO_ROOT

logger = logging.getLogger(__name__)


def _resolve_relative_paths(
    file_path: Path,
    base_dir: Path,
    keys: Sequence[str],
) -> None:
    """Resolve relative path values for given keys in a config file.

    Matches key-value patterns in both TOML (key = "../path") and
    JSON ("key": "../path") and resolves them relative to base_dir.
    """
    content = file_path.read_text()
    for key in keys:
        pattern = rf'("?{re.escape(key)}"?\s*[=:]\s*")(\.\.?/[^"]*)"'
        content = re.sub(
            pattern,
            lambda m: m.group(1) + str((base_dir / m.group(2)).resolve()) + '"',
            content,
        )
    file_path.write_text(content)


def copy_template(
    code_path: Path,
    lib_name: str = "Formalization",
    ignored_patterns: Sequence[str] = (),
) -> None:
    """Copy the project template to create a new workspace.

    Copies REPO_ROOT/template/ to code_path, resolves relative submodule
    paths to absolute, substitutes the library name, and initializes a
    git repo.

    Args:
        code_path: Where to create the workspace.
        lib_name: Name for the Lean library (becomes the source directory
            and root import file, e.g. ``"BooleanFourier"`` →
            ``BooleanFourier/`` + ``BooleanFourier.lean``).
        ignored_patterns: Glob patterns to exclude when copying the template.

    Raises:
        FileExistsError: If code_path already exists.
        FileNotFoundError: If template is missing.
    """
    code_path = code_path.resolve()
    template_path = REPO_ROOT / "template"

    if code_path.exists():
        raise FileExistsError(f"Workspace already exists: {code_path}")
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    logger.info("Initializing Workspace at %s", code_path)

    # Copy template (exclude .lake — packages will be resolved fresh by lake build)
    ignore = shutil.ignore_patterns(*ignored_patterns) if ignored_patterns else None
    shutil.copytree(template_path, code_path, ignore=ignore)

    # Resolve relative paths to absolute
    lakefile = code_path / "lakefile.toml"
    if lakefile.exists():
        _resolve_relative_paths(lakefile, template_path, keys=["path"])
        # Substitute library name placeholder
        content = lakefile.read_text()
        lakefile.write_text(content.replace("__LIB_NAME__", lib_name))
    manifest = code_path / "lake-manifest.json"
    if manifest.exists():
        _resolve_relative_paths(manifest, template_path, keys=["dir"])
        content = manifest.read_text()
        manifest.write_text(content.replace("__LIB_NAME__", lib_name))

    # Create library source directory and root import file
    (code_path / lib_name).mkdir(exist_ok=True)
    (code_path / f"{lib_name}.lean").write_text("")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=code_path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=code_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=code_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial Workspace"],
        cwd=code_path,
        check=True,
        capture_output=True,
    )
    logger.info("Workspace ready: %s", code_path)


def initialize_workspace(
    run_path: Path,
    data_source: Path | None = None,
    data_files: list[str] | None = None,
    code_dir: str = "workspace",
    data_dir: str = "data",
    lib_name: str = "Formalization",
    ignored_patterns: Sequence[str] = (),
) -> None:
    """Initialize a workspace for one pipeline run.

    Creates the directory structure:
    - <code_dir>/  Git repo (from template)
    - <data_dir>/  Input data (books, theorem statements, etc.)

    Args:
        run_path: Root directory for this run.
        data_source: Directory of input data files to copy in.
        data_files: If given, copy only these filenames from data_source.
        code_dir: Name of the code subdirectory.
        data_dir: Name of the data subdirectory.
        lib_name: Name for the Lean library (source directory and root import).
        ignored_patterns: Glob patterns to exclude when copying the template.

    Raises:
        FileExistsError: If run_path already exists.
    """
    run_path = run_path.resolve()

    if run_path.exists():
        raise FileExistsError(f"Workspace already exists: {run_path}. Pass this path to resume an existing run.")

    copy_template(run_path / code_dir, lib_name=lib_name, ignored_patterns=ignored_patterns)

    (run_path / "skills").mkdir(exist_ok=True)
    (run_path / "traces").mkdir(exist_ok=True)
    (run_path / data_dir).mkdir(exist_ok=True)

    if data_source:
        data_source = Path(data_source).resolve()
        data_dest = run_path / data_dir
        if data_files:
            for fname in data_files:
                src = data_source / fname
                if src.exists():
                    dst = data_dest / fname
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                else:
                    logger.warning("  Data file not found: %s", src)
            logger.info("  Copied data files: %s", data_files)
        else:
            shutil.copytree(data_source, data_dest, dirs_exist_ok=True)
            logger.info("  Copied data: %s -> %s", data_source, data_dest)

    logger.info("Workspace ready: %s", run_path)


def ensure_workspace(
    run_path: Path,
    data_source: Path | None = None,
    data_files: list[str] | None = None,
    code_dir: str = "workspace",
    data_dir: str = "data",
    lib_name: str = "Formalization",
    ignored_patterns: Sequence[str] = (),
) -> None:
    """Ensure a workspace exists, creating it if necessary.

    If run_path already exists, assumes it is a valid workspace and
    returns immediately (resume mode).
    """
    if run_path.exists():
        logger.info("Resuming existing workspace: %s", run_path)
        return
    initialize_workspace(
        run_path, data_source, data_files, code_dir, data_dir, lib_name=lib_name, ignored_patterns=ignored_patterns
    )
