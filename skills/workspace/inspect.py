#!/usr/bin/env python3
"""Inspect a Lean 4 workspace — project structure, sorry/axiom counts, declarations.

Usage:
    python3 inspect.py [path]                  # Full workspace summary
    python3 inspect.py --search "pattern" [path]  # Search .lean files
    python3 inspect.py --declarations [path]      # List declarations
    python3 inspect.py --targets [path]           # Read targets file
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

EXCLUDED_DIRS = {".git", ".lake", ".venv", "__pycache__", "node_modules", "runs", "traces"}
LEAN_DECL_RE = re.compile(
    r"^\s*(?:private\s+|protected\s+)?"
    r"(theorem|lemma|def|abbrev|axiom|constant|inductive|structure|class|instance)\s+([^\s(:]+)"
)


def resolve_workspace(path: str | None = None) -> Path:
    """Resolve a workspace root from a path, env var, or cwd."""
    if path:
        candidate = Path(path).expanduser()
    else:
        for var in ("LEAN_PROJECT_DIR", "AUTOFORM_WORKSPACE", "CLAUDE_PROJECT_DIR"):
            value = os.environ.get(var)
            if value and "$" not in value:
                candidate = Path(value).expanduser()
                break
        else:
            candidate = Path.cwd()
    if candidate.is_file():
        candidate = candidate.parent
    return candidate.resolve()


def inspect_workspace(path: str | None = None) -> dict[str, Any]:
    """Scan a Lean workspace and return a structured summary."""
    root = resolve_workspace(path)
    lakefile = _find_upwards(root, ["lakefile.toml", "lakefile.lean"])
    project_root = lakefile.parent if lakefile else root
    lean_toolchain = _find_upwards(root, ["lean-toolchain"])
    targets = _find_first(project_root, ["targets.yaml", "targets.yml", "targets.json"])
    book = _find_first(project_root, ["book.md", "book.tex"])
    lean_files = list(_iter_files(project_root, ".lean", limit=5000))
    declarations = list_lean_declarations(str(project_root), limit=200)

    toolchain_version = None
    if lean_toolchain and lean_toolchain.exists():
        toolchain_version = lean_toolchain.read_text().strip()

    return {
        "workspace": str(root),
        "project_root": str(project_root),
        "lakefile": str(lakefile) if lakefile else None,
        "lean_toolchain": toolchain_version,
        "targets_file": str(targets) if targets else None,
        "book_file": str(book) if book else None,
        "lean_file_count": len(lean_files),
        "declaration_count": len(declarations["declarations"]),
        "sorry_count": _count_pattern(project_root, "sorry"),
        "axiom_count": _count_pattern(project_root, r"^\s*axiom\s", regex=True),
        "tools_available": {
            "lake": _tool_works("lake"),
            "lean": _tool_works("lean"),
            "rg": _tool_works("rg"),
        },
    }


def list_targets(path: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Read a targets.yaml/yml/json file from a workspace."""
    root_or_file = resolve_workspace(path)
    target_path = root_or_file if root_or_file.is_file() else _find_first(
        root_or_file, ["targets.yaml", "targets.yml", "targets.json"]
    )
    if target_path is None:
        return {"targets_path": None, "targets": [], "error": "No targets file found."}

    text = target_path.read_text(encoding="utf-8")
    if target_path.suffix == ".json":
        raw_targets = json.loads(text)
    else:
        raw_targets = _parse_yaml_targets(text)

    if not isinstance(raw_targets, list):
        return {"targets_path": str(target_path), "targets": [], "error": "Targets file did not parse as a list."}

    targets = [t for t in raw_targets if isinstance(t, dict)]
    return {
        "targets_path": str(target_path),
        "count": len(targets),
        "targets": targets[:max(0, limit)],
    }


