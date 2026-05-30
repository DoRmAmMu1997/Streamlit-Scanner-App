from __future__ import annotations

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

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from backend.config import FUNDAMENTALS_PDF_DIR


logger = logging.getLogger(__name__)


_REQUEST_TIMEOUT_SECONDS = 30
_PDF_USER_AGENT = (
    "hemant-scanner/1.0 (+personal use; "
    "https://github.com/DoRmAmMu1997/Streamlit-Scanner-App)"
)


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
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]
    return f"{safe_tail}_{digest}"


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
    # Only fetch over HTTP(S). Rejecting other schemes (file://, ftp://, etc.)
    # closes an SSRF / local-file-read avenue if a transcript_url is ever
    # malformed or tampered with upstream on screener.in.
    if urlparse(url).scheme.lower() not in ("http", "https"):
        logger.warning("Refusing to fetch non-http(s) PDF URL: %s", url)
        return None
    cache_root = Path(cache_dir) if cache_dir else FUNDAMENTALS_PDF_DIR
    cache_root.mkdir(parents=True, exist_ok=True)

    stem = _safe_filename(url)
    pdf_path = cache_root / f"{stem}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path

    owned_session = session is None
    sess = session or requests.Session()
    try:
        response = sess.get(
            url,
            headers={"User-Agent": _PDF_USER_AGENT, "Accept": "application/pdf,*/*"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        if response.status_code != 200:
            logger.warning("PDF fetch %s returned HTTP %s", url, response.status_code)
            return None
        if not response.content:
            return None
        pdf_path.write_bytes(response.content)
        return pdf_path
    except requests.RequestException:
        logger.warning("PDF fetch %s failed", url, exc_info=True)
        return None
    finally:
        if owned_session:
            sess.close()


def _extract_with_pdfplumber(pdf_path: Path) -> str:
    """Primary extractor — pure-Python, MIT, works for typeset PDFs."""
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("pdfplumber not installed; cannot extract PDF text")
        return ""

    chunks: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if page_text:
                    chunks.append(page_text)
    except Exception:  # noqa: BLE001 — extractors throw odd errors on weird PDFs
        logger.warning("pdfplumber failed on %s", pdf_path, exc_info=True)
        return ""
    return "\n\n".join(chunks).strip()


def _extract_with_pypdf(pdf_path: Path) -> str:
    """Fallback extractor — ``pypdf`` if available, otherwise empty."""
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return ""

    try:
        reader = PdfReader(str(pdf_path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:  # noqa: BLE001
        logger.warning("pypdf failed on %s", pdf_path, exc_info=True)
        return ""


def extract_text(pdf_path: Path | str) -> str:
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

    text_cache = pdf_path.with_suffix(".txt")
    if text_cache.exists() and text_cache.stat().st_mtime >= pdf_path.stat().st_mtime:
        try:
            cached = text_cache.read_text(encoding="utf-8")
            if cached.strip():
                return cached
        except OSError:
            # Fall through to re-extract if the cached text file is unreadable.
            pass

    text = _extract_with_pdfplumber(pdf_path)
    if not text:
        text = _extract_with_pypdf(pdf_path)

    if text:
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
        text = extract_text(pdf_path)
        if not text:
            continue
        if len(text) > max_chars:
            # Keep the front of the transcript — opening remarks + management
            # commentary live early; later pages are usually Q&A repeats.
            return text[:max_chars] + "\n\n[... transcript truncated ...]"
        return text
    return ""
