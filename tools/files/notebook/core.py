"""Notebook operations — read and edit Jupyter notebook cells.

No MCP dependencies.
"""

from __future__ import annotations

import json
import os

DEFAULT_MAX_OUTPUTS = 3
DEFAULT_OUTPUT_TRUNCATION = 500


class NotebookOps:
    """Jupyter notebook read/edit operations scoped to allowed directories."""

    def __init__(self, allowed_dirs: list[str]) -> None:
        self.allowed_dirs = allowed_dirs

    def _validate(self, path: str) -> str:
        resolved = os.path.realpath(path)
        for d in self.allowed_dirs:
            allowed = os.path.realpath(d)
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved
        raise PermissionError(f"Access denied — {resolved} is outside allowed directories")

    @staticmethod
    def _read_notebook(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _write_notebook(path: str, nb: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)
            f.write("\n")

    @staticmethod
    def _format_cell(cell: dict, index: int) -> str:
        cell_type = cell.get("cell_type", "unknown")
        source = "".join(cell.get("source", []))
        outputs = cell.get("outputs", [])
        lines = [f"--- Cell {index} ({cell_type}) ---"]
        lines.append(source)
        if outputs:
            lines.append(f"\n[{len(outputs)} output(s)]")
            for out in outputs[:DEFAULT_MAX_OUTPUTS]:
                if "text" in out:
                    lines.append("".join(out["text"])[:DEFAULT_OUTPUT_TRUNCATION])
                elif "data" in out:
                    for mime, data in out["data"].items():
                        if mime.startswith("text/"):
                            lines.append("".join(data)[:DEFAULT_OUTPUT_TRUNCATION])
        return "\n".join(lines)

    def read_notebook(self, path: str) -> str:
        fpath = self._validate(path)
        nb = self._read_notebook(fpath)
        cells = nb.get("cells", [])
        if not cells:
            return f"{path}: empty notebook"
        parts = [f"Notebook: {path} ({len(cells)} cells)\n"]
        for i, cell in enumerate(cells):
            parts.append(self._format_cell(cell, i))
        return "\n\n".join(parts)

    def edit_notebook_cell(
        self,
        path: str,
        cell_number: int,
        new_source: str,
        cell_type: str = "",
        edit_mode: str = "replace",
    ) -> str:
        fpath = self._validate(path)
        nb = self._read_notebook(fpath)
        cells = nb.get("cells", [])

        if edit_mode == "insert":
            if not cell_type:
                return "Error: cell_type is required for insert mode"
            new_cell = {
                "cell_type": cell_type,
                "source": new_source.splitlines(keepends=True),
                "metadata": {},
            }
            if cell_type == "code":
                new_cell["outputs"] = []
                new_cell["execution_count"] = None
            cells.insert(cell_number, new_cell)
            self._write_notebook(fpath, nb)
            return f"Inserted {cell_type} cell at position {cell_number}"

        if cell_number < 0 or cell_number >= len(cells):
            return f"Error: cell_number {cell_number} out of range (0-{len(cells) - 1})"

        if edit_mode == "delete":
            deleted = cells.pop(cell_number)
            self._write_notebook(fpath, nb)
            return f"Deleted cell {cell_number} ({deleted.get('cell_type', 'unknown')})"

        # replace
        cell = cells[cell_number]
        cell["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            cell["cell_type"] = cell_type
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        self._write_notebook(fpath, nb)
        return f"Replaced cell {cell_number}"
