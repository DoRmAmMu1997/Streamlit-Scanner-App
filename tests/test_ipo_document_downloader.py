"""IPO-003 secure document downloader and content-addressed cache tests."""

from __future__ import annotations

import datetime as dt
import hashlib
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests

from backend.ipo.documents.downloader import (
    MAX_PDF_BYTES,
    IpoDocumentDownloadError,
    IpoDocumentDownloadErrorCode,
    download_document_file,
)
from backend.ipo.models import Confidence, IpoDocumentParseStatus, IpoDocumentRecord

PDF_BYTES = b"%PDF-1.7\nsmall trusted fixture\n%%EOF\n"
DETAIL_URL = "https://www.sebi.gov.in/filings/public-issues/example-rhp.html"
PDF_URL = "https://www.sebi.gov.in/sebi_data/attachdocs/example.pdf"


def _public_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    """Return a stable public address so tests never perform live DNS lookups."""
    assert host in {"sebi.gov.in", "www.sebi.gov.in"}
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


class FakeResponse:
    """Small streaming response double that records resource cleanup."""

    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        content_type: str = "application/pdf",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the deterministic FakeResponse test double without live I/O."""
        self.body = body
        self.status_code = status_code
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield bounded chunks just like ``requests.Response.iter_content``."""
        self.iterated = True
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        """Record that the downloader released the HTTP connection."""
        self.closed = True


