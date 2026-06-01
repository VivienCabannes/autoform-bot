"""PDF operations — read and extract text from PDF files.

No MCP dependencies. Uses pypdfium2 for text extraction.
"""

from __future__ import annotations

import os

import pypdfium2 as pdfium

MAX_PAGES_PER_READ = 20
PAGE_COUNT_THRESHOLD = 10


class PdfOps:
    """PDF text extraction scoped to allowed directories."""

    def __init__(self, allowed_dirs: list[str]) -> None:
        self.allowed_dirs = allowed_dirs

    def _validate(self, path: str) -> str:
        resolved = os.path.realpath(path)
        for d in self.allowed_dirs:
            allowed = os.path.realpath(d)
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved
        raise PermissionError(f"Access denied — {resolved} is outside allowed directories")

    def read_pdf(self, path: str, pages: str | None = None) -> str:
        """Extract text from a PDF file.

        Args:
            path: Path to the PDF file.
            pages: Optional page range — "5" (single), "1-10" (range),
                   or "3-" (open-ended). 1-indexed.

        Returns:
            Formatted text with metadata header and per-page content.
        """
        fpath = self._validate(path)

        doc = pdfium.PdfDocument(fpath)
        try:
            total_pages = len(doc)

            if total_pages == 0:
                return f"{path}: empty PDF (0 pages)"

            page_indices = _resolve_pages(pages, total_pages)

            parts = [f"PDF: {path} ({total_pages} pages, reading {len(page_indices)})\n"]
            for idx in page_indices:
                page = doc[idx]
                textpage = page.get_textpage()
                text = textpage.get_text_bounded().strip()
                parts.append(f"--- Page {idx + 1} / {total_pages} ---\n{text}")
                textpage.close()
                page.close()

            return "\n\n".join(parts)
        finally:
            doc.close()


def _parse_page_range(pages: str) -> tuple[int, int | None]:
    """Parse a page range string into (first, last) 1-indexed bounds.

    Supports: "5" -> (5, 5), "1-10" -> (1, 10), "3-" -> (3, None).

    Raises:
        ValueError: On invalid format.
    """
    pages = pages.strip()

    if pages.startswith("-"):
        raise ValueError(f"Start page must be >= 1, got {pages}")

    if "-" not in pages:
        n = int(pages)
        if n < 1:
            raise ValueError(f"Page number must be >= 1, got {n}")
        return (n, n)

    left, right = pages.split("-", 1)
    first = int(left)
    if first < 1:
        raise ValueError(f"Start page must be >= 1, got {first}")

    if right.strip() == "":
        return (first, None)

    last = int(right)
    if last < first:
        raise ValueError(f"End page {last} < start page {first}")
    return (first, last)


def _resolve_pages(pages: str | None, total_pages: int) -> list[int]:
    """Convert a page range string to a list of 0-indexed page indices.

    Raises:
        ValueError: On invalid range or limit violations.
    """
    if pages is None:
        if total_pages > PAGE_COUNT_THRESHOLD:
            raise ValueError(
                f"PDF has {total_pages} pages (> {PAGE_COUNT_THRESHOLD}). "
                f'Specify a page range with the pages parameter (e.g. pages="1-{PAGE_COUNT_THRESHOLD}").'
            )
        return list(range(total_pages))

    first, last = _parse_page_range(pages)

    if first > total_pages:
        raise ValueError(f"Start page {first} exceeds total pages ({total_pages})")

    if last is None:
        last = total_pages
    last = min(last, total_pages)

    count = last - first + 1
    if count > MAX_PAGES_PER_READ:
        raise ValueError(
            f"Requested {count} pages (max {MAX_PAGES_PER_READ}). "
            f'Narrow the range, e.g. pages="{first}-{first + MAX_PAGES_PER_READ - 1}".'
        )

    return list(range(first - 1, last))
