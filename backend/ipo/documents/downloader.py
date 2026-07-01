"""Bounded, SSRF-resistant SEBI prospectus downloader for IPO-003.

Beginner note:
This module deliberately has one narrow job: turn an already-inventoried SEBI
filing-detail URL into verified PDF bytes on disk. It does not update database
rows or parse pages. The repository service performs database work before and
after this function so a slow network request never keeps a transaction open.

The cache is *content addressed*. Its filename is the SHA-256 digest of the PDF
bytes, not a company name or remote filename. Two records containing identical
bytes therefore converge on one immutable local file without trusting either
remote name.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import ipaddress
import os
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Never
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from backend.ipo.models import IpoDocumentParseStatus, IpoDocumentRecord

ALLOWED_HOSTS = frozenset({"sebi.gov.in", "www.sebi.gov.in"})
ALLOWED_PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/octet-stream"})
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)
STREAM_CHUNK_BYTES = 64 * 1024


class IpoDocumentDownloadErrorCode(enum.StrEnum):
    """Stable, secret-safe failure reasons suitable for logs and audit rows."""

    UNSAFE_URL = "unsafe_url"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
    INVALID_DETAIL_PAGE = "invalid_detail_page"
    INVALID_PDF = "invalid_pdf"
    UNSAFE_CACHE_PATH = "unsafe_cache_path"
    SOURCE_CHANGED = "source_changed"
    UNSUPPORTED_DOCUMENT_TYPE = "unsupported_document_type"


class IpoDocumentDownloadError(RuntimeError):
    """Raise one sanitized downloader failure without remote response details."""

    def __init__(self, code: IpoDocumentDownloadErrorCode) -> None:
        """Store one stable error code without retaining unsafe remote details."""
        self.code = IpoDocumentDownloadErrorCode(code)
        super().__init__(f"IPO document download failed ({self.code.value}).")


@dataclass(frozen=True)
class IpoDocumentDownloadResult:
    """Verified cache metadata returned to the database orchestration layer."""

    document_id: int
    content_sha256: str
    downloaded_at: dt.datetime
    file_path: str
    page_count: int | None
    parse_status: IpoDocumentParseStatus
    cache_hit: bool
    bytes_written: int


def _raise(code: IpoDocumentDownloadErrorCode) -> Never:
    """Keep failure construction terse while preserving one safe error shape."""
    raise IpoDocumentDownloadError(code)


def _canonical_sebi_url(
    value: str,
    *,
    base_url: str | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    require_pdf_path: bool = False,
) -> str:
    """Return one canonical public SEBI HTTPS URL or fail before networking.

    Host allowlisting blocks ordinary SSRF, while resolving the allowlisted host
    and rejecting non-public answers also catches a poisoned hosts file or DNS
    response that points SEBI's name at loopback/private infrastructure.
    """
    candidate = urljoin(base_url or "", str(value).strip())
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)
    if (
        parsed.scheme.casefold() != "https"
        or host not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)
    if require_pdf_path and not parsed.path.startswith("/sebi_data/attachdocs/"):
        _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)

    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM)
        addresses = {str(answer[4][0]) for answer in answers}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)
    except IpoDocumentDownloadError:
        raise
    except (OSError, TypeError, ValueError, IndexError):
        _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _content_type(response: Any) -> str:
    """Normalize a response media type while ignoring optional charset data."""
    return str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()


def _declared_length(response: Any, limit: int) -> None:
    """Reject an oversized or malformed Content-Length before reading a body."""
    raw = response.headers.get("Content-Length")
    if raw is None:
        return
    try:
        length = int(raw)
    except (TypeError, ValueError):
        _raise(IpoDocumentDownloadErrorCode.RESPONSE_TOO_LARGE)
    if length < 0 or length > limit:
        _raise(IpoDocumentDownloadErrorCode.RESPONSE_TOO_LARGE)


def _request_with_redirects(
    session: Any,
    url: str,
    *,
    resolver: Callable[..., Any],
) -> Any:
    """GET one URL while validating and closing every manual redirect hop."""
    current_url = _canonical_sebi_url(url, resolver=resolver)
    for redirect_count in range(MAX_REDIRECTS + 1):
        response = session.get(
            current_url,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": "Streamlit-Scanner-App/IPO-003"},
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        try:
            location = response.headers.get("Location")
            if not location or redirect_count >= MAX_REDIRECTS:
                _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)
            current_url = _canonical_sebi_url(
                str(location), base_url=current_url, resolver=resolver
            )
        finally:
            response.close()
    _raise(IpoDocumentDownloadErrorCode.UNSAFE_URL)


def _fetch(
    session: Any,
    url: str,
    *,
    resolver: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> Any:
    """Return an open successful response after bounded transient retries."""
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        response = None
        try:
            response = _request_with_redirects(session, url, resolver=resolver)
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt == len(RETRY_DELAYS_SECONDS):
                    response.close()
                    response = None
                    _raise(IpoDocumentDownloadErrorCode.HTTP_ERROR)
                response.close()
                response = None
                sleeper(RETRY_DELAYS_SECONDS[attempt])
                continue
            if response.status_code != 200:
                response.close()
                response = None
                _raise(IpoDocumentDownloadErrorCode.HTTP_ERROR)
            return response
        except requests.RequestException:
            if response is not None:
                response.close()
            if attempt == len(RETRY_DELAYS_SECONDS):
                _raise(IpoDocumentDownloadErrorCode.NETWORK_ERROR)
            sleeper(RETRY_DELAYS_SECONDS[attempt])
    _raise(IpoDocumentDownloadErrorCode.NETWORK_ERROR)


def _read_html(response: Any) -> bytes:
    """Read one detail page within the 2 MiB hostile-input budget."""
    if _content_type(response) != "text/html":
        _raise(IpoDocumentDownloadErrorCode.UNEXPECTED_CONTENT_TYPE)
    _declared_length(response, MAX_HTML_BYTES)
    body = bytearray()
    for chunk in response.iter_content(chunk_size=STREAM_CHUNK_BYTES):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_HTML_BYTES:
            _raise(IpoDocumentDownloadErrorCode.RESPONSE_TOO_LARGE)
    return bytes(body)


def _extract_pdf_url(
    body: bytes,
    *,
    detail_url: str,
    resolver: Callable[..., Any],
) -> str:
    """Extract exactly one official prospectus iframe target from hostile HTML."""
    soup = BeautifulSoup(body, "html.parser")
    candidates: list[str] = []
    for iframe in soup.find_all("iframe"):
        source = iframe.get("src")
        if not source:
            continue
        wrapper = urlsplit(urljoin(detail_url, str(source)))
        values = parse_qs(wrapper.query, keep_blank_values=True).get("file", [])
        if len(values) != 1 or not values[0].strip():
            continue
        candidates.append(
            _canonical_sebi_url(
                values[0],
                base_url=detail_url,
                resolver=resolver,
                require_pdf_path=True,
            )
        )
    if len(candidates) != 1:
        _raise(IpoDocumentDownloadErrorCode.INVALID_DETAIL_PAGE)
    return candidates[0]


def _contained_cache_path(data_dir: Path, relative_value: str) -> Path:
    """Resolve a stored POSIX path without allowing traversal or symlink escape."""
    relative = PurePosixPath(str(relative_value))
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in str(relative_value)
        or (relative.parts and ":" in relative.parts[0])
    ):
        _raise(IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH)
    root = data_dir.resolve()
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        _raise(IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            _raise(IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH)
    return candidate


def _hash_file(path: Path) -> tuple[str, int]:
    """Calculate the digest and byte count without loading a PDF into memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _verified_cache_result(
    document: IpoDocumentRecord,
    *,
    data_dir: Path,
) -> IpoDocumentDownloadResult | None:
    """Return a cache hit only after containment, type, size, and digest checks."""
    if not (document.content_sha256 and document.file_path and document.downloaded_at):
        return None
    path = _contained_cache_path(data_dir, document.file_path)
    if path.exists() and path.is_file() and not path.is_symlink():
        digest, size = _hash_file(path)
        if digest == document.content_sha256 and size <= MAX_PDF_BYTES:
            return IpoDocumentDownloadResult(
                document_id=document.id,
                content_sha256=digest,
                downloaded_at=document.downloaded_at,
                file_path=document.file_path,
                page_count=None,
                parse_status=IpoDocumentParseStatus.PENDING,
                cache_hit=True,
                bytes_written=size,
            )
        # The row points inside our cache but the bytes no longer match their
        # provenance digest. Remove only this controlled regular file, then fetch
        # a fresh copy instead of ever returning corrupt data.
        path.unlink()
    return None


