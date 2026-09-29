"""Offline regressions for transcript URL validation before any HTTP request."""

from __future__ import annotations

import socket
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import requests

from backend.fundamentals import pdf_reader, pdf_transport


class RecordingSession(requests.Session):
    """Record attempted destinations without opening a socket.

    Beginner note: a rejected URL must never reach this transport. Checking only
    the returned result would miss SSRF that already happened before rejection.
    """

    def __init__(self, responses: list[requests.Response]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[tuple[str | bytes, dict[str, Any]]] = []

    def __call__(self, target, **kwargs):
        self.calls.append((target.url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str | bytes, **kwargs: Any) -> requests.Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def response(status: int = 200, *, location: str | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = b"%PDF-1.4 offline"
    # Beginner note: requests sets this private marker at runtime, but its
    # published stubs do not expose it. ``setattr`` keeps this fixture aligned
    # with that runtime seam without weakening typing for production code.
    object.__setattr__(result, "_content_consumed", True)
    result.headers["Content-Type"] = "application/pdf"
    if location is not None:
        result.headers["Location"] = location
    return result


def test_injected_session_cannot_skip_private_dns(tmp_path: Path, monkeypatch, caplog):
    """A supplied Session is never authority to waive destination validation."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    ])
    session = RecordingSession([response()])
    assert pdf_reader.download_pdf("https://public-looking.example/file.pdf", cache_dir=tmp_path,
                                   session=session) is None
    assert session.calls == []
    assert "Session injection is unsupported" in caplog.text


def test_redirect_is_followed_manually_after_public_validation(tmp_path: Path, monkeypatch):
    """Removing the manual redirect loop loses a valid public transcript."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    ])
    session = RecordingSession([response(302, location="../final.pdf"), response()])
    result = pdf_reader.download_pdf("https://example.com/docs/start.pdf", cache_dir=tmp_path, transport=session)
    assert result is not None
    assert [url for url, _ in session.calls] == [
        "https://example.com/docs/start.pdf", "https://example.com/final.pdf"
    ]
    assert all(options["timeout"] == 30 for _, options in session.calls)


@pytest.mark.parametrize("destination", ["http://127.0.0.1/secret", "http://169.254.169.254/metadata"])
def test_redirect_to_internal_address_has_zero_target_requests(tmp_path: Path, monkeypatch, destination: str):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    ])
    session = RecordingSession([response(302, location=destination), response()])
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path, transport=session) is None
    assert [url for url, _ in session.calls] == ["https://example.com/file.pdf"]
    assert len(session.responses) == 1


@pytest.mark.parametrize("answers", [[], ["8.8.8.8", "10.0.0.1"], ["::1"], ["224.0.0.1"], ["bad"]])
def test_bad_dns_has_zero_transport_requests(tmp_path: Path, answers):
    """A mixed answer set must not permit selecting only its public member."""
    transport = RecordingSession([response()])
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: answers, transport=transport) is None
    assert transport.calls == []


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/file", "https://user:pass@example.com/file",
    "https://example.com:99999/file", "https://example.com:0/file", "http://[fe80::1%25eth0]/",
    "https://example.com\\@127.0.0.1/file", "\nhttps://example.com/file", "https://%31%32%37.0.0.1/file",
])
def test_malformed_initial_destination_has_zero_requests(tmp_path: Path, url: str):
    transport = RecordingSession([response()])
    assert pdf_reader.download_pdf(url, cache_dir=tmp_path, resolver=lambda *_: ["8.8.8.8"],
                                   transport=transport) is None
    assert transport.calls == []


@pytest.mark.parametrize("location", [None, "", " ", "\n/next.pdf", "file:///etc/passwd",
                                      "https://example.com:bad/file", "http://10.0.0.1/file", "/file.pdf"])
def test_missing_invalid_or_looping_redirect_stops_before_next_request(tmp_path: Path, location):
    transport = RecordingSession([response(302, location=location), response()])
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: ["8.8.8.8"], transport=transport) is None
    assert len(transport.calls) == 1