class FailingStreamResponse(FakeResponse):
    """Response double whose body fails after headers have been accepted."""

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Raise lazily, matching failures surfaced by requests while streaming."""
        self.iterated = True
        raise requests.ConnectionError("upstream token=supersecret123456")
        yield b""  # pragma: no cover - keeps this method an iterator.


class FakeSession:
    """FIFO request double used to prove redirects and retries deterministically."""

    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        """Initialize the deterministic FakeSession test double without live I/O."""
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        """Return the next response or raise the next simulated network error."""
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        """Record ownership cleanup when the downloader created the session."""
        self.closed = True


def _document(**overrides: object) -> IpoDocumentRecord:
    """Build one detached metadata-only filing record for downloader tests."""
    values: dict[str, object] = {
        "id": 11,
        "issue_id": 7,
        "document_type": "rhp",
        "document_url": DETAIL_URL,
        "source_url": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do",
        "source_confidence": Confidence.HIGH,
        "filing_date": dt.date(2026, 6, 30),
        "record_hash": "b" * 64,
        "content_sha256": None,
        "downloaded_at": None,
        "file_path": None,
        "page_count": None,
        "parse_status": IpoDocumentParseStatus.NOT_DOWNLOADED,
        "created_at": dt.datetime(2026, 6, 30, tzinfo=dt.UTC),
    }
    values.update(overrides)
    return IpoDocumentRecord(**values)


@pytest.mark.parametrize(
    "iframe_file",
    [PDF_URL, "/sebi_data/attachdocs/example.pdf"],
)
def test_detail_page_downloads_absolute_or_root_relative_iframe_pdf_atomically(
    tmp_path: Path,
    iframe_file: str,
) -> None:
    """Resolve both official iframe forms and persist bytes under their digest."""
    detail = (
        f'<iframe src="../../../web/?file={iframe_file}"></iframe>'
        '<a href="/abridged.pdf">Abridged Prospectus</a>'
    ).encode()
    html_response = FakeResponse(detail, content_type="text/html; charset=UTF-8")
    pdf_response = FakeResponse(PDF_BYTES)
    session = FakeSession([html_response, pdf_response])

    result = download_document_file(
        _document(),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
        sleeper=lambda _delay: None,
        now=lambda: dt.datetime(2026, 6, 30, 12, tzinfo=dt.UTC),
    )

    digest = hashlib.sha256(PDF_BYTES).hexdigest()
    assert result.content_sha256 == digest
    assert result.file_path == f"ipo/documents/{digest}.pdf"
    assert result.parse_status is IpoDocumentParseStatus.PENDING
    assert result.page_count is None
    assert result.bytes_written == len(PDF_BYTES)
    assert (tmp_path / result.file_path).read_bytes() == PDF_BYTES
    assert not list((tmp_path / "ipo" / "documents").glob("*.part"))
    assert [call[0] for call in session.calls] == [DETAIL_URL, PDF_URL]
    assert html_response.closed and pdf_response.closed


def test_direct_official_pdf_response_skips_html_resolution(tmp_path: Path) -> None:
    """Permit a filing URL that already responds with the prospectus PDF."""
    session = FakeSession([FakeResponse(PDF_BYTES)])

    result = download_document_file(
        _document(document_url=PDF_URL),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
    )

    assert result.bytes_written == len(PDF_BYTES)
    assert len(session.calls) == 1


def test_direct_pdf_response_must_use_the_official_attachment_path(
    tmp_path: Path,
) -> None:
    """A PDF media type must not turn an unrelated SEBI path into evidence."""
    session = FakeSession([FakeResponse(PDF_BYTES)])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(document_url=DETAIL_URL),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert session.calls[0][0] == DETAIL_URL


@pytest.mark.parametrize("content_type", ["text/html", "application/pdf"])
def test_stream_failure_uses_secret_safe_network_error_taxonomy(
    tmp_path: Path,
    content_type: str,
) -> None:
    """Lazy body failures should look like every other safe network failure."""
    response = FailingStreamResponse(PDF_BYTES, content_type=content_type)
    session = FakeSession([response])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(document_url=PDF_URL),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.NETWORK_ERROR
    assert "supersecret123456" not in str(caught.value)
    assert response.closed
    cache_dir = tmp_path / "ipo" / "documents"
    assert not cache_dir.exists() or not list(cache_dir.iterdir())


def test_verified_cache_hit_performs_no_http_request(tmp_path: Path) -> None:
    """Rehash the stored file before declaring a zero-network cache hit."""
    digest = hashlib.sha256(PDF_BYTES).hexdigest()
    relative_path = f"ipo/documents/{digest}.pdf"
    cached = tmp_path / relative_path
    cached.parent.mkdir(parents=True)
    cached.write_bytes(PDF_BYTES)
    session = FakeSession([])

    result = download_document_file(
        _document(
            content_sha256=digest,
            downloaded_at=dt.datetime(2026, 6, 30, tzinfo=dt.UTC),
            file_path=relative_path,
            parse_status=IpoDocumentParseStatus.PENDING,
        ),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
    )

    assert result.cache_hit is True
    assert result.bytes_written == len(PDF_BYTES)
    assert session.calls == []


@pytest.mark.parametrize(
    "document_url",
    [
        "http://www.sebi.gov.in/filings/example.html",
        "https://user:password@www.sebi.gov.in/filings/example.html",
        "https://www.sebi.gov.in:444/filings/example.html",
        "https://www.sebi.gov.in:invalid/filings/example.html",
        "https://[www.sebi.gov.in/filings/example.html",
        "https://sebi.gov.in.evil.example/filings/example.html",
    ],
)
def test_unsafe_document_url_fails_before_http(tmp_path: Path, document_url: str) -> None:
    """Reject SSRF-shaped filing URLs without giving the HTTP client a chance."""
    session = FakeSession([])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(document_url=document_url),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert session.calls == []
    assert document_url not in str(caught.value)


def test_unsupported_document_type_is_rejected_with_dedicated_code(tmp_path: Path) -> None:
    """Reject a non-DRHP/RHP document with its own code and no HTTP request."""
    session = FakeSession([])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(document_type="final_offer"),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSUPPORTED_DOCUMENT_TYPE
    assert session.calls == []


@pytest.mark.parametrize(
    "detail",
    [
        b"<html>No iframe here</html>",
        (
            f'<iframe src="/web/?file={PDF_URL}"></iframe>'
            f'<iframe src="/web/?file={PDF_URL}"></iframe>'
        ).encode(),
        b'<iframe src="/web/?file=https://evil.example/prospectus.pdf"></iframe>',
        b'<iframe src="/web/?file=/other/location/prospectus.pdf"></iframe>',
    ],
)
def test_hostile_or_ambiguous_iframe_target_fails_closed(
    tmp_path: Path,
    detail: bytes,
) -> None:
    """Never guess which PDF to trust when the detail page contract is unclear."""
    session = FakeSession([FakeResponse(detail, content_type="text/html")])

    with pytest.raises(IpoDocumentDownloadError):
        download_document_file(
            _document(),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(
            PDF_BYTES,
            headers={"Content-Length": str(MAX_PDF_BYTES + 1)},
        ),
        FakeResponse(b"not a pdf", content_type="application/pdf"),
        FakeResponse(PDF_BYTES, content_type="text/plain"),
    ],
)
def test_oversized_or_invalid_pdf_is_never_committed(
    tmp_path: Path,
    response: FakeResponse,
) -> None:
    """Apply size, media-type, and magic checks before cache publication."""
    session = FakeSession([response])

    with pytest.raises(IpoDocumentDownloadError):
        download_document_file(
            _document(document_url=PDF_URL),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    cache_dir = tmp_path / "ipo" / "documents"
    assert not cache_dir.exists() or not list(cache_dir.iterdir())
    assert response.closed


def test_transient_network_failure_retries_with_bounded_delay(tmp_path: Path) -> None:
    """Retry a temporary connection problem without exposing exception text."""
    delays: list[float] = []
    session = FakeSession([requests.Timeout("secret query"), FakeResponse(PDF_BYTES)])

    result = download_document_file(
        _document(document_url=PDF_URL),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
        sleeper=delays.append,
    )

    assert result.parse_status is IpoDocumentParseStatus.PENDING
    assert len(session.calls) == 2
    assert delays == [2.0]


def test_private_dns_answer_is_rejected_before_http(tmp_path: Path) -> None:
    """Treat a private DNS resolution as an SSRF failure even for an allowed name."""
    session = FakeSession([])

    def private_resolver(
        _host: str, port: int, **_kwargs: object
    ) -> list[tuple[object, ...]]:
        """Simulate DNS rebinding the allowed SEBI name to loopback."""
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(), data_dir=tmp_path, session=session, resolver=private_resolver
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert session.calls == []


def test_cross_host_redirect_is_rejected_and_response_is_closed(tmp_path: Path) -> None:
    """Revalidate every redirect hop instead of trusting requests' auto-following."""
    redirect = FakeResponse(
        b"",
        status_code=302,
        headers={"Location": "https://evil.example/prospectus.pdf"},
    )
    session = FakeSession([redirect])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(), data_dir=tmp_path, session=session, resolver=_public_resolver
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert redirect.closed