def _stream_pdf_to_cache(
    response: Any,
    *,
    document_id: int,
    data_dir: Path,
    downloaded_at: dt.datetime,
) -> IpoDocumentDownloadResult:
    """Stream, validate, fsync, and atomically publish one content-addressed PDF."""
    if _content_type(response) not in ALLOWED_PDF_CONTENT_TYPES:
        _raise(IpoDocumentDownloadErrorCode.UNEXPECTED_CONTENT_TYPE)
    _declared_length(response, MAX_PDF_BYTES)
    # Validate the directory itself before opening the temporary file. Checking
    # only the final digest path would detect a symlink escape after untrusted
    # bytes had already been written through that link.
    cache_dir = _contained_cache_path(data_dir, "ipo/documents")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Re-resolve after creation so an unexpected pre-existing link or directory
    # replacement cannot hide behind the non-strict pre-creation resolution.
    cache_dir = _contained_cache_path(data_dir, "ipo/documents")
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".download-", suffix=".part", dir=cache_dir, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            for chunk in response.iter_content(chunk_size=STREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    _raise(IpoDocumentDownloadErrorCode.RESPONSE_TOO_LARGE)
                if len(prefix) < 5:
                    prefix.extend(chunk[: 5 - len(prefix)])
                digest.update(chunk)
                handle.write(chunk)
            if not bytes(prefix).startswith(b"%PDF-"):
                _raise(IpoDocumentDownloadErrorCode.INVALID_PDF)
            handle.flush()
            os.fsync(handle.fileno())

        content_sha256 = digest.hexdigest()
        relative = PurePosixPath("ipo", "documents", f"{content_sha256}.pdf")
        final_path = _contained_cache_path(data_dir, relative.as_posix())
        os.replace(temporary_path, final_path)
        temporary_path = None
        return IpoDocumentDownloadResult(
            document_id=document_id,
            content_sha256=content_sha256,
            downloaded_at=downloaded_at,
            file_path=relative.as_posix(),
            page_count=None,
            parse_status=IpoDocumentParseStatus.PENDING,
            cache_hit=False,
            bytes_written=size,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def download_document_file(
    document: IpoDocumentRecord,
    *,
    data_dir: Path,
    session: Any | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> IpoDocumentDownloadResult:
    """Download or verify one DRHP/RHP file without touching the database.

    Args:
        document: Detached metadata loaded by the repository before networking.
        data_dir: Runtime data root; returned paths are relative to this folder.
        session: Optional requests-compatible session for deterministic tests.
        sleeper: Retry-delay function, injectable so tests do not really sleep.
        resolver: DNS resolver used to reject private/non-public destinations.
        now: UTC clock used for reproducible provenance timestamps.

    Returns:
        Frozen verified cache metadata ready for a short persistence transaction.

    Raises:
        IpoDocumentDownloadError: For every safe, categorized failure condition.
    """
    # Defense in depth behind download_document()'s own IpoValidationError: only
    # DRHP/RHP prospectuses are downloadable. A distinct code keeps an operator
    # reading a log or audit from mistaking this for an SSRF/URL rejection.
    if document.document_type not in {"drhp", "rhp"}:
        _raise(IpoDocumentDownloadErrorCode.UNSUPPORTED_DOCUMENT_TYPE)
    cache_hit = _verified_cache_result(document, data_dir=data_dir)
    if cache_hit is not None:
        return cache_hit

    owned_session = session is None
    active_session = requests.Session() if session is None else session
    response = None
    try:
        detail_url = _canonical_sebi_url(document.document_url, resolver=resolver)
        response = _fetch(
            active_session, detail_url, resolver=resolver, sleeper=sleeper
        )
        media_type = _content_type(response)
        if media_type == "text/html":
            try:
                pdf_url = _extract_pdf_url(
                    _read_html(response), detail_url=detail_url, resolver=resolver
                )
            finally:
                response.close()
                response = None
            response = _fetch(
                active_session, pdf_url, resolver=resolver, sleeper=sleeper
            )
        return _stream_pdf_to_cache(
            response,
            document_id=document.id,
            data_dir=data_dir,
            downloaded_at=now().astimezone(dt.UTC),
        )
    finally:
        if response is not None:
            response.close()
        if owned_session:
            active_session.close()
