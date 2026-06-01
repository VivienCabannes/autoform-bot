"""Scratchpad operations — scoped file storage for agent notes.

No MCP dependencies. All file operations are restricted to the
configured scratchpad directory.
"""

from __future__ import annotations

import os
from pathlib import Path


class ScratchpadOps:
    """Scoped scratchpad directory for agent notes and intermediate work."""

    def __init__(self, scratchpad_dir: str) -> None:
        self.scratchpad_dir = scratchpad_dir
        os.makedirs(scratchpad_dir, exist_ok=True)

    def _validate(self, raw_path: str) -> Path:
        """Resolve *raw_path* relative to scratchpad dir and ensure it stays within."""
        base = Path(self.scratchpad_dir).resolve()
        resolved = (base / raw_path).resolve()
        if resolved == base or str(resolved).startswith(str(base) + os.sep):
            return resolved
        raise PermissionError(f"Access denied — {resolved} is outside scratchpad: {base}")

    def read(self, path: str) -> str:
        """Read a file from the scratchpad."""
        p = self._validate(path)
        if not p.is_file():
            return f"Error: {p} does not exist"
        return p.read_text(encoding="utf-8", errors="replace")

    def write(self, path: str, content: str) -> str:
        """Write or overwrite a file in the scratchpad (creates parent dirs)."""
        p = self._validate(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {p.relative_to(Path(self.scratchpad_dir).resolve())}"

    def list_files(self) -> str:
        """List all files in the scratchpad recursively."""
        base = Path(self.scratchpad_dir).resolve()
        if not base.is_dir():
            return "(scratchpad directory does not exist)"
        files: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for fname in sorted(filenames):
                full = Path(dirpath) / fname
                files.append(str(full.relative_to(base)))
        return "\n".join(files) if files else "(empty)"

    def delete(self, path: str) -> str:
        """Delete a file from the scratchpad."""
        p = self._validate(path)
        if not p.is_file():
            return f"Error: {p} does not exist"
        p.unlink()
        return f"Deleted {p.relative_to(Path(self.scratchpad_dir).resolve())}"
