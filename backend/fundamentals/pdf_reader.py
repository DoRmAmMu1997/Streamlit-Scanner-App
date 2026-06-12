"""Download + extract text from screener.in-linked PDF documents.

The Check Fundamentals agent exposes a `read_recent_concall_transcript` tool.
That tool calls into this module to (a) download the PDF behind the most
recent concall row's `transcript_url`, (b) extract its plain text with
``pdfplumber`` (pure-Python, no model download), and (c) hand the text back
to the LLM.

Two layers of caching:
- The PDF itself is persisted under ``data/cache/fundamentals/pdfs/`` so
  repeated tool calls do not re-download the same document.
- The extracted text is cached as a sibling ``.txt`` file so re-runs skip
  the (relatively slow) parse step.

Failure mode: every function returns an empty string or ``None`` on any
problem (404, malformed PDF, parse error). The agent treats "no text" as
"no transcript available" and writes its forward outlook from announcements
+ structured data only. This is intentional: a missing transcript should
not crash the whole verdict.

Extension point: when ``extract_text`` returns ``""`` for what is clearly a
scanned PDF, a future revision can swap in a HuggingFace OCR pass (e.g.
``microsoft/trocr-base-printed`` via ``transformers``). The interface
stays the same — callers see one ``str`` either way.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import requests

from backend.config import FUNDAMENTALS_PDF_DIR
from backend.url_safety import is_safe_http_url

logger = logging.getLogger(__name__)


_REQUEST_TIMEOUT_SECONDS = 30
_PDF_USER_AGENT = (
    "hemant-scanner/1.0 (+personal use; "
    "https://github.com/DoRmAmMu1997/Streamlit-Scanner-App)"
)
# Concall PDFs are streamed to disk and the running byte total is checked
# against this ceiling. transcript_url values are scraped from screener.in, so
# an oversized or malicious URL must not be able to read an unbounded body into
# memory (DoS). Mirrors the streamed byte cap in backend/universe_builder.py.
_MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MiB
_MAX_PDF_PAGES = 30


def _safe_filename(url: str, *, fallback_prefix: str = "doc") -> str:
    """Turn a URL into a safe local filename stem.

    Strips query strings, keeps a short trailing slug, and adds a hash-like
    suffix so two URLs that share a filename do not collide.
    """
    cleaned = re.sub(r"[?#].*$", "", url)
    tail = cleaned.rstrip("/").rsplit("/", 1)[-1] or fallback_prefix
    # Strip extension; we'll add .pdf ourselves.
    tail = re.sub(r"\.pdf$", "", tail, flags=re.IGNORECASE)
    # Sanitize for the filesystem.
    safe_tail = re.sub(r"[^A-Za-z0-9._-]+", "_", tail)[:80] or fallback_prefix
    # Short hash for collision avoidance across symbols. Uses hashlib (not the
    # builtin hash(), which is salted per-process) so the same URL maps to the
    # same filename across restarts — otherwise the on-disk PDF cache would
    # silently miss every new session.
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:10]
    return f"{safe_tail}_{digest}"


def _looks_like_pdf(content_type: str | None, body: bytes) -> bool:
    """Return True when headers and bytes agree this is plausibly a PDF.

    URLs ending in `.pdf` can still return HTML login/error pages. Checking the
    response type plus the PDF magic bytes keeps those pages out of the parser
    and the on-disk cache.
    """
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type and normalized_type not in {
        "application/pdf",
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return False
    return body.lstrip().startswith(b"%PDF-")


def download_pdf(
    url: str,
    *,
    cache_dir: Path | str | None = None,
    session: requests.Session | None = None,
) -> Path | None:
    """Download ``url`` to disk and return the path, or ``None`` on failure.

    Cache hits return the existing path without re-fetching.
    """
    if not url:
        return None
    owned_session = session is None
    # Transcript URLs are scraped from third-party pages, so they are untrusted.
    # A safe URL must be public HTTP(S). Real network fetches also resolve DNS
    # to reject domains pointing at private/link-local addresses; injected test
    # sessions skip DNS so unit tests stay offline.
    if not is_safe_http_url(url, resolve_dns=owned_session):
        logger.warning("Refusing to fetch unsafe PDF URL: %s", url)
        return None
    cache_root = Path(cache_dir) if cache_dir else FUNDAMENTALS_PDF_DIR
    cache_root.mkdir(parents=True, exist_ok=True)

    stem = _safe_filename(url)
    pdf_path = cache_root / f"{stem}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path

    sess = session or requests.Session()
    try:
        # stream=True + a chunked read so an oversized response can never be
        # pulled into memory all at once. The response is a context manager so
        # an early return (bad status, oversized body) closes the connection on
        # the way out — same idiom as the capped download in universe_builder.
        with sess.get(
            url,
            headers={"User-Agent": _PDF_USER_AGENT, "Accept": "application/pdf,*/*"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
            stream=True,
        ) as response:
            if response.status_code != 200:
                logger.warning("PDF fetch %s returned HTTP %s", url, response.status_code)
                return None
            final_url = getattr(response, "url", url) or url
            if not is_safe_http_url(final_url, resolve_dns=owned_session):
                logger.warning("PDF fetch %s redirected to unsafe URL %s", url, final_url)
                return None
            buffer = bytearray()
            for chunk in response.iter_content(chunk_size=65536):  # 64 KiB
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) > _MAX_PDF_BYTES:
                    logger.warning(
                        "PDF fetch %s exceeded the %d-byte cap; aborting download",
                        url,
                        _MAX_PDF_BYTES,
                    )
                    return None
            if not buffer:
                return None
            if not _looks_like_pdf(response.headers.get("Content-Type"), bytes(buffer[:1024])):
                logger.warning("PDF fetch %s did not return a PDF-like response", url)
                return None
            pdf_path.write_bytes(bytes(buffer))
            return pdf_path
    except requests.RequestException:
        logger.warning("PDF fetch %s failed", url, exc_info=True)
        return None
    finally:
        if owned_session:
            sess.close()


def _append_limited(
    chunks: list[str],
    page_text: str,
    *,
    max_chars: int | None,
) -> bool:
    """Append text and return False once the caller has enough characters.

    Both PDF extractors join pages with blank lines. This helper keeps the
    limit logic identical across pdfplumber and pypdf, and lets parsing stop as
    soon as the model prompt has enough transcript text.
    """
    if not page_text:
        return True
    if max_chars is None:
        chunks.append(page_text)
        return True
    current = "\n\n".join(chunks)
    separator_len = 2 if current else 0
    remaining = max_chars - len(current) - separator_len
    if remaining <= 0:
        return False
    chunks.append(page_text[:remaining])
    return len("\n\n".join(chunks)) < max_chars


def _extract_with_pdfplumber(
    pdf_path: Path,
    *,
    max_chars: int | None = None,
    max_pages: int | None = None,
) -> str:
    """Primary extractor — pure-Python, MIT, works for typeset PDFs."""
    try:
        import pdfplumber  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        logger.warning("pdfplumber not installed; cannot extract PDF text")
        return ""

    chunks: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = pdf.pages[:max_pages] if max_pages is not None else pdf.pages
            for page in pages:
                page_text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if not _append_limited(chunks, page_text, max_chars=max_chars):
                    break
    except Exception:  # noqa: BLE001 — extractors throw odd errors on weird PDFs
        logger.warning("pdfplumber failed on %s", pdf_path, exc_info=True)
        return ""
    return "\n\n".join(chunks).strip()


def _extract_with_pypdf(
    pdf_path: Path,
    *,
    max_chars: int | None = None,
    max_pages: int | None = None,
) -> str:
    """Fallback extractor — ``pypdf`` if available, otherwise empty."""
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        return ""

    try:
        reader = PdfReader(str(pdf_path))
        pages = reader.pages[:max_pages] if max_pages is not None else reader.pages
        chunks: list[str] = []
        for page in pages:
            if not _append_limited(chunks, page.extract_text() or "", max_chars=max_chars):
                break
        return "\n\n".join(chunks).strip()
    except Exception:  # noqa: BLE001
        logger.warning("pypdf failed on %s", pdf_path, exc_info=True)
        return ""


def extract_text(
    pdf_path: Path | str,
    *,
    max_chars: int | None = None,
    max_pages: int | None = None,
) -> str:
    """Return the plain-text contents of ``pdf_path``, or ``""`` on failure.

    The extracted text is cached alongside the PDF as ``<stem>.txt`` so
    repeated calls (same agent re-run, multiple stocks share a transcript)
    skip the slow extraction. The cache file is invalidated automatically
    when the PDF is replaced (re-download writes a new bytes blob with the
    same stem).
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return ""

    # Only unlimited extraction uses the shared text cache. A limited parse is
    # intentionally partial; writing it to `<stem>.txt` would make a future full
    # extraction incorrectly return truncated text.
    cache_allowed = max_chars is None and max_pages is None
    text_cache = pdf_path.with_suffix(".txt")
    if (
        cache_allowed
        and text_cache.exists()
        and text_cache.stat().st_mtime >= pdf_path.stat().st_mtime
    ):
        try:
            cached = text_cache.read_text(encoding="utf-8")
            if cached.strip():
                return cached
        except OSError:
            # Fall through to re-extract if the cached text file is unreadable.
            pass

    text = _extract_with_pdfplumber(pdf_path, max_chars=max_chars, max_pages=max_pages)
    if not text:
        text = _extract_with_pypdf(pdf_path, max_chars=max_chars, max_pages=max_pages)

    if text and cache_allowed:
        try:
            text_cache.write_text(text, encoding="utf-8")
        except OSError:
            logger.warning("Could not write text cache to %s", text_cache, exc_info=True)
    return text