def test_fourth_redirect_is_not_requested(tmp_path: Path):
    transport = RecordingSession([response(302, location=f"/{n}.pdf") for n in range(1, 6)])
    assert pdf_reader.download_pdf("https://example.com/0.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: ["8.8.8.8"], transport=transport) is None
    assert len(transport.calls) == 4


def test_three_redirects_can_complete_and_each_response_closes(tmp_path: Path, monkeypatch):
    responses = [response(302, location=f"/{n}.pdf") for n in range(1, 4)] + [response()]
    closed: list[int] = []
    for index, item in enumerate(responses):
        monkeypatch.setattr(item, "close", lambda index=index: closed.append(index))
    transport = RecordingSession(responses.copy())
    result = pdf_reader.download_pdf("https://example.com/0.pdf", cache_dir=tmp_path,
                                     resolver=lambda *_: ["8.8.8.8"], transport=transport)
    assert result is not None
    assert closed == [0, 1, 2, 3]


def test_default_transport_uses_pinned_ip_original_tls_host_and_no_environment(tmp_path: Path, monkeypatch):
    """Inspect the real adapter/pool down to its connection without networking.

    Beginner note: a mocked Session.get would not prove pinning. Intercepting
    urllib3's pool request leaves requests' preparation and adapter selection
    intact, and constructing its connection verifies the eventual socket host.
    """
    from io import BytesIO

    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.response import HTTPResponse

    observed = []
    sessions = []
    real_session = requests.Session

    def session_factory():
        session = real_session()
        sessions.append(session)
        return session

    def urlopen(pool, method, url, **kwargs):
        conn = pool._new_conn()
        assert pool.cert_reqs == "CERT_REQUIRED"
        assert pool.port == 8443
        observed.append((pool.host, conn.host, conn.server_hostname, conn.assert_hostname, kwargs))
        return HTTPResponse(body=BytesIO(b"%PDF-1.4 safe"), status=200,
                            headers={"Content-Type": "application/pdf"}, preload_content=False)

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "must-not-be-used")
    monkeypatch.setattr(pdf_transport.requests, "Session", session_factory)
    monkeypatch.setattr(HTTPSConnectionPool, "urlopen", urlopen)
    result = pdf_reader.download_pdf("https://example.com:8443/file.pdf", cache_dir=tmp_path,
                                     resolver=lambda *_: ["8.8.8.8"])
    assert result is not None
    assert len(observed) == 1
    pool_host, socket_host, sni, certificate_host, options = observed[0]
    assert pool_host == socket_host == "8.8.8.8"
    assert sni == certificate_host == "example.com"
    assert options["headers"]["Host"] == "example.com:8443"
    assert options["redirect"] is False
    assert sessions[0].trust_env is False
    assert sessions[0].proxies == {}
    assert options["timeout"].connect_timeout == 30


def test_dns_change_after_validation_cannot_change_socket_destination(monkeypatch):
    """The socket creation receives a numeric IP after the DNS answer changes."""
    from urllib3.util import connection

    target = pdf_transport.resolve_public_target("https://example.com/file", resolver=lambda *_: ["8.8.8.8"])
    adapter = pdf_transport._PinnedAdapter(target)
    request = requests.Request("GET", target.url).prepare()
    pool = adapter.get_connection_with_tls_context(request, True, {})
    destinations = []
    sentinel = object()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("Hostname resolved a second time"))

    def connect(address, *args, **kwargs):
        destinations.append(address)
        return sentinel

    monkeypatch.setattr(connection, "create_connection", connect)
    # Beginner note: urllib3's runtime connection has this private hook, while
    # the public stub only promises the base connection interface. Accessing it
    # through ``getattr`` models the narrow fixture-only seam explicitly.
    assert object.__getattribute__(pool._new_conn(), "_new_conn")() is sentinel
    assert destinations == [("8.8.8.8", 443)]
    adapter.close()


def test_dns_failure_closes_previous_response_and_writes_nothing(tmp_path: Path, monkeypatch):
    first = response(302, location="https://other.example/file.pdf")
    closed = []
    monkeypatch.setattr(first, "close", lambda: closed.append(True))
    transport = RecordingSession([first])

    def resolver(host, port):
        if host == "other.example":
            raise socket.gaierror("offline failure")
        return ["8.8.8.8"]

    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=resolver, transport=transport) is None
    assert closed == [True]
    assert len(transport.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_production_redirect_does_not_read_unbounded_redirect_body(tmp_path: Path, monkeypatch):
    """Even allow_redirects=False in Session.get eagerly reads redirect content.

    Beginner note: the PDF byte cap is ineffective if requests consumes an
    unlimited redirect body before returning control to our manual hop loop.
    Intercepting only the adapter response exercises that hidden Session path.
    """
    from io import BytesIO

    from urllib3.response import HTTPResponse

    class ForbiddenBody(BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail("Redirect body must not be consumed")

    seen = []

    def send(adapter, request, **kwargs):
        seen.append(request.url)
        item = response(302, location="/final.pdf") if len(seen) == 1 else response()
        if len(seen) == 1:
            # Beginner note: ``False`` is requests' runtime sentinel meaning
            # that the response body has not been eagerly consumed. The stub
            # types the field as bytes, so these fixture-only private seams use
            # setattr while preserving the production behavior under test.
            object.__setattr__(item, "_content", False)
            object.__setattr__(item, "_content_consumed", False)
            item.raw = HTTPResponse(body=ForbiddenBody(b"unbounded"), preload_content=False)
        return item

    monkeypatch.setattr(pdf_transport._PinnedAdapter, "send", send)
    result = pdf_reader.download_pdf("https://example.com/start.pdf", cache_dir=tmp_path,
                                     resolver=lambda *_: ["8.8.8.8"])
    assert result is not None
    assert seen == ["https://example.com/start.pdf", "https://example.com/final.pdf"]


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_public_ipv6_literal_is_pinned_without_dns(scheme: str):
    target = pdf_transport.resolve_public_target(
        f"{scheme}://[2606:4700:4700::1111]/file.pdf",
        resolver=lambda *_: pytest.fail("Literal addresses do not need DNS"),
    )
    assert target.address == "2606:4700:4700::1111"
    assert target.host_header == "[2606:4700:4700::1111]"


def test_tls_failure_closes_owned_session_without_writing(tmp_path: Path, monkeypatch):
    closed = []
    original_close = requests.Session.close

    def close(session):
        closed.append(True)
        original_close(session)

    def send(*args, **kwargs):
        raise requests.exceptions.SSLError("certificate mismatch")

    monkeypatch.setattr(requests.Session, "close", close)
    monkeypatch.setattr(pdf_transport._PinnedAdapter, "send", send)
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: ["8.8.8.8"]) is None
    assert closed == [True]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["size", "type", "stream"])
