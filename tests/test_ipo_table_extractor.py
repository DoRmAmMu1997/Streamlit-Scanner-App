"""IPO-010 deterministic PDF table/text extraction tests.

Beginner note:
The extractor is the first code that ever opens a cached prospectus, so its
job is to be boring and bounded: 1-based page numbers that later page
citations can be verified against, hard caps that keep a hostile PDF from
exhausting memory, and typed error codes instead of raw parser tracebacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.ipo.documents.table_extractor import (
    ExtractedPage,
    ExtractedTable,
    IpoDocumentParseError,
    extract_document_pages,
)


class _FakePage:
    """Mimic the two pdfplumber page methods the extractor consumes."""

    def __init__(self, text: str, tables: list[list[list[Any]]] | None = None) -> None:
        """Record the canned text and raw table cells for this page."""
        self._text = text
        self._tables = tables or []

    def extract_text(self, **_kwargs: Any) -> str:
        """Return the canned page text like ``pdfplumber`` would."""
        return self._text

    def extract_tables(self) -> list[list[list[Any]]]:
        """Return the canned raw tables like ``pdfplumber`` would."""
        return self._tables


class _FakePdf:
    """Mimic the ``pdfplumber.open`` context manager around fake pages."""

    def __init__(self, pages: list[_FakePage]) -> None:
        """Hold the fake page list the extractor will iterate."""
        self.pages = pages

    def __enter__(self) -> _FakePdf:
        """Enter like a real pdfplumber document handle."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Exit without suppressing exceptions, like the real handle."""


def _open_pdf_factory(pages: list[_FakePage]):
    """Build an ``open_pdf`` seam returning the given fake document."""

    def _open(_path: str) -> _FakePdf:
        """Ignore the path and hand back the canned fake document."""
        return _FakePdf(pages)

    return _open


def test_pages_are_numbered_from_one_with_text_and_tables(tmp_path: Path) -> None:
    """Page numbers are the provenance anchor; they must be 1-based and dense."""
    pages = [
        _FakePage("RISK FACTORS\nThis issue involves risks."),
        _FakePage(
            "RESTATED FINANCIAL INFORMATION",
            tables=[[["Particulars", "FY26"], ["Revenue", "1,234.50"]]],
        ),
    ]

    extracted = extract_document_pages(
        tmp_path / "doc.pdf", open_pdf=_open_pdf_factory(pages)
    )

    assert [page.page_number for page in extracted] == [1, 2]
    assert "RISK FACTORS" in extracted[0].text
    assert extracted[0].tables == ()
    assert extracted[1].tables == (
        ExtractedTable(page_number=2, rows=(("Particulars", "FY26"), ("Revenue", "1,234.50"))),
    )


def test_hostile_pdf_caps_bound_cells_text_and_table_count(tmp_path: Path) -> None:
    """Oversized content is truncated, never loaded unbounded into memory."""
    huge_cell = "9" * 1000
    many_tables = [[[huge_cell]] for _ in range(50)]
    pages = [_FakePage("x" * 100_000, tables=many_tables)]

    extracted = extract_document_pages(
        tmp_path / "doc.pdf", open_pdf=_open_pdf_factory(pages)
    )

    page = extracted[0]
    assert len(page.text) == 20_000
    assert len(page.tables) == 20
    assert all(len(cell) <= 200 for table in page.tables for row in table.rows for cell in row)


def test_none_cells_become_empty_strings(tmp_path: Path) -> None:
    """pdfplumber emits ``None`` for merged cells; storage wants strings."""
    pages = [_FakePage("text", tables=[[["Revenue", None], [None, "1,234.50"]]])]

    extracted = extract_document_pages(
        tmp_path / "doc.pdf", open_pdf=_open_pdf_factory(pages)
    )

    assert extracted[0].tables[0].rows == (("Revenue", ""), ("", "1,234.50"))


def test_too_many_pages_fails_closed(tmp_path: Path) -> None:
    """A document over the page limit is rejected, not silently truncated.

    Beginner note:
        Truncating would silently invalidate page citations beyond the cut,
        so the extractor refuses instead; the caller records a typed failure.
    """
    pages = [_FakePage("p") for _ in range(5)]

    with pytest.raises(IpoDocumentParseError) as excinfo:
        extract_document_pages(
            tmp_path / "doc.pdf", max_pages=4, open_pdf=_open_pdf_factory(pages)
        )
    assert excinfo.value.code == "page_limit_exceeded"


def test_unreadable_pdf_maps_to_a_typed_code(tmp_path: Path) -> None:
    """Any parser explosion becomes one stable, secret-safe error code."""

    def _broken_open(_path: str) -> _FakePdf:
        """Simulate pdfplumber failing on corrupt bytes."""
        raise ValueError("corrupt xref table")

    with pytest.raises(IpoDocumentParseError) as excinfo:
        extract_document_pages(tmp_path / "doc.pdf", open_pdf=_broken_open)
    assert excinfo.value.code == "unreadable_pdf"


def test_document_with_no_extractable_text_fails_closed(tmp_path: Path) -> None:
    """A scanned/image-only prospectus yields no text and must be flagged."""
    pages = [_FakePage(""), _FakePage("")]

    with pytest.raises(IpoDocumentParseError) as excinfo:
        extract_document_pages(tmp_path / "doc.pdf", open_pdf=_open_pdf_factory(pages))
    assert excinfo.value.code == "empty_document"


def _escape_pdf_text(value: str) -> str:
    """Escape parentheses and backslashes for a PDF literal string."""
    return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _minimal_pdf(pages: list[list[str]]) -> bytes:
    """Assemble a tiny but structurally valid PDF with real extractable text.

    Beginner note:
        The repo has no PDF-writing dependency, so this helper builds one by
        hand: a catalog, a page tree, one content stream per page, one shared
        font, and a byte-accurate xref table. pdfminer (pdfplumber's engine)
        parses it exactly like a real prospectus, which lets the integration
        test exercise the true pdfplumber path without any binary fixture
        checked into the repository.
    """
    objects: list[bytes] = []
    page_count = len(pages)
    font_number = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(page_count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    for index, lines in enumerate(pages):
        page_number = 3 + 2 * index
        content_number = page_number + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode()
        )
        text_ops = " ".join(
            f"({_escape_pdf_text(line)}) Tj 0 -16 Td" for line in lines
        )
        stream = f"BT /F1 12 Tf 72 720 Td {text_ops} ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    body = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(body)
    xref = f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return body + xref + trailer


def test_real_pdfplumber_reads_the_generated_fixture(tmp_path: Path) -> None:
    """Integration: the default pdfplumber path extracts real page text."""
    pdf_path = tmp_path / "fixture.pdf"
    pdf_path.write_bytes(
        _minimal_pdf(
            [
                ["RISK FACTORS", "This issue involves material risks."],
                ["RESTATED CONSOLIDATED FINANCIAL INFORMATION", "Revenue 1,234.50"],
            ]
        )
    )

    extracted = extract_document_pages(pdf_path)

    assert [page.page_number for page in extracted] == [1, 2]
    assert "RISK FACTORS" in extracted[0].text
    assert "1,234.50" in extracted[1].text
    assert all(isinstance(page, ExtractedPage) for page in extracted)