def read_recent_concall_text(
    concalls: Iterable[dict[str, Any]] | None,
    *,
    cache_dir: Path | str | None = None,
    session: requests.Session | None = None,
    max_chars: int = 40000,
) -> str:
    """Download + extract the most recent concall transcript and return its text.

    `concalls` is the list shape produced by `_extract_concalls` in the
    scraper: ``[{month, transcript_url, ai_summary_url, ppt_url, rec_url}, ...]``,
    newest first. We walk it and pick the first row whose ``transcript_url``
    is set; everything else (PPTs, recordings) is ignored because the agent
    only consumes text.

    Returns ``""`` if no transcript is available or the download / parse fails.
    The result is truncated to ``max_chars`` so a 50-page transcript still
    fits inside the model's context comfortably.
    """
    if not concalls:
        return ""
    for row in concalls:
        url = (row or {}).get("transcript_url")
        if not url:
            continue
        pdf_path = download_pdf(url, cache_dir=cache_dir, session=session)
        if pdf_path is None:
            continue
        # Limit during extraction, not after, so a parser-bomb style PDF cannot
        # force the worker to process every page before we trim the prompt text.
        text = extract_text(pdf_path, max_chars=max_chars, max_pages=_MAX_PDF_PAGES)
        if not text:
            continue
        if len(text) > max_chars:
            # Keep the front of the transcript — opening remarks + management
            # commentary live early; later pages are usually Q&A repeats.
            return text[:max_chars] + "\n\n[... transcript truncated ...]"
        return text
    return ""
