"""IPO-010: bounded, process-isolated extraction from cached PDFs.

The parent process owns policy and provenance. A short-lived child owns the
pdfplumber parser and can be terminated when hostile document structure hangs
or exceeds a resource budget. The child emits only a bounded JSON receipt;
callers never receive live parser objects.

Beginner note:
Hash verification proves which PDF we opened, not that its internal object
graph is safe. The process boundary protects the long-lived screening job,
while the in-child object limits prevent a nominally successful parse from
returning an unbounded result.
"""

from __future__ import annotations

import enum
import json
import multiprocessing
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

MAX_PAGES_DEFAULT: Final = 800
_MAX_CELL_CHARS: Final = 200
_MAX_PAGE_TEXT_CHARS: Final = 20_000
_MAX_TABLES_PER_PAGE: Final = 20


class IpoDocumentParseError(RuntimeError):
    """Raise one stable, secret-safe parser failure to facade callers."""

    def __init__(self, code: str, message: str) -> None:
        """Store the stable code alongside a payload-free summary."""
        super().__init__(message)
        self.code = code


class PdfParseStatus(enum.StrEnum):
    """State of one bounded parse attempt."""

    SUCCESS = "success"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class PdfExtractionBudget:
    """All resource dimensions an external PDF may influence.

    The defaults are intentionally conservative for an offline prospectus
    workflow. Tests can lower one limit to exercise a boundary without
    manufacturing a destructive document.
    """

    wall_time_seconds: float = 60.0
    max_pages: int = MAX_PAGES_DEFAULT
    max_tables_per_page: int = _MAX_TABLES_PER_PAGE
    max_rows_per_table: int = 250
    max_columns_per_row: int = 50
    max_cells_per_document: int = 100_000
    max_cell_chars: int = _MAX_CELL_CHARS
    max_page_text_chars: int = _MAX_PAGE_TEXT_CHARS
    max_document_text_chars: int = 2_000_000
    max_glyphs_per_page: int = 50_000
    max_glyphs_per_document: int = 2_000_000
    max_serialized_result_bytes: int = 16 * 1024 * 1024
    linux_address_space_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        """Reject disabled or nonsensical limits before a child is launched."""
        for name, value in vars(self).items():
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive.")


@dataclass(frozen=True)
class ExtractedTable:
    """One candidate table with its page number as the provenance anchor."""

    page_number: int
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ExtractedPage:
    """One bounded page receipt, numbered from one."""

    page_number: int
    text: str
    tables: tuple[ExtractedTable, ...]


