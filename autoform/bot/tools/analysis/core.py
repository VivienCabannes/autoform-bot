# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Core analysis functions for Lean codebases."""

from __future__ import annotations

from pathlib import Path


def find_sorries(code_path: Path) -> list[str]:
    """Search all Lean files under *code_path* for uses of ``sorry``.

    Skips worktree and ``.lake`` directories. Returns a list of
    ``"rel/path.lean:line: text"`` strings, or an empty list when the
    codebase is sorry-free.
    """
    skip_dirs = {"worktrees", ".lake"}
    results = []
    for lean_file in sorted(code_path.rglob("*.lean")):
        if skip_dirs & set(lean_file.parts):
            continue
        for i, line in enumerate(lean_file.read_text(errors="replace").splitlines(), 1):
            if "sorry" in line:
                rel = lean_file.relative_to(code_path)
                results.append(f"{rel}:{i}: {line.strip()}")
    return results