def test_pdf_redirect_cannot_escape_the_attachment_tree(tmp_path: Path) -> None:
    """Keep the stricter PDF-path policy active on every redirect hop.

    The detail page may point at a valid attachment URL which then redirects.
    Dropping ``require_pdf_path`` while following that redirect would let the
    trusted host move the fetch to an unrelated path after validation.
    """
    detail = FakeResponse(
        f'<iframe src="/web/?file={PDF_URL}"></iframe>'.encode(),
        content_type="text/html",
    )
    redirect = FakeResponse(
        b"",
        status_code=302,
        headers={"Location": "/sebi_data/private/secret.pdf"},
    )
    session = FakeSession([detail, redirect])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert [call[0] for call in session.calls] == [DETAIL_URL, PDF_URL]
    assert detail.closed and redirect.closed


def test_pdf_redirect_within_attachment_tree_remains_allowed(tmp_path: Path) -> None:
    """The redirect hardening must not block a normal in-tree PDF move."""
    redirected_pdf = "https://www.sebi.gov.in/sebi_data/attachdocs/final.pdf"
    detail = FakeResponse(
        f'<iframe src="/web/?file={PDF_URL}"></iframe>'.encode(),
        content_type="text/html",
    )
    redirect = FakeResponse(
        b"",
        status_code=302,
        headers={"Location": redirected_pdf},
    )
    session = FakeSession([detail, redirect, FakeResponse(PDF_BYTES)])

    result = download_document_file(
        _document(),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
    )

    assert result.bytes_written == len(PDF_BYTES)
    assert [call[0] for call in session.calls] == [DETAIL_URL, PDF_URL, redirected_pdf]


def test_redirected_detail_page_resolves_relative_iframe_from_final_url(
    tmp_path: Path,
) -> None:
    """Relative iframe paths belong to the page that actually returned HTML.

    Beginner note: a redirect can move a detail page into another directory.
    Resolving its relative links against the original pre-redirect URL invents
    a different resource and can reject a legitimate prospectus.
    """
    redirected_detail = (
        "https://www.sebi.gov.in/sebi_data/attachdocs/2026/detail.html"
    )
    relative_pdf = (
        "https://www.sebi.gov.in/sebi_data/attachdocs/2026/prospectus.pdf"
    )
    redirect = FakeResponse(
        b"",
        status_code=302,
        headers={"Location": redirected_detail},
    )
    detail = FakeResponse(
        b'<iframe src="viewer.html?file=prospectus.pdf"></iframe>',
        content_type="text/html",
    )
    session = FakeSession([redirect, detail, FakeResponse(PDF_BYTES)])

    result = download_document_file(
        _document(),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
    )

    assert result.bytes_written == len(PDF_BYTES)
    assert [call[0] for call in session.calls] == [
        DETAIL_URL,
        redirected_detail,
        relative_pdf,
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://[www.sebi.gov.in/file.pdf",
        "https://www.sebi.gov.in:invalid/file.pdf",
    ],
)
def test_malformed_redirect_has_safe_url_error_and_closes_response(
    tmp_path: Path,
    location: str,
) -> None:
    """Malformed redirect syntax stays inside the downloader error taxonomy."""
    redirect = FakeResponse(b"", status_code=302, headers={"Location": location})

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(),
            data_dir=tmp_path,
            session=FakeSession([redirect]),
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert redirect.closed


