"""IPO-010: deterministic, bounded page/table extraction from cached PDFs.

This is the parse stage that IPO-003 deliberately deferred. It opens one
already-verified, content-addressed PDF from the local cache and returns each
page's text and candidate tables with 1-based page numbers — the provenance
anchors that every later page citation is verified against. There is no AI
here and no network: pdfplumber reads local bytes, and everything else is
plain data shaping.

Beginner note:
A prospectus PDF is untrusted input even after its bytes are hash-verified,
because hostile *content* (absurdly long cells, thousands of tables, a
million pages) can exhaust memory. Every dimension is therefore capped, and
structural problems surface as one typed ``IpoDocumentParseError`` code
instead of a raw parser traceback that could leak file internals into logs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# Hostile-content resource caps, mirroring the downloader's 50 MiB byte cap
# philosophy: bound every dimension a PDF author controls.
MAX_PAGES_DEFAULT: Final = 800
_MAX_CELL_CHARS: Final = 200
_MAX_PAGE_TEXT_CHARS: Final = 20_000
_MAX_TABLES_PER_PAGE: Final = 20


class IpoDocumentParseError(RuntimeError):
    """Raised when a cached PDF cannot be parsed into bounded pages.

    Beginner note:
        ``code`` is one of three stable, secret-safe identifiers —
        ``unreadable_pdf`` (the parser failed on the bytes),
        ``page_limit_exceeded`` (the document is larger than the cap), and
        ``empty_document`` (no page produced any text, the classic sign of a
        scanned/image-only prospectus). Callers branch on the code and never
        need to inspect, log, or persist a parser traceback.
    """

    def __init__(self, code: str, message: str) -> None:
        """Store the stable code alongside the human-readable summary."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExtractedTable:
    """One candidate table with its page number as the provenance anchor."""

    page_number: int
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ExtractedPage:
    """One page's bounded text and candidate tables, numbered from one."""

    page_number: int
    text: str
    tables: tuple[ExtractedTable, ...]


def _default_open_pdf(path: str) -> Any:
    """Open one local PDF with pdfplumber, importing it lazily.

    Beginner note:
        The lazy import mirrors ``backend/fundamentals/pdf_reader.py``: CI and
        module imports stay fast and dependency-tolerant, and tests replace
        this seam entirely with fake page objects so no real parsing runs.
    """
    import pdfplumber  # type: ignore[import-untyped, unused-ignore]

    return pdfplumber.open(path)


def _bounded_tables(page: Any, page_number: int) -> tuple[ExtractedTable, ...]:
    """Normalize one page's raw tables into capped, string-only rows."""
    tables: list[ExtractedTable] = []
    for raw_table in page.extract_tables()[:_MAX_TABLES_PER_PAGE]:
        rows = tuple(
            tuple(str(cell or "").strip()[:_MAX_CELL_CHARS] for cell in raw_row)
            for raw_row in raw_table
        )
        tables.append(ExtractedTable(page_number=page_number, rows=rows))
    return tuple(tables)


def extract_document_pages(
    pdf_path: Path | str,
    *,
    max_pages: int = MAX_PAGES_DEFAULT,
    open_pdf: Callable[[str], Any] | None = None,
) -> tuple[ExtractedPage, ...]:
    """Parse one cached PDF into bounded pages with 1-based numbering.

    Args:
        pdf_path: Local path of the hash-verified cached document.
        max_pages: Hard page cap. A longer document is rejected outright
            rather than truncated, because truncation would silently
            invalidate any page citation beyond the cut.
        open_pdf: Injectable opener returning a pdfplumber-shaped context
            manager (an object with ``.pages``); tests pass fakes.

    Returns:
        Every page in order, each with capped text and candidate tables.

    Raises:
        IpoDocumentParseError: With a stable ``code`` when the PDF cannot be
            read, exceeds the page cap, or contains no extractable text.
    """
    opener = open_pdf if open_pdf is not None else _default_open_pdf
    pages: list[ExtractedPage] = []
    try:
        with opener(str(pdf_path)) as pdf:
            if len(pdf.pages) > max_pages:
                raise IpoDocumentParseError(
                    "page_limit_exceeded",
                    f"Document has {len(pdf.pages)} pages; the cap is {max_pages}.",
                )
            for index, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text(x_tolerance=2, y_tolerance=2) or "")[
                    :_MAX_PAGE_TEXT_CHARS
                ]
                pages.append(
                    ExtractedPage(
                        page_number=index,
                        text=text,
                        tables=_bounded_tables(page, index),
                    )
                )
    except IpoDocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - parsers throw odd errors on weird PDFs
        # Only the exception class name survives; parser messages can embed
        # arbitrary file content and must never reach logs or storage.
        raise IpoDocumentParseError(
            "unreadable_pdf",
            f"PDF could not be parsed ({type(exc).__name__}).",
        ) from exc

    if not pages or all(not page.text.strip() for page in pages):
        raise IpoDocumentParseError(
            "empty_document",
            "No page produced extractable text (scanned or image-only PDF).",
        )
    return tuple(pages)
