"""Lightweight spawned-process implementation for bounded IPO PDF parsing.

This module intentionally lives directly below ``backend``. A spawned child
imports the target function's module before it can apply resource limits; using
``backend.ipo.documents.table_extractor`` as that target would first execute the
broad ``backend.ipo`` re-export facade and load unrelated data-science modules.

Beginner note:
    Keep this module dependency-light at import time. In particular, pdfplumber
    is imported only after the Linux address-space limit is active. The parent
    process owns the public typed receipt and revalidates every returned bound.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from typing import Any


class _WorkerParseError(RuntimeError):
    """Carry one stable code without retaining hostile parser text.

    This exception never crosses the process boundary. It is converted into a
    primitive review payload inside the child, keeping parser-controlled text
    out of logs, persistence, and parent exceptions.
    """

    def __init__(self, code: str) -> None:
        """Store only the parent-safe failure code."""
        super().__init__(code)
        self.code = code


def _review_payload(code: str) -> dict[str, Any]:
    """Return one payload-free review-required result.

    Beginner note:
        The worker intentionally drops partial pages when any limit fails.
        Returning only a stable code prevents downstream code from treating
        truncated extraction as complete evidence.
    """
    return {"status": "review_required", "error_code": code, "pages": []}


def _default_open_pdf(path: str) -> Any:
    """Import and open pdfplumber only inside the resource-limited child."""
    import pdfplumber  # type: ignore[import-untyped, unused-ignore]

    return pdfplumber.open(path)


def _limit(budget: Mapping[str, int | float], name: str) -> int:
    """Read one positive integral object limit from the parent-owned budget."""
    return int(budget[name])


def _raw_tables(
    page: Any,
    budget: Mapping[str, int | float],
) -> list[Any]:
    """Extract only the selected table objects when the parser exposes that seam.

    ``find_tables`` lets us count candidates before expanding every table into
    rows. Older/test-compatible page objects expose only ``extract_tables``;
    that fallback is still checked immediately after extraction.

    Beginner note:
        Counting table objects before materializing their rows is the earliest
        containment point supported by current ``pdfplumber``. The fallback
        exists for compatibility, but its output is rejected before cells are
        normalized or serialized.
    """
    maximum = _limit(budget, "max_tables_per_page")
    finder = getattr(page, "find_tables", None)
    if callable(finder):
        located = list(finder())
        if len(located) > maximum:
            raise _WorkerParseError("page_table_limit_exceeded")
        return [table.extract() for table in located]

    extracted = list(page.extract_tables())
    if len(extracted) > maximum:
        raise _WorkerParseError("page_table_limit_exceeded")
    return extracted


def _bounded_tables(
    page: Any,
    page_number: int,
    budget: Mapping[str, int | float],
    *,
    cells_seen: int,
) -> tuple[list[dict[str, Any]], int]:
    """Normalize candidate tables while enforcing every retained dimension.

    Beginner note:
        Per-table row and column limits stop one pathological table, while the
        cumulative cell counter stops a document from distributing excessive
        work across many individually valid tables.
    """
    tables: list[dict[str, Any]] = []
    for raw_table in _raw_tables(page, budget):
        if len(raw_table) > _limit(budget, "max_rows_per_table"):
            raise _WorkerParseError("table_row_limit_exceeded")
        rows: list[tuple[str, ...]] = []
        for raw_row in raw_table:
            if len(raw_row) > _limit(budget, "max_columns_per_row"):
                raise _WorkerParseError("table_column_limit_exceeded")
            normalized: list[str] = []
            for cell in raw_row:
                text = str(cell or "").strip()
                if len(text) > _limit(budget, "max_cell_chars"):
                    raise _WorkerParseError("cell_text_limit_exceeded")
                cells_seen += 1
                if cells_seen > _limit(budget, "max_cells_per_document"):
                    raise _WorkerParseError("document_cell_limit_exceeded")
                normalized.append(text)
            rows.append(tuple(normalized))
        tables.append({"page_number": page_number, "rows": rows})
    return tables, cells_seen


def extract_payload(
    pdf_path: str,
    budget: Mapping[str, int | float],
    *,
    open_pdf: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Parse one PDF into a bounded primitive payload.

    ``open_pdf`` preserves the deterministic in-process seam used by unit tests.
    Production leaves it unset so pdfplumber is imported after resource policy
    is active in :func:`worker_entry`.

    Args:
        pdf_path: Parent-selected path to the verified cached PDF.
        budget: Primitive copy of the parent-owned resource budget.
        open_pdf: Optional deterministic parser seam for unit tests.

    Returns:
        A JSON-compatible success payload containing bounded pages, or a
        review-required payload containing one stable error code.

    Beginner note:
        Counts are checked as soon as their corresponding parser objects become
        visible. This is different from extracting everything and truncating
        afterward, which would spend the memory we are trying to protect.
    """
    opener = open_pdf if open_pdf is not None else _default_open_pdf
    pages: list[dict[str, Any]] = []
    cells_seen = 0
    text_seen = 0
    glyphs_seen = 0
    try:
        with opener(pdf_path) as pdf:
            pdf_pages = pdf.pages
            if len(pdf_pages) > _limit(budget, "max_pages"):
                raise _WorkerParseError("page_limit_exceeded")
            for index, page in enumerate(pdf_pages, start=1):
                # pdfminer exposes characters before text/table expansion.
                # Bounding glyphs first limits work at the earliest useful seam.
                glyph_count = len(getattr(page, "chars", ()))
                if glyph_count > _limit(budget, "max_glyphs_per_page"):
                    raise _WorkerParseError("page_glyph_limit_exceeded")
                glyphs_seen += glyph_count
                if glyphs_seen > _limit(budget, "max_glyphs_per_document"):
                    raise _WorkerParseError("document_glyph_limit_exceeded")

                text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if len(text) > _limit(budget, "max_page_text_chars"):
                    raise _WorkerParseError("page_text_limit_exceeded")
                text_seen += len(text)
                if text_seen > _limit(budget, "max_document_text_chars"):
                    raise _WorkerParseError("document_text_limit_exceeded")
                tables, cells_seen = _bounded_tables(
                    page,
                    index,
                    budget,
                    cells_seen=cells_seen,
                )
                pages.append(
                    {"page_number": index, "text": text, "tables": tables}
                )
    except _WorkerParseError as exc:
        return _review_payload(exc.code)
    except Exception:  # noqa: BLE001 - parser text is untrusted
        return _review_payload("unreadable_pdf")

    if not pages or all(not str(page["text"]).strip() for page in pages):
        return _review_payload("empty_document")
    return {"status": "success", "error_code": None, "pages": pages}