def search_lean(pattern: str, path: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Search .lean files for a literal string pattern."""
    if not pattern.strip():
        return {"matches": [], "error": "Pattern must be non-empty."}
    root = resolve_workspace(path)

    if shutil.which("rg"):
        command = [
            "rg", "--fixed-strings", "--line-number", "--no-heading",
            "--glob", "*.lean", pattern, str(root),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        lines = result.stdout.splitlines()[:max(0, limit)]
        return {
            "workspace": str(root),
            "pattern": pattern,
            "matches": [_parse_rg_line(line, root) for line in lines],
        }

    matches: list[dict[str, Any]] = []
    for file_path in _iter_files(root, ".lean", limit=5000):
        for line_number, line in enumerate(file_path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pattern in line:
                matches.append({"path": str(_relative(file_path, root)), "line": line_number, "text": line.strip()})
                if len(matches) >= limit:
                    return {"workspace": str(root), "pattern": pattern, "matches": matches}
    return {"workspace": str(root), "pattern": pattern, "matches": matches}


def list_lean_declarations(path: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List Lean declarations by lightweight source scanning."""
    root = resolve_workspace(path)
    declarations: list[dict[str, Any]] = []
    for file_path in _iter_files(root, ".lean", limit=5000):
        for line_number, line in enumerate(file_path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            match = LEAN_DECL_RE.match(line)
            if not match:
                continue
            declarations.append({
                "kind": match.group(1),
                "name": match.group(2),
                "path": str(_relative(file_path, root)),
                "line": line_number,
            })
            if len(declarations) >= limit:
                return {"workspace": str(root), "declarations": declarations}
    return {"workspace": str(root), "declarations": declarations}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tool_works(cmd: str) -> bool:
    """Return True only if ``cmd`` is on PATH *and* runs.

    A bare ``shutil.which`` is misleading for Lean: the ``elan`` shim is on PATH
    even with no toolchain configured, so ``which`` reports ``lean``/``lake`` as
    available when ``lean --version`` actually errors. Verify it runs.
    """
    if shutil.which(cmd) is None:
        return False
    try:
        result = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _count_pattern(root: Path, pattern: str, *, regex: bool = False) -> int:
    if shutil.which("rg"):
        command = ["rg", "--count-matches", "--glob", "*.lean", pattern, str(root)]
        if not regex:
            command.insert(1, "--fixed-strings")
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        count = 0
        for line in result.stdout.splitlines():
            try:
                count += int(line.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
        return count

    compiled = re.compile(pattern) if regex else None
    count = 0
    for file_path in _iter_files(root, ".lean", limit=5000):
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        count += len(compiled.findall(text)) if compiled else text.count(pattern)
    return count


def _find_first(root: Path, names: list[str]) -> Path | None:
    for name in names:
        direct = root / name
        if direct.exists():
            return direct
    for path in root.rglob("*"):
        if _skip(path):
            continue
        if path.name in names:
            return path
    return None


def _find_upwards(start: Path, names: list[str]) -> Path | None:
    for directory in [start, *start.parents]:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _iter_files(root: Path, suffix: str, *, limit: int) -> Any:
    yielded = 0
    for path in root.rglob(f"*{suffix}"):
        if _skip(path):
            continue
        yielded += 1
        yield path
        if yielded >= limit:
            return


def _skip(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def _relative(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return path


def _parse_yaml_targets(text: str) -> list[dict[str, Any]]:
    try:
        import yaml
        parsed = yaml.safe_load(text)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        items: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                if current:
                    items.append(current)
                current = {}
                _assign_yaml_field(current, stripped[2:])
            elif current is not None and ":" in stripped:
                _assign_yaml_field(current, stripped)
        if current:
            items.append(current)
        return items


def _assign_yaml_field(target: dict[str, Any], text: str) -> None:
    if ":" not in text:
        return
    key, value = text.split(":", 1)
    key, value = key.strip(), value.strip()
    if not key:
        return
    target[key] = _coerce_scalar(value)


def _coerce_scalar(value: str) -> Any:
    """Coerce a YAML scalar from the fallback parser to a Python value.

    Handles the cases the targets schema actually uses without PyYAML:
    inline flow lists (``[a, b]``), booleans, null, and quoted/plain strings.
    """
    # Quoted string — strip quotes, keep verbatim.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    # Inline flow list: [], [a], [a, b]
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(item.strip()) for item in inner.split(",") if item.strip()]
    lowered = value.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "~", ""}:
        return None
    return value


def _parse_rg_line(line: str, root: Path) -> dict[str, Any]:
    parts = line.split(":", 2)
    path = parts[0] if parts else ""
    try:
        line_number = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        line_number = None
    text = parts[2].strip() if len(parts) > 2 else ""
    return {"path": str(_relative(Path(path), root)), "line": line_number, "text": text}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect a Lean 4 workspace")
    parser.add_argument("path", nargs="?", default=None, help="Workspace path (default: $LEAN_PROJECT_DIR or cwd)")
    parser.add_argument("--search", metavar="PATTERN", help="Search .lean files for a pattern")
    parser.add_argument("--declarations", action="store_true", help="List Lean declarations")
    parser.add_argument("--targets", action="store_true", help="Read targets file")
    args = parser.parse_args()

    if args.search:
        result = search_lean(args.search, args.path)
    elif args.declarations:
        result = list_lean_declarations(args.path)
    elif args.targets:
        result = list_targets(args.path)
    else:
        result = inspect_workspace(args.path)

    print(json.dumps(result, indent=2))
