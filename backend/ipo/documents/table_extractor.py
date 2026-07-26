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
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from backend.ipo_pdf_worker import extract_payload, worker_entry

MAX_PAGES_DEFAULT: Final = 800
_MAX_CELL_CHARS: Final = 200
_MAX_PAGE_TEXT_CHARS: Final = 20_000
_MAX_TABLES_PER_PAGE: Final = 20


class IpoDocumentParseError(RuntimeError):
    """Raise one stable, secret-safe parser failure to facade callers.

    Beginner note:
        Parser exceptions may echo hostile PDF text or local cache paths. The
        public facade therefore exposes a small machine-readable ``code`` and a
        fixed human message instead of forwarding the original exception.
    """

    def __init__(self, code: str, message: str) -> None:
        """Store the stable code alongside a payload-free summary."""
        super().__init__(message)
        self.code = code


class PdfParseStatus(enum.StrEnum):
    """State of one bounded parse attempt.

    Beginner note:
        A parse is deliberately all-or-review: callers either receive a
        complete bounded page set or a reason to send the document to a human.
        There is no "partially trusted" page collection that could later be
        mistaken for a complete prospectus.
    """

    SUCCESS = "success"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class PdfExtractionBudget:
    """All resource dimensions an external PDF may influence.

    The defaults are intentionally conservative for an offline prospectus
    workflow. Tests can lower one limit to exercise a boundary without
    manufacturing a destructive document.

    Beginner note:
        A time limit alone is not enough for hostile documents. A parser can
        finish quickly while creating millions of cells or characters, so each
        attacker-controlled dimension has its own explicit ceiling.
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
    """One candidate table with its page number as the provenance anchor.

    Beginner note:
        Rows are immutable tuples because later evidence verification must
        compare against exactly what the bounded parser returned. Keeping the
        page beside the rows prevents a table from losing its citation context.
    """

    page_number: int
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ExtractedPage:
    """One bounded page receipt, numbered from one.

    Beginner note:
        Prospectuses and human reviewers use 1-based page citations. The parser
        preserves that convention at the boundary so downstream code never has
        to guess whether a citation needs an offset.
    """

    page_number: int
    text: str
    tables: tuple[ExtractedTable, ...]


@dataclass(frozen=True)
class PdfParseReceipt:
    """Serializable outcome of one bounded parse attempt.

    Beginner note:
        This receipt is the only information the long-lived parent accepts from
        the short-lived parser. Success contains pages and no error; review
        contains one safe code and no pages. The invariant blocks accidental
        use of truncated output.
    """

    status: PdfParseStatus
    pages: tuple[ExtractedPage, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        """Keep success and review-required states mutually exclusive.

        Raises:
            ValueError: If a success lacks pages or any failure carries pages.
        """
        if self.status is PdfParseStatus.SUCCESS:
            if not self.pages or self.error_code is not None:
                raise ValueError("A successful PDF receipt needs pages and no error code.")
        elif self.pages or not self.error_code:
            raise ValueError("A review-required PDF receipt needs one error code and no pages.")


WorkerRunner = Callable[[Path, PdfExtractionBudget], bytes]


def _review(code: str) -> PdfParseReceipt:
    """Build one payload-free review receipt.

    The helper is intentionally unable to accept raw parser text. That makes
    secret-safe, prompt-safe failure reporting the easiest call-site behavior.
    """
    return PdfParseReceipt(status=PdfParseStatus.REVIEW_REQUIRED, error_code=code)


def _extract_in_process(
    pdf_path: Path,
    budget: PdfExtractionBudget,
    *,
    open_pdf: Callable[[str], Any] | None = None,
) -> PdfParseReceipt:
    """Run the shared parser primitive through an injected in-process seam.

    Beginner note:
        Production parsing always uses a child process. Unit tests inject a fake
        ``open_pdf`` implementation here so they can exercise every object
        limit deterministically without launching a process or parsing hostile
        binary fixtures.
    """
    payload = extract_payload(
        str(pdf_path),
        vars(budget),
        open_pdf=open_pdf,
    )
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _receipt_from_bytes(encoded)


def _receipt_from_bytes(data: bytes) -> PdfParseReceipt:
    """Strictly rebuild the parent-owned result from a child JSON message.

    Beginner note:
        A process boundary is also a trust boundary. Even though our own worker
        produced the bytes, the parent treats the message as untrusted: JSON is
        decoded into fresh immutable domain objects instead of unpickling
        executable Python objects.

    Raises:
        ValueError: If the worker response is not valid UTF-8 JSON or does not
            match the expected receipt shape.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError
        status = PdfParseStatus(payload["status"])
        error_code = payload.get("error_code")
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, list):
            raise TypeError
        # Reconstruct every nested value explicitly. This keeps the worker from
        # smuggling arbitrary object types across the process boundary.
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
    """Re-check every child-controlled object dimension in the parent.

    Beginner note:
        Limits are enforced twice on purpose. The worker stops expensive parser
        work early; this parent-side pass ensures a buggy, stale, or compromised
        worker cannot return an oversized result that bypasses policy.
    """
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


def _run_worker(pdf_path: Path, budget: PdfExtractionBudget) -> bytes:
    """Spawn one parser child and return its bounded serialized receipt.

    Args:
        pdf_path: Verified local cache path. The child never chooses this path.
        budget: Parent-owned limits copied into primitive spawn arguments.

    Returns:
        Raw JSON bytes emitted by the worker.

    Raises:
        TimeoutError: If the worker exceeds its wall-time budget.
        ChildProcessError: If the worker exits abnormally or cannot finish
            cleanup.
        OverflowError: If the pipe message exceeds the serialized-result limit.

    Beginner note:
        ``spawn`` starts a fresh interpreter on every platform. That is slower
        than ``fork`` but avoids inheriting parser state and matches Windows,
        which makes timeout and cleanup behavior consistent in production.
    """
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=worker_entry,
        args=(str(pdf_path), vars(budget), send_connection),
        name="ipo-pdf-parser",
    )
    process.start()
    # Only the child should own the sending endpoint after start. Closing the
    # parent's duplicate lets EOF/crash detection work instead of waiting for a
    # writer that the parent accidentally kept alive.
    send_connection.close()
    deadline = time.monotonic() + budget.wall_time_seconds
    try:
        while not receive_connection.poll(0.05):
            if not process.is_alive():
                process.join()
                raise ChildProcessError
            if time.monotonic() >= deadline:
                # Terminate first so normal process cleanup can run. ``kill`` is
                # the last resort for a parser that ignores termination.
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

    Beginner note:
        This is the non-raising API used by batch orchestration. A damaged,
        scanned, timed-out, or oversized prospectus becomes a review receipt,
        allowing other IPOs in the job to continue without treating the failed
        document as successfully extracted.
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
    """Return complete extracted pages or raise the legacy typed exception.

    Args:
        pdf_path: Path to a verified cached PDF.
        max_pages: Backward-compatible override for only the page limit.
        budget: Optional complete extraction budget.
        open_pdf: Test seam; production callers leave this unset.

    Returns:
        A complete immutable collection of 1-based page receipts.

    Raises:
        IpoDocumentParseError: If parsing requires human review.

    Beginner note:
        Older callers expect exceptions, while newer orchestration consumes
        receipts. This small facade preserves the old contract without
        duplicating the security policy.
    """
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
