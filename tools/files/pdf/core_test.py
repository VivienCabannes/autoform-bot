"""Unit tests for PDF operations."""

from __future__ import annotations

import os


import pytest

from .core import MAX_PAGES_PER_READ, PAGE_COUNT_THRESHOLD, PdfOps, _parse_page_range, _resolve_pages


# ---------------------------------------------------------------------------
# Page range parsing
# ---------------------------------------------------------------------------


class TestParsePageRange:
    def test_single_page(self):
        assert _parse_page_range("5") == (5, 5)

    def test_range(self):
        assert _parse_page_range("1-10") == (1, 10)

    def test_open_ended(self):
        assert _parse_page_range("3-") == (3, None)

    def test_whitespace(self):
        assert _parse_page_range("  2 - 5  ") == (2, 5)

    def test_zero_page_raises(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            _parse_page_range("0")

    def test_inverted_range_raises(self):
        with pytest.raises(ValueError, match="End page .* < start page"):
            _parse_page_range("10-5")

    def test_negative_start_raises(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            _parse_page_range("-1-5")


# ---------------------------------------------------------------------------
# Page resolution
# ---------------------------------------------------------------------------


class TestResolvePages:
    def test_no_pages_small_doc(self):
        result = _resolve_pages(None, 5)
        assert result == [0, 1, 2, 3, 4]

    def test_no_pages_large_doc_raises(self):
        with pytest.raises(ValueError, match=f"> {PAGE_COUNT_THRESHOLD}"):
            _resolve_pages(None, PAGE_COUNT_THRESHOLD + 1)

    def test_no_pages_at_threshold(self):
        result = _resolve_pages(None, PAGE_COUNT_THRESHOLD)
        assert len(result) == PAGE_COUNT_THRESHOLD

    def test_single_page(self):
        assert _resolve_pages("3", 10) == [2]

    def test_range(self):
        assert _resolve_pages("2-4", 10) == [1, 2, 3]

    def test_open_ended(self):
        assert _resolve_pages("8-", 10) == [7, 8, 9]

    def test_exceeds_total_pages_raises(self):
        with pytest.raises(ValueError, match="exceeds total pages"):
            _resolve_pages("15", 10)

    def test_range_clamped_to_total(self):
        result = _resolve_pages("8-20", 10)
        assert result == [7, 8, 9]

    def test_too_many_pages_raises(self):
        with pytest.raises(ValueError, match=f"max {MAX_PAGES_PER_READ}"):
            _resolve_pages(f"1-{MAX_PAGES_PER_READ + 1}", 100)


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestPdfOpsValidation:
    def test_allowed_path(self, tmp_path):
        ops = PdfOps([str(tmp_path)])
        pdf_file = tmp_path / "test.pdf"
        pdf_file.touch()
        resolved = ops._validate(str(pdf_file))
        assert resolved == os.path.realpath(str(pdf_file))

    def test_denied_path(self, tmp_path):
        ops = PdfOps([str(tmp_path)])
        with pytest.raises(PermissionError, match="outside allowed directories"):
            ops._validate("/etc/passwd")


# ---------------------------------------------------------------------------
# Text extraction (integration — requires pypdfium2)
# ---------------------------------------------------------------------------


class TestPdfOpsReadPdf:
    @pytest.fixture
    def simple_pdf(self, tmp_path):
        """Create a minimal valid PDF with one page of text."""
        import pypdfium2 as pdfium

        pdf_path = tmp_path / "test.pdf"
        doc = pdfium.PdfDocument.new()
        page = doc.new_page(200, 200)
        page.close()
        with open(pdf_path, "wb") as f:
            doc.save(f)
        doc.close()
        return pdf_path

    def test_read_simple_pdf(self, simple_pdf, tmp_path):
        ops = PdfOps([str(tmp_path)])
        result = ops.read_pdf(str(simple_pdf))
        assert "1 pages" in result
        assert "Page 1 / 1" in result

    def test_read_nonexistent_raises(self, tmp_path):
        ops = PdfOps([str(tmp_path)])
        with pytest.raises(Exception):
            ops.read_pdf(str(tmp_path / "nonexistent.pdf"))

    def test_read_with_pages_param(self, simple_pdf, tmp_path):
        ops = PdfOps([str(tmp_path)])
        result = ops.read_pdf(str(simple_pdf), pages="1")
        assert "Page 1 / 1" in result
