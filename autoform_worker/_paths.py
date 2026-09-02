"""Shared worktree path classifications."""

from __future__ import annotations


GENERATED_DIRECTORY_NAMES = frozenset(
    {".git", ".hg", ".lake", ".sl", ".venv", "__pycache__", "build", "lake-packages"}
)


__all__ = ["GENERATED_DIRECTORY_NAMES"]
