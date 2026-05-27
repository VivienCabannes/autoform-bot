# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Book chunking — load book files and split into overlapping pieces."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_BOOK_EXTENSIONS = {".md", ".tex"}


@dataclass(frozen=True)
class Chunk:
    """A piece of a book."""

    index: int
    text: str
    start_line: int
    end_line: int


def discover_book_files(book_dir: Path) -> list[Path]:
    """Find all markdown and TeX files in a book directory."""
    files = sorted(f for f in book_dir.iterdir() if f.is_file() and f.suffix in _BOOK_EXTENSIONS)
    if not files:
        raise FileNotFoundError(f"No .md or .tex files found in {book_dir}")
    logger.info("Discovered %d book files in %s", len(files), book_dir)
    return files


def _load_book(book_dir: Path) -> list[str]:
    """Load all book files into a single list of lines."""
    files = discover_book_files(book_dir)
    all_lines: list[str] = []
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines()
        all_lines.extend(lines)
        logger.info("Loaded %s (%d lines)", f.name, len(lines))
    logger.info("Total: %d lines from %d files", len(all_lines), len(files))
    return all_lines


def _chunk_lines(
    lines: list[str],
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """Split lines into overlapping chunks."""
    if not lines:
        return []

    chunks: list[Chunk] = []
    start = 0
    index = 0

    while start < len(lines):
        end = min(start + chunk_size, len(lines))
        text = "\n".join(lines[start:end])
        chunks.append(
            Chunk(
                index=index,
                text=text,
                start_line=start + 1,
                end_line=end,
            )
        )
        index += 1
        start += chunk_size - overlap
        if start >= len(lines):
            break

    return chunks


def chunk_all(
    book_dir: Path,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """Load all book files as one text and chunk it."""
    lines = _load_book(book_dir)
    chunks = _chunk_lines(lines, chunk_size, overlap)
    logger.info("Chunked into %d chunks (chunk_size=%d, overlap=%d)", len(chunks), chunk_size, overlap)
    return chunks
