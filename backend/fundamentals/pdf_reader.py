"""Download + extract text from screener.in-linked PDF documents.

The Check Fundamentals agent exposes a `read_recent_concall_transcript` tool.
That tool calls into this module to (a) download the PDF behind the most
recent concall row's `transcript_url`, (b) extract its plain text with
``pdfplumber`` (pure-Python, no model download), and (c) hand the text back
to the LLM.

Two layers of caching:
- The PDF itself is persisted under ``data/cache/fundamentals/pdfs/`` so
  repeated tool calls do not re-download the same document.
- The extracted text is cached as a sibling ``.transcript-v1.txt`` file so re-runs skip
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
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from backend.config import FUNDAMENTALS_PDF_DIR
from backend.fundamentals.pdf_transport import Resolver, Transport, open_pinned_response, resolve_public_target
from backend.transcript_pdf_process import run_transcript_worker as _run_transcript_worker

logger = logging.getLogger(__name__)


_REQUEST_TIMEOUT_SECONDS = 30
# Wall-clock budget for the whole download: every redirect hop and every body
# chunk. The per-request timeout above only bounds a single socket wait, so a
# host dripping one byte per 29 s would otherwise never be cut off.
_DOWNLOAD_DEADLINE_SECONDS = 120
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


def _publish_atomically(data: bytes, destination: Path) -> None:
    """Write ``data`` to a same-directory temp file, then rename it into place.

    Beginner note: the cache treats any non-empty ``.pdf`` as a hit. Writing
    the destination directly means a crash or full disk mid-write leaves a
    truncated file that is served forever. ``os.replace`` within one directory
    is atomic, so readers see either no file or the complete one; the
    ``finally`` removes the temp file whenever the rename did not happen.
    """
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.stem}.", suffix=".tmp", delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
    try:
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def download_pdf(
    url: str,
    *,
    cache_dir: Path | str | None = None,
    session: requests.Session | None = None,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
) -> Path | None:
    """Download a public transcript to disk, returning ``None`` on refusal/failure.

    Args:
        url: Untrusted transcript URL scraped from a third-party page.
        cache_dir: Optional destination for the existing PDF cache.
        session: Legacy injection, unsupported without an explicit transport.
        resolver: Trusted DNS resolver seam for offline tests, never URL input.
        transport: Trusted single-hop response seam for offline tests.

    Returns:
        Cached/downloaded PDF path, or ``None`` if validation or fetching fails.

    Beginner note: each redirect is a fresh untrusted destination. Validate it
    before opening a response, and pin the socket to that validated IP. Passing
    a Session no longer waives DNS checks; legacy session-only injection fails
    safely instead of inheriting its proxies, credentials or unsafe adapters.
    One wall-clock deadline covers every hop and body chunk, and the file is
    published atomically so an interrupted write never becomes a cache hit.
    """
    if session is not None and transport is None:
        logger.warning("PDF Session injection is unsupported; use explicit resolver/transport injection")
        return None
    if not url:
        return None
    fetch = transport or open_pinned_response
    try:
        target = resolve_public_target(url, resolver=resolver)
        cache_root = Path(cache_dir) if cache_dir else FUNDAMENTALS_PDF_DIR
        cache_root.mkdir(parents=True, exist_ok=True)
        pdf_path = cache_root / f"{_safe_filename(url)}.pdf"
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return pdf_path

        visited: set[str] = set()
        deadline = time.monotonic() + _DOWNLOAD_DEADLINE_SECONDS
        # Three redirects permit at most four requests. A fourth redirect is
        # refused without even resolving or contacting its destination.
        for hop in range(4):
            if target.url in visited:
                return None
            visited.add(target.url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("PDF fetch exceeded its %d-second deadline", _DOWNLOAD_DEADLINE_SECONDS)
                return None
            with fetch(
                target,
                headers={"User-Agent": _PDF_USER_AGENT, "Accept": "application/pdf,*/*"},
                timeout=min(_REQUEST_TIMEOUT_SECONDS, remaining),
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if hop == 3 or not location or not location.strip():
                        return None
                    # urljoin accepts relative links. Validate raw Location first
                    # because it otherwise strips some leading control bytes.
                    if any(ord(char) <= 32 or ord(char) == 127 for char in location) or "\\" in location:
                        return None
                    next_url = urljoin(target.url, location)
                elif response.status_code == 200:
                    buffer = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        if time.monotonic() > deadline:
                            logger.warning("PDF fetch exceeded its %d-second deadline", _DOWNLOAD_DEADLINE_SECONDS)
                            return None
                        if not chunk:
                            continue
                        if len(buffer) + len(chunk) > _MAX_PDF_BYTES:
                            logger.warning("PDF fetch exceeded the %d-byte cap", _MAX_PDF_BYTES)
                            return None
                        buffer.extend(chunk)
                    if not buffer or not _looks_like_pdf(response.headers.get("Content-Type"), bytes(buffer[:1024])):
                        return None
                    _publish_atomically(bytes(buffer), pdf_path)
                    return pdf_path
                else:
                    logger.warning("PDF fetch returned HTTP %s", response.status_code)
                    return None
            # Leave the response context before DNS validation of the next hop,
            # so refusal and resolution failures never retain an open socket.
            target = resolve_public_target(next_url, resolver=resolver)
        return None
    except (requests.RequestException, OSError, ValueError):
        # Do not put signed query strings, userinfo or HTTP exception bodies in
        # logs. The caller deliberately treats absent transcripts as no evidence.
        logger.warning("PDF fetch failed or destination was refused")
        return None


def extract_text(
    pdf_path: Path | str,
    *,
    max_chars: int | None = None,
    max_pages: int | None = None,
) -> str:
    """Return bounded transcript text, or empty text when containment fails.

    Args:
        pdf_path: Downloaded PDF on local disk.
        max_chars: Optional stricter limit within the 40,000-character ceiling.
        max_pages: Optional stricter limit within the first-30-page ceiling.

    Returns:
        Validated transcript text within both ceilings, or an empty string if
        the file, parsers, resource containment, or worker receipt is unavailable.

    Beginner note:
        Both parsers run in the same killable child with a 60-second deadline
        and a 512 MiB OS memory limit. There is no in-process fallback. A new
        cache suffix separates bounded transcript receipts from legacy full-text
        caches, whose page count cannot be verified. Limited calls never write
        the shared cache, so one small prompt cannot truncate a later request.
    """
    pdf_path = Path(pdf_path)
    chars = min(40000, max_chars) if max_chars is not None else 40000
    pages = min(30, max_pages) if max_pages is not None else 30
    if chars <= 0 or pages <= 0 or not pdf_path.is_file():
        return ""
    cache_allowed = max_chars is None and max_pages is None
    text_cache = pdf_path.with_suffix(".transcript-v1.txt")
    try:
        if cache_allowed and text_cache.is_file() and text_cache.stat().st_mtime >= pdf_path.stat().st_mtime:
            with text_cache.open(encoding="utf-8") as stream:
                cached = stream.read(chars + 1)
            if cached.strip() and len(cached) <= chars:
                return cached
    except (OSError, UnicodeError):
        pass
    try:
        encoded = _run_transcript_worker(pdf_path, max_chars=chars, max_pages=pages)
        if len(encoded) > 256 * 1024:
            return ""
        payload = json.loads(encoded.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"text"}:
            return ""
        text = payload["text"]
        if not isinstance(text, str) or len(text) > chars:
            return ""
    except Exception:  # noqa: BLE001 - parser/process errors expose no hostile text
        return ""
    if text and cache_allowed:
        try:
            text_cache.write_text(text, encoding="utf-8")
        except OSError:
            logger.warning("Could not write bounded transcript cache")
    return text


def read_recent_concall_text(
    concalls: Iterable[dict[str, Any]] | None,
    *,
    cache_dir: Path | str | None = None,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
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

    Beginner note: ``resolver``/``transport`` are the same trusted offline-test
    seams ``download_pdf`` accepts. A bare Session is no longer accepted here,
    because ``download_pdf`` refuses session-only injection and would turn
    every such call into a silent empty transcript.
    """
    if not concalls:
        return ""
    for row in concalls:
        url = (row or {}).get("transcript_url")
        if not url:
            continue
        pdf_path = download_pdf(url, cache_dir=cache_dir, resolver=resolver, transport=transport)
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