@dataclass(frozen=True)
class PdfParseReceipt:
    """Serializable outcome of one bounded parse attempt."""

    status: PdfParseStatus
    pages: tuple[ExtractedPage, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        """Keep success and review-required states mutually exclusive."""
        if self.status is PdfParseStatus.SUCCESS:
            if not self.pages or self.error_code is not None:
                raise ValueError("A successful PDF receipt needs pages and no error code.")
        elif self.pages or not self.error_code:
            raise ValueError("A review-required PDF receipt needs one error code and no pages.")


WorkerRunner = Callable[[Path, PdfExtractionBudget], bytes]


def _review(code: str) -> PdfParseReceipt:
    """Build one payload-free review receipt."""
    return PdfParseReceipt(status=PdfParseStatus.REVIEW_REQUIRED, error_code=code)


def _default_open_pdf(path: str) -> Any:
    """Open one local PDF with pdfplumber, importing it only in the parser process."""
    import pdfplumber  # type: ignore[import-untyped, unused-ignore]

    return pdfplumber.open(path)


def _raw_tables(page: Any, budget: PdfExtractionBudget) -> list[Any]:
    """Extract only the selected table objects when the parser exposes that seam."""
    finder = getattr(page, "find_tables", None)
    if callable(finder):
        located = list(finder())
        if len(located) > budget.max_tables_per_page:
            raise IpoDocumentParseError(
                "page_table_limit_exceeded",
                "A PDF page exceeded the candidate-table limit.",
            )
        return [table.extract() for table in located]

    extracted = list(page.extract_tables())
    if len(extracted) > budget.max_tables_per_page:
        raise IpoDocumentParseError(
            "page_table_limit_exceeded",
            "A PDF page exceeded the candidate-table limit.",
        )
    return extracted


def _bounded_tables(
    page: Any,
    page_number: int,
    budget: PdfExtractionBudget,
    *,
    cells_seen: int,
) -> tuple[tuple[ExtractedTable, ...], int]:
    """Normalize candidate tables while enforcing every retained dimension."""
    tables: list[ExtractedTable] = []
    for raw_table in _raw_tables(page, budget):
        if len(raw_table) > budget.max_rows_per_table:
            raise IpoDocumentParseError(
                "table_row_limit_exceeded",
                "A candidate table exceeded the row limit.",
            )
        rows: list[tuple[str, ...]] = []
        for raw_row in raw_table:
            if len(raw_row) > budget.max_columns_per_row:
                raise IpoDocumentParseError(
                    "table_column_limit_exceeded",
                    "A candidate table exceeded the column limit.",
                )
            normalized: list[str] = []
            for cell in raw_row:
                text = str(cell or "").strip()
                if len(text) > budget.max_cell_chars:
                    raise IpoDocumentParseError(
                        "cell_text_limit_exceeded",
                        "A candidate-table cell exceeded the text limit.",
                    )
                cells_seen += 1
                if cells_seen > budget.max_cells_per_document:
                    raise IpoDocumentParseError(
                        "document_cell_limit_exceeded",
                        "The PDF exceeded the document cell limit.",
                    )
                normalized.append(text)
            rows.append(tuple(normalized))
        tables.append(ExtractedTable(page_number=page_number, rows=tuple(rows)))
    return tuple(tables), cells_seen


def _extract_in_process(
    pdf_path: Path,
    budget: PdfExtractionBudget,
    *,
    open_pdf: Callable[[str], Any] | None = None,
) -> PdfParseReceipt:
    """Run pdfplumber under explicit object limits and return a typed receipt."""
    opener = open_pdf if open_pdf is not None else _default_open_pdf
    pages: list[ExtractedPage] = []
    cells_seen = 0
    text_seen = 0
    glyphs_seen = 0
    try:
        with opener(str(pdf_path)) as pdf:
            pdf_pages = pdf.pages
            if len(pdf_pages) > budget.max_pages:
                raise IpoDocumentParseError(
                    "page_limit_exceeded",
                    "The PDF exceeded the page limit.",
                )
            for index, page in enumerate(pdf_pages, start=1):
                page_glyphs = getattr(page, "chars", ())
                glyph_count = len(page_glyphs)
                if glyph_count > budget.max_glyphs_per_page:
                    raise IpoDocumentParseError(
                        "page_glyph_limit_exceeded",
                        "A PDF page exceeded the glyph limit.",
                    )
                glyphs_seen += glyph_count
                if glyphs_seen > budget.max_glyphs_per_document:
                    raise IpoDocumentParseError(
                        "document_glyph_limit_exceeded",
                        "The PDF exceeded the document glyph limit.",
                    )

                text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if len(text) > budget.max_page_text_chars:
                    raise IpoDocumentParseError(
                        "page_text_limit_exceeded",
                        "A PDF page exceeded the text limit.",
                    )
                text_seen += len(text)
                if text_seen > budget.max_document_text_chars:
                    raise IpoDocumentParseError(
                        "document_text_limit_exceeded",
                        "The PDF exceeded the document text limit.",
                    )
                tables, cells_seen = _bounded_tables(
                    page,
                    index,
                    budget,
                    cells_seen=cells_seen,
                )
                pages.append(
                    ExtractedPage(page_number=index, text=text, tables=tables)
                )
    except IpoDocumentParseError as exc:
        return _review(exc.code)
    except Exception:  # noqa: BLE001 - parser messages may contain hostile content
        return _review("unreadable_pdf")

    if not pages or all(not page.text.strip() for page in pages):
        return _review("empty_document")
    return PdfParseReceipt(status=PdfParseStatus.SUCCESS, pages=tuple(pages))


def _receipt_to_bytes(receipt: PdfParseReceipt) -> bytes:
    """Encode one bounded child result as plain JSON rather than pickle."""
    payload = {
        "status": receipt.status.value,
        "error_code": receipt.error_code,
        "pages": [
            {
                "page_number": page.page_number,
                "text": page.text,
                "tables": [
                    {"page_number": table.page_number, "rows": table.rows}
                    for table in page.tables
                ],
            }
            for page in receipt.pages
        ],
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _receipt_from_bytes(data: bytes) -> PdfParseReceipt:
    """Strictly rebuild the parent-owned result from a child JSON message."""
    try:
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError
        status = PdfParseStatus(payload["status"])
        error_code = payload.get("error_code")
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, list):
            raise TypeError
        pages: list[ExtractedPage] = []
        for raw_page in raw_pages:
            if not isinstance(raw_page, dict):
                raise TypeError
            raw_tables = raw_page["tables"]
            if not isinstance(raw_tables, list):
                raise TypeError
            tables = tuple(
                ExtractedTable(
                    page_number=int(raw_table["page_number"]),
                    rows=tuple(
                        tuple(str(cell) for cell in row)
                        for row in raw_table["rows"]
                    ),
                )
                for raw_table in raw_tables
            )
            pages.append(
                ExtractedPage(
                    page_number=int(raw_page["page_number"]),
                    text=str(raw_page["text"]),
                    tables=tables,
                )
            )
        return PdfParseReceipt(
            status=status,
            pages=tuple(pages),
            error_code=str(error_code) if error_code is not None else None,
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The PDF worker returned a malformed result.") from exc


def _receipt_fits_budget(
    receipt: PdfParseReceipt,
    budget: PdfExtractionBudget,
) -> bool:
    """Re-check every child-controlled object dimension in the parent."""
    if receipt.status is PdfParseStatus.REVIEW_REQUIRED:
        return True
    if len(receipt.pages) > budget.max_pages:
        return False
    if [page.page_number for page in receipt.pages] != list(
        range(1, len(receipt.pages) + 1)
    ):
        return False

    text_chars = 0
    cells = 0
    for page in receipt.pages:
        if len(page.text) > budget.max_page_text_chars:
            return False
        text_chars += len(page.text)
        if text_chars > budget.max_document_text_chars:
            return False
        if len(page.tables) > budget.max_tables_per_page:
            return False
        for table in page.tables:
            if table.page_number != page.page_number:
                return False
            if len(table.rows) > budget.max_rows_per_table:
                return False
            for row in table.rows:
                if len(row) > budget.max_columns_per_row:
                    return False
                cells += len(row)
                if cells > budget.max_cells_per_document:
                    return False
                if any(len(cell) > budget.max_cell_chars for cell in row):
                    return False
    return any(page.text.strip() for page in receipt.pages)


def _apply_linux_memory_limit(budget: PdfExtractionBudget) -> None:
    """Apply the ADR's child-only address-space limit where Python supports it."""
    if sys.platform != "linux":
        return
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS,
        (budget.linux_address_space_bytes, budget.linux_address_space_bytes),
    )


def _worker_entry(
    pdf_path: str,
    budget: PdfExtractionBudget,
    send_connection: Any,
) -> None:
    """Child entrypoint: contain parser state and emit one bounded byte message."""
    try:
        _apply_linux_memory_limit(budget)
        receipt = _extract_in_process(Path(pdf_path), budget)
        encoded = _receipt_to_bytes(receipt)
        if len(encoded) > budget.max_serialized_result_bytes:
            encoded = _receipt_to_bytes(_review("worker_result_limit_exceeded"))
        send_connection.send_bytes(encoded)
    finally:
        send_connection.close()


def _run_worker(pdf_path: Path, budget: PdfExtractionBudget) -> bytes:
    """Spawn one parser child and terminate it when its wall time expires."""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker_entry,
        args=(str(pdf_path), budget, send_connection),
        name="ipo-pdf-parser",
    )
    process.start()
    send_connection.close()
    deadline = time.monotonic() + budget.wall_time_seconds
    try:
        while not receive_connection.poll(0.05):
            if not process.is_alive():
                process.join()
                raise ChildProcessError
            if time.monotonic() >= deadline:
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                raise TimeoutError
        try:
            data = receive_connection.recv_bytes(
                maxlength=budget.max_serialized_result_bytes
            )
        except OSError as exc:
            raise OverflowError from exc
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.exitcode not in {0, None}:
            raise ChildProcessError
        return data
    finally:
        receive_connection.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def parse_document_pages(
    pdf_path: Path | str,
    *,
    budget: PdfExtractionBudget | None = None,
    open_pdf: Callable[[str], Any] | None = None,
    run_worker: WorkerRunner | None = None,
) -> PdfParseReceipt:
    """Return one typed bounded parse receipt.

    ``open_pdf`` is the existing deterministic parser seam used by unit tests.
    Supplying it intentionally keeps that fake in-process; production omits it
    and always uses the killable worker. ``run_worker`` tests parent failure
    mapping without starting destructive child fixtures.
    """
    active_budget = budget or PdfExtractionBudget()
    path = Path(pdf_path)
    if open_pdf is not None:
        return _extract_in_process(path, active_budget, open_pdf=open_pdf)
    worker = run_worker if run_worker is not None else _run_worker
    try:
        encoded = worker(path, active_budget)
    except TimeoutError:
        return _review("worker_timeout")
    except ChildProcessError:
        return _review("worker_crashed")
    except OverflowError:
        return _review("worker_result_limit_exceeded")
    except Exception:  # noqa: BLE001 - process boundary returns stable receipts
        return _review("worker_failed")
    if len(encoded) > active_budget.max_serialized_result_bytes:
        return _review("worker_result_limit_exceeded")
    try:
        receipt = _receipt_from_bytes(encoded)
    except ValueError:
        return _review("malformed_worker_response")
    if not _receipt_fits_budget(receipt, active_budget):
        return _review("worker_result_budget_exceeded")
    return receipt


def extract_document_pages(
    pdf_path: Path | str,
    *,
    max_pages: int | None = None,
    budget: PdfExtractionBudget | None = None,
    open_pdf: Callable[[str], Any] | None = None,
) -> tuple[ExtractedPage, ...]:
    """Compatibility facade returning pages or the existing typed exception."""
    active_budget = budget or PdfExtractionBudget()
    if max_pages is not None and active_budget.max_pages != max_pages:
        active_budget = replace(active_budget, max_pages=max_pages)
    receipt = parse_document_pages(
        pdf_path,
        budget=active_budget,
        open_pdf=open_pdf,
    )
    if receipt.status is PdfParseStatus.REVIEW_REQUIRED:
        raise IpoDocumentParseError(
            receipt.error_code or "unreadable_pdf",
            "The PDF requires human review before extraction can continue.",
        )
    return receipt.pages