def test_rejected_pdf_closes_response_and_never_publishes(tmp_path: Path, monkeypatch, failure):
    first = response()
    closed = []
    monkeypatch.setattr(first, "close", lambda: closed.append(True))
    if failure == "size":
        monkeypatch.setattr(pdf_reader, "_MAX_PDF_BYTES", 5)
    elif failure == "type":
        first.headers["Content-Type"] = "text/html"
    else:
        def fail_stream(**kwargs):
            raise requests.exceptions.ChunkedEncodingError("truncated")
        monkeypatch.setattr(first, "iter_content", fail_stream)
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: ["8.8.8.8"], transport=RecordingSession([first])) is None
    assert closed == [True]
    assert list(tmp_path.iterdir()) == []


def test_adapter_rejects_changed_url_verification_or_proxy():
    target = pdf_transport.resolve_public_target("https://example.com/file.pdf", resolver=lambda *_: ["8.8.8.8"])
    with closing(pdf_transport._PinnedAdapter(target)) as adapter:
        request = requests.Request("GET", target.url).prepare()
        for verify, proxies in [(False, {}), (True, {"https": "http://127.0.0.1"})]:
            with pytest.raises(requests.RequestException):
                adapter.get_connection_with_tls_context(request, verify, proxies)
        request.url = "https://other.example/file.pdf"
        with pytest.raises(requests.RequestException):
            adapter.get_connection_with_tls_context(request, True, {})


def test_unreachable_first_address_falls_back_to_next_validated_address(tmp_path: Path, monkeypatch):
    """Pinning must not turn one unreachable DNS answer into a lost transcript.

    Beginner note: the old requests path tried every getaddrinfo answer. Each
    fallback here is still a validated public address, and TLS still checks the
    original hostname, so failover adds availability without widening egress.
    """
    attempted: list[str] = []

    def send(adapter, request, **kwargs):
        attempted.append(adapter.target.address)
        if adapter.target.address == "2606:4700:4700::1111":
            raise requests.exceptions.ConnectionError("network unreachable")
        return response()

    monkeypatch.setattr(pdf_transport._PinnedAdapter, "send", send)
    result = pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                     resolver=lambda *_: ["2606:4700:4700::1111", "8.8.8.8"])
    assert result is not None
    assert attempted == ["2606:4700:4700::1111", "8.8.8.8"]


def test_address_fallback_is_bounded_and_fails_closed(tmp_path: Path, monkeypatch):
    attempted: list[str] = []

    def send(adapter, request, **kwargs):
        attempted.append(adapter.target.address)
        raise requests.exceptions.ConnectTimeout("timed out")

    monkeypatch.setattr(pdf_transport._PinnedAdapter, "send", send)
    answers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"]
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: answers) is None
    assert attempted == answers[:3]
    assert list(tmp_path.iterdir()) == []


def test_slow_drip_download_stops_at_overall_deadline(tmp_path: Path, monkeypatch):
    """Per-read timeouts alone let a hostile host drip bytes for hours."""
    from types import SimpleNamespace

    ticks = iter(range(0, 100_000, 50))
    monkeypatch.setattr(pdf_reader, "time", SimpleNamespace(monotonic=lambda: next(ticks)), raising=False)
    first = response()
    monkeypatch.setattr(first, "iter_content", lambda **_: iter([b"%PDF-"] + [b"x"] * 100))
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: ["8.8.8.8"], transport=RecordingSession([first])) is None
    assert list(tmp_path.iterdir()) == []


def test_failed_cache_publish_leaves_no_partial_pdf(tmp_path: Path, monkeypatch):
    """A truncated cache file would otherwise be served as a hit forever."""
    import os

    def fail_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    assert pdf_reader.download_pdf("https://example.com/file.pdf", cache_dir=tmp_path,
                                   resolver=lambda *_: ["8.8.8.8"], transport=RecordingSession([response()])) is None
    assert list(tmp_path.iterdir()) == []


def test_read_recent_concall_text_forwards_trusted_transport_seams(tmp_path: Path, monkeypatch):
    transport = RecordingSession([response()])
    monkeypatch.setattr(pdf_reader, "extract_text", lambda *_a, **_k: "transcript body")
    text = pdf_reader.read_recent_concall_text(
        [{"transcript_url": "https://example.com/t.pdf"}], cache_dir=tmp_path,
        resolver=lambda *_: ["8.8.8.8"], transport=transport,
    )
    assert text == "transcript body"
    assert len(transport.calls) == 1
