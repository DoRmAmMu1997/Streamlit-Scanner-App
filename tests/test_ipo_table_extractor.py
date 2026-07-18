"""IPO-010 deterministic PDF table/text extraction tests.

Beginner note:
The extractor is the first code that ever opens a cached prospectus, so its
job is to be boring and bounded: 1-based page numbers that later page
citations can be verified against, hard caps that keep a hostile PDF from
exhausting memory, and typed error codes instead of raw parser tracebacks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.ipo.documents.table_extractor import (
    ExtractedPage,
    ExtractedTable,
    IpoDocumentParseError,
    PdfExtractionBudget,
    PdfParseStatus,
    extract_document_pages,
    parse_document_pages,
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


def test_hostile_pdf_text_limit_returns_review_receipt(tmp_path: Path) -> None:
    """Oversized page text is rejected, never silently treated as complete."""
    huge_cell = "9" * 1000
    many_tables = [[[huge_cell]] for _ in range(50)]
    pages = [_FakePage("x" * 100_000, tables=many_tables)]

    receipt = parse_document_pages(
        tmp_path / "doc.pdf",
        budget=PdfExtractionBudget(max_page_text_chars=20_000),
        open_pdf=_open_pdf_factory(pages),
    )

    assert receipt.status is PdfParseStatus.REVIEW_REQUIRED
    assert receipt.error_code == "page_text_limit_exceeded"
    assert receipt.pages == ()


def test_hostile_pdf_table_shape_limits_fail_closed(tmp_path: Path) -> None:
    """Rows, columns, cells, and cell length are all independently bounded."""
    pages = [_FakePage("text", tables=[[["x" * 201]]])]

    receipt = parse_document_pages(
        tmp_path / "doc.pdf",
        budget=PdfExtractionBudget(max_cell_chars=200),
        open_pdf=_open_pdf_factory(pages),
    )

    assert receipt.status is PdfParseStatus.REVIEW_REQUIRED
    assert receipt.error_code == "cell_text_limit_exceeded"

    many_rows = [[["1"] for _ in range(251)]]
    row_receipt = parse_document_pages(
        tmp_path / "doc.pdf",
        budget=PdfExtractionBudget(max_rows_per_table=250),
        open_pdf=_open_pdf_factory([_FakePage("text", tables=many_rows)]),
    )
    assert row_receipt.error_code == "table_row_limit_exceeded"

    many_columns = [[["1" for _ in range(51)]]]
    column_receipt = parse_document_pages(
        tmp_path / "doc.pdf",
        budget=PdfExtractionBudget(max_columns_per_row=50),
        open_pdf=_open_pdf_factory([_FakePage("text", tables=many_columns)]),
    )
    assert column_receipt.error_code == "table_column_limit_exceeded"

    two_cells = [[["1", "2"]]]
    total_receipt = parse_document_pages(
        tmp_path / "doc.pdf",
        budget=PdfExtractionBudget(max_cells_per_document=1),
        open_pdf=_open_pdf_factory([_FakePage("text", tables=two_cells)]),
    )
    assert total_receipt.error_code == "document_cell_limit_exceeded"


def test_worker_failures_and_oversized_wire_results_are_typed(tmp_path: Path) -> None:
    """The parent maps timeout/crash/wire failures to review-safe receipts."""
    path = tmp_path / "doc.pdf"

    def _timeout(_path: Path, _budget: PdfExtractionBudget) -> bytes:
        """Simulate the parent terminating a child that missed its deadline."""
        raise TimeoutError

    timeout = parse_document_pages(path, run_worker=_timeout)
    assert timeout.status is PdfParseStatus.REVIEW_REQUIRED
    assert timeout.error_code == "worker_timeout"

    def _crash(_path: Path, _budget: PdfExtractionBudget) -> bytes:
        """Simulate a child process exiting without a result."""
        raise ChildProcessError

    crash = parse_document_pages(path, run_worker=_crash)
    assert crash.error_code == "worker_crashed"

    malformed = parse_document_pages(
        path, run_worker=lambda _path, _budget: b"{not-json"
    )
    assert malformed.error_code == "malformed_worker_response"

    oversized = parse_document_pages(
        path,
        budget=PdfExtractionBudget(max_serialized_result_bytes=10),
        run_worker=lambda _path, _budget: b"x" * 11,
    )
    assert oversized.error_code == "worker_result_limit_exceeded"


def test_parent_revalidates_worker_object_budgets(tmp_path: Path) -> None:
    """A compromised/mismatched child cannot return objects beyond policy."""
    oversized_success = json.dumps(
        {
            "status": "success",
            "error_code": None,
            "pages": [
                {"page_number": 1, "text": "one", "tables": []},
                {"page_number": 2, "text": "two", "tables": []},
            ],
        }
    ).encode()

    receipt = parse_document_pages(
        tmp_path / "doc.pdf",
        budget=PdfExtractionBudget(max_pages=1),
        run_worker=lambda _path, _budget: oversized_success,
    )

    assert receipt.status is PdfParseStatus.REVIEW_REQUIRED
    assert receipt.error_code == "worker_result_budget_exceeded"


def test_custom_budget_is_not_overridden_by_facade_default(tmp_path: Path) -> None:
    """Passing a complete budget keeps every caller-selected limit intact."""
    pages = [_FakePage("one"), _FakePage("two")]

    with pytest.raises(IpoDocumentParseError) as excinfo:
        extract_document_pages(
            tmp_path / "doc.pdf",
            budget=PdfExtractionBudget(max_pages=1),
            open_pdf=_open_pdf_factory(pages),
        )

    assert excinfo.value.code == "page_limit_exceeded"


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
    """Integration: the default spawn worker extracts real page text."""
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