@pytest.mark.parametrize(
    "iframe_source",
    [
        "https://[www.sebi.gov.in/web/?file=/sebi_data/attachdocs/demo.pdf",
        "https://www.sebi.gov.in:invalid/web/?file=/sebi_data/attachdocs/demo.pdf",
    ],
)
def test_malformed_iframe_wrapper_has_invalid_detail_page_error(
    tmp_path: Path,
    iframe_source: str,
) -> None:
    """Broken wrapper syntax is a bad detail page, not a raw parser exception."""
    detail = FakeResponse(
        f'<iframe src="{iframe_source}"></iframe>'.encode(),
        content_type="text/html",
    )

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(),
            data_dir=tmp_path,
            session=FakeSession([detail]),
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.INVALID_DETAIL_PAGE
    assert detail.closed


@pytest.mark.parametrize(
    "iframe_file",
    [
        "/sebi_data/attachdocs/../secret.pdf",
        "/sebi_data/attachdocs/%2e%2e/secret.pdf",
        "/sebi_data/attachdocs/%252e%252e/secret.pdf",
        "/sebi_data/attachdocs/demo%252f..%252fsecret.pdf",
    ],
)
def test_iframe_pdf_target_rejects_encoded_path_confusion_before_http(
    tmp_path: Path,
    iframe_file: str,
) -> None:
    """A hostile iframe target must never become the download request."""
    detail = FakeResponse(
        f'<iframe src="/web/?file={iframe_file}"></iframe>'.encode(),
        content_type="text/html",
    )
    session = FakeSession([detail])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_URL
    assert [call[0] for call in session.calls] == [DETAIL_URL]
    assert detail.closed


def test_terminal_http_error_closes_response(tmp_path: Path) -> None:
    """Release the connection even when a non-retryable response cannot be used."""
    response = FakeResponse(b"not found", status_code=404, content_type="text/html")

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(),
            data_dir=tmp_path,
            session=FakeSession([response]),
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.HTTP_ERROR
    assert response.closed


def test_corrupt_cache_entry_is_removed_and_downloaded_again(tmp_path: Path) -> None:
    """Never return bytes whose current digest disagrees with stored provenance."""
    expected_digest = hashlib.sha256(PDF_BYTES).hexdigest()
    relative_path = f"ipo/documents/{expected_digest}.pdf"
    cached = tmp_path / relative_path
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"corrupt")
    session = FakeSession([FakeResponse(PDF_BYTES)])

    result = download_document_file(
        _document(
            document_url=PDF_URL,
            content_sha256=expected_digest,
            downloaded_at=dt.datetime(2026, 6, 30, tzinfo=dt.UTC),
            file_path=relative_path,
            parse_status=IpoDocumentParseStatus.PENDING,
        ),
        data_dir=tmp_path,
        session=session,
        resolver=_public_resolver,
    )

    assert result.cache_hit is False
    assert cached.read_bytes() == PDF_BYTES
    assert len(session.calls) == 1


@pytest.mark.parametrize("unsafe_path", ["../outside.pdf", "C:/outside.pdf"])
def test_untrusted_stored_cache_path_is_rejected_without_http(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    """Treat database paths as untrusted input and contain them beneath DATA_DIR."""
    session = FakeSession([])

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(
                content_sha256="d" * 64,
                downloaded_at=dt.datetime(2026, 6, 30, tzinfo=dt.UTC),
                file_path=unsafe_path,
                parse_status=IpoDocumentParseStatus.PENDING,
            ),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH
    assert session.calls == []


def test_cache_directory_is_containment_checked_before_pdf_bytes_are_streamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an unsafe cache directory before a temporary file receives bytes.

    A final-file containment check is too late: by then a large response may
    already have been written through a hostile directory link. This test makes
    the ordering explicit even on Windows hosts that cannot create symlinks.
    """
    response = FakeResponse(PDF_BYTES)
    session = FakeSession([response])

    def reject_cache_directory(_data_dir: Path, relative_value: str) -> Path:
        """Model the shared containment helper rejecting the cache directory."""
        assert relative_value == "ipo/documents"
        raise IpoDocumentDownloadError(IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH)

    monkeypatch.setattr(
        "backend.ipo.documents.downloader._contained_cache_path",
        reject_cache_directory,
    )

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(document_url=PDF_URL),
            data_dir=tmp_path,
            session=session,
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH
    assert response.iterated is False
    assert response.closed is True


def test_existing_cache_directory_symlink_is_rejected_before_writing(
    tmp_path: Path,
) -> None:
    """Never write a temporary PDF through a cache-directory symlink."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    cache_parent = tmp_path / "ipo"
    cache_parent.mkdir()
    cache_link = cache_parent / "documents"
    try:
        cache_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows host does not permit directory symlinks.")
    response = FakeResponse(PDF_BYTES)

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document_file(
            _document(document_url=PDF_URL),
            data_dir=tmp_path,
            session=FakeSession([response]),
            resolver=_public_resolver,
        )

    assert caught.value.code is IpoDocumentDownloadErrorCode.UNSAFE_CACHE_PATH
    assert response.iterated is False
    assert list(outside.iterdir()) == []