def _apply_linux_memory_limit(budget: Mapping[str, int | float]) -> None:
    """Apply the child-only address-space limit before importing pdfplumber.

    Beginner note:
        The limit belongs in this lightweight module because a spawned process
        imports its target module before calling the target function. Importing
        the broad IPO facade first would load unrelated libraries and consume
        much of the budget before the PDF parser starts.
    """
    if sys.platform != "linux":
        return
    import resource

    maximum = _limit(budget, "linux_address_space_bytes")
    resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))


def _encode_payload(payload: Mapping[str, Any]) -> bytes:
    """Encode only primitive JSON so no executable object crosses back.

    JSON costs a little more encoding work than pickle, but it gives the parent
    a small data-only format that can be decoded and validated independently.
    """
    return json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def worker_entry(
    pdf_path: str,
    budget: Mapping[str, int | float],
    send_connection: Any,
) -> None:
    """Contain parser state and emit one bounded byte message to the parent.

    Args:
        pdf_path: Verified cache path selected by the parent.
        budget: Primitive resource limits supplied by the parent.
        send_connection: One-way pipe endpoint owned by this child.

    Beginner note:
        This is a multiprocessing entrypoint, so it must stay at module scope
        and accept spawn-serializable arguments. Cleanup lives in ``finally`` so
        the parent can reliably detect EOF after success, failure, or timeout.
    """
    try:
        _apply_linux_memory_limit(budget)
        encoded = _encode_payload(extract_payload(pdf_path, budget))
        if len(encoded) > _limit(budget, "max_serialized_result_bytes"):
            encoded = _encode_payload(
                _review_payload("worker_result_limit_exceeded")
            )
        send_connection.send_bytes(encoded)
    finally:
        send_connection.close()
