#!/usr/bin/env python3
"""Build a clean, single-manifest Autoform package for Muse/TBH."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("packaging/muse/.muse-plugin/plugin.json")
DIRECTORIES = (
    "agents",
    "assets",
    "docs",
    "examples",
    "hooks",
    "internal",
    "scripts",
    "servers",
    "skills",
    "templates",
)
FILES = (
    "CONTRIBUTING.md",
    "LICENSE",
    "QUICKSTART.md",
    "README.md",
    "SETUP.md",
    "pyproject.toml",
    "uv.lock",
)
IGNORED_NAMES = {
    ".DS_Store",
    ".git",
    ".lake",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in IGNORED_NAMES or name.endswith((".pyc", ".pyo"))
    }


def build_muse_plugin(output: Path, *, force: bool = False) -> Path:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise FileExistsError(f"output already exists: {output}; pass --force to rebuild")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for relative in DIRECTORIES:
            source = REPO_ROOT / relative
            if not source.is_dir():
                raise FileNotFoundError(f"required Muse package directory is missing: {source}")
            shutil.copytree(source, stage / relative, ignore=_ignore)

        for relative in FILES:
            source = REPO_ROOT / relative
            if not source.is_file():
                raise FileNotFoundError(f"required Muse package file is missing: {source}")
            shutil.copy2(source, stage / relative)

        manifest_source = REPO_ROOT / MANIFEST
        manifest_target = stage / ".muse-plugin" / "plugin.json"
        manifest_target.parent.mkdir(parents=True)
        shutil.copy2(manifest_source, manifest_target)

        if output.exists():
            shutil.rmtree(output)
        os.replace(stage, output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "dist" / "muse" / "autoform",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    output = build_muse_plugin(args.output, force=args.force)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
