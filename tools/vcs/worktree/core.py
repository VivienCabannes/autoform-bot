"""Worktree session tracking — manages active worktrees for a session.

Wraps core/worktree.py primitives with session-level tracking.
No MCP dependencies.
"""

from __future__ import annotations

from pathlib import Path

from core.worktree import (
    cleanup_worktree,
    create_worktree,
    has_commits,
    merge_to_main,
    sync_to_main,
)


class WorktreeSession:
    """Tracks active worktrees for a session."""

    def __init__(self, repo_root: str) -> None:
        self.repo = Path(repo_root).resolve()
        self.active: dict[str, Path] = {}

    def _resolve(self, branch_name: str) -> Path | None:
        wt_path = self.active.get(branch_name)
        if not wt_path:
            wt_path = self.repo / "worktrees" / branch_name
            if not wt_path.exists():
                return None
        return wt_path

    def create(self, branch_name: str) -> str:
        try:
            wt_path = create_worktree(self.repo, branch_name)
            self.active[branch_name] = wt_path
            return f"Created worktree at {wt_path}"
        except RuntimeError as e:
            return f"Error: {e}"

    def sync(self, branch_name: str) -> str:
        wt_path = self._resolve(branch_name)
        if not wt_path:
            return f"Error: Worktree '{branch_name}' not found"
        sync_to_main(wt_path, self.repo)
        return f"Synced worktree '{branch_name}' to main"

    def check_commits(self, branch_name: str) -> str:
        wt_path = self._resolve(branch_name)
        if not wt_path:
            return f"Error: Worktree '{branch_name}' not found"
        result = has_commits(wt_path, self.repo)
        return f"Has commits: {result}"

    def merge(self, branch_name: str) -> str:
        wt_path = self._resolve(branch_name)
        if not wt_path:
            return f"Error: Worktree '{branch_name}' not found"
        success, error = merge_to_main(wt_path, self.repo)
        if success:
            return f"Successfully merged worktree '{branch_name}' to main"
        return f"Merge failed: {error}"

    def cleanup(self, branch_name: str) -> str:
        wt_path = self._resolve(branch_name)
        if not wt_path:
            wt_path = self.repo / "worktrees" / branch_name
        cleanup_worktree(wt_path, self.repo)
        self.active.pop(branch_name, None)
        return f"Cleaned up worktree '{branch_name}'"

    def list_active(self) -> str:
        if not self.active:
            return "No active worktrees in this session."
        lines = [f"  {name}: {path}" for name, path in self.active.items()]
        return "Active worktrees:\n" + "\n".join(lines)
