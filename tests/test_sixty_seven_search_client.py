from __future__ import annotations

import json

import pytest
import requests

from backend.sixty_seven.search_client import (
    SerpApiClient,
    SerpApiSearchError,
    SerpApiSetupError,
)

_ONE_MIB = 1024 * 1024


class _FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        status_code: int = 200,
        *,
        body: bytes | None = None,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        status_error: BaseException | None = None,
        stream_error: Exception | None = None,
        close_error: BaseException | None = None,
    ):
        self._payload = payload
        self._body = (
            json.dumps(payload).encode("utf-8") if body is None else body
        )
        self._chunks = chunks
        self._status_error = status_error
        self._stream_error = stream_error
        self._close_error = close_error
        self.status_code = status_code
        self.text = str(payload)
        self.headers = headers or {}
        self.iterated = False
        self.json_called = False
        self.closed = False

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        self.json_called = True
        return self._payload

    def iter_content(self, chunk_size: int):
        self.iterated = True
        if self._stream_error is not None:
            raise self._stream_error
        if self._chunks is not None:
            yield from self._chunks
            return
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset : offset + chunk_size]

    def close(self):
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FakeSession:
    def __init__(self, response: _FakeResponse | Exception):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, *, params, timeout, stream):
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "timeout": timeout,
                "stream": stream,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_serpapi_client_normalizes_organic_results():
    session = _FakeSession(
        _FakeResponse(
            {
                "organic_results": [
                    {
                        "title": "Demo Industries turnaround",
                        "link": "https://example.com/demo",
                        "displayed_link": "example.com",
                        "snippet": "Margins recovered after raw material pressure eased.",
                        "date": "May 2026",
                    },
                    {
                        "title": "Ignored third result",
                        "link": "https://example.com/ignored",
                        "snippet": "not returned because max_results=1",
                    },
                ]
            }
        )
    )

    client = SerpApiClient(api_key="secret", session=session)
    results = client.search("DEMO fall reason", max_results=1)

    assert len(results) == 1
    assert results[0].query == "DEMO fall reason"
    assert results[0].title == "Demo Industries turnaround"
    assert results[0].link == "https://example.com/demo"
    assert results[0].source == "example.com"
    assert results[0].snippet.startswith("Margins recovered")
    params = session.calls[0]["params"]
    assert params["engine"] == "google"
    assert params["q"] == "DEMO fall reason"
    assert params["gl"] == "in"
    assert params["hl"] == "en"
    assert params["api_key"] == "secret"
    assert params["num"] == 1
    assert session.calls[0]["stream"] is True


def test_serpapi_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    with pytest.raises(SerpApiSetupError):
        SerpApiClient(api_key="").search("DEMO")


def test_serpapi_client_raises_on_api_error_payload():
    session = _FakeSession(_FakeResponse({"error": "Invalid API key"}))

    with pytest.raises(SerpApiSearchError, match="Invalid API key"):
        SerpApiClient(api_key="secret", session=session).search("DEMO")


def test_serpapi_client_raises_on_network_error():
    session = _FakeSession(requests.Timeout("slow"))

    with pytest.raises(SerpApiSearchError, match="slow"):
        SerpApiClient(api_key="secret", session=session).search("DEMO")


def test_serpapi_client_redacts_api_key_from_network_error():
    """Requests exceptions can include a URL with the SerpAPI key query param."""
    session = _FakeSession(
        requests.Timeout(
            "GET https://serpapi.com/search?engine=google&api_key=serp-secret&q=DEMO"
        )
    )

    with pytest.raises(SerpApiSearchError) as exc_info:
        SerpApiClient(api_key="serp-secret", session=session).search("DEMO")

    message = str(exc_info.value)
    assert "serp-secret" not in message
    assert "***REDACTED***" in message


def test_serpapi_client_returns_empty_list_when_no_results():
    session = _FakeSession(_FakeResponse({"organic_results": []}))

    assert SerpApiClient(api_key="secret", session=session).search("DEMO") == []


def test_serpapi_client_rejects_advertised_oversized_response_before_reading():
    response = _FakeResponse(
        {"organic_results": []},
        headers={"Content-Length": str(_ONE_MIB + 1)},
    )

    with pytest.raises(SerpApiSearchError, match="response exceeded"):
        SerpApiClient(
            api_key="secret", session=_FakeSession(response)
        ).search("bounded")

    assert response.iterated is False
    assert response.json_called is False
    assert response.closed is True


@pytest.mark.parametrize(
    "headers",
    [{}, {"Content-Length": "unknown"}, {"Content-Length": "-1"}],
)
def test_serpapi_client_streams_when_content_length_is_missing_or_invalid(headers):
    response = _FakeResponse({"organic_results": []}, headers=headers)

    assert (
        SerpApiClient(api_key="secret", session=_FakeSession(response)).search(
            "bounded"
        )
        == []
    )
    assert response.iterated is True
    assert response.json_called is False


def test_serpapi_client_rejects_streamed_body_crossing_one_mib_before_decode():
    response = _FakeResponse(
        body=b"",
        chunks=[b"x" * _ONE_MIB, b"x"],
        headers={"Content-Length": "invalid"},
    )

    with pytest.raises(SerpApiSearchError, match="response exceeded"):
        SerpApiClient(
            api_key="secret", session=_FakeSession(response)
        ).search("bounded")

    assert response.iterated is True
    assert response.json_called is False


def test_serpapi_client_clamps_result_count_sent_to_provider():
    session = _FakeSession(_FakeResponse({"organic_results": []}))

    SerpApiClient(api_key="secret", session=session).search(
        "bounded", max_results=10_000
    )

    assert session.calls[0]["params"]["num"] == 10


def test_serpapi_client_accepts_only_strings_and_caps_each_result_field():
    long_text = "x" * 2_001
    session = _FakeSession(
        _FakeResponse(
            {
                "organic_results": [
                    {
                        "title": {"nested": "not evidence"},
                        "link": ["https://unsafe.example"],
                        "displayed_link": {"nested": "not evidence"},
                        "source": long_text,
                        "snippet": long_text,
                        "date": ["today"],
                    },
                    {"title": ["nested"], "snippet": {"nested": "text"}},
                ]
            }
        )
    )

    results = SerpApiClient(api_key="secret", session=session).search("bounded")

    assert len(results) == 1
    result = results[0]
    assert result.title == ""
    assert result.link == ""
    assert result.date == ""
    assert result.source == long_text[:2_000]
    assert result.snippet == long_text[:2_000]


def test_serpapi_client_reports_redacted_cleanup_error_after_successful_decode():
    secret = "serp-secret"
    response = _FakeResponse(
        {"organic_results": []},
        close_error=requests.ConnectionError(
            f"close failed for https://serpapi.com/?api_key={secret}"
        ),
    )

    with pytest.raises(SerpApiSearchError, match="cleanup failed") as exc_info:
        SerpApiClient(api_key=secret, session=_FakeSession(response)).search("DEMO")

    assert response.closed is True
    assert secret not in str(exc_info.value)
    assert "***REDACTED***" in str(exc_info.value)


def test_cleanup_failure_does_not_override_redacted_streaming_error():
    secret = "serp-secret"
    response = _FakeResponse(
        {"organic_results": []},
        stream_error=requests.Timeout(
            f"stream failed for https://serpapi.com/?api_key={secret}"
        ),
        close_error=requests.ConnectionError("cleanup replacement"),
    )

    with pytest.raises(SerpApiSearchError, match="stream failed") as exc_info:
        SerpApiClient(api_key=secret, session=_FakeSession(response)).search("DEMO")

    message = str(exc_info.value)
    assert response.closed is True
    assert "cleanup replacement" not in message
    assert secret not in message
    assert "***REDACTED***" in message


def test_cleanup_failure_does_not_override_primary_response_limit_error():
    response = _FakeResponse(
        body=b"",
        chunks=[b"x" * _ONE_MIB, b"x"],
        close_error=requests.ConnectionError("cleanup replacement"),
    )

    with pytest.raises(SerpApiSearchError, match="response exceeded") as exc_info:
        SerpApiClient(
            api_key="secret", session=_FakeSession(response)
        ).search("bounded")

    assert response.closed is True
    assert "cleanup replacement" not in str(exc_info.value)


def test_cleanup_is_attempted_without_overriding_cancellation():
    response = _FakeResponse(
        {"organic_results": []},
        status_error=KeyboardInterrupt(),
        close_error=requests.ConnectionError("cleanup replacement"),
    )

    with pytest.raises(KeyboardInterrupt):
        SerpApiClient(
            api_key="secret", session=_FakeSession(response)
        ).search("bounded")

    assert response.closed is True


@pytest.mark.parametrize(
    ("primary", "cleanup"),
    [
        (KeyboardInterrupt("primary keyboard"), SystemExit("cleanup system")),
        (SystemExit("primary system"), GeneratorExit("cleanup generator")),
        (GeneratorExit("primary generator"), KeyboardInterrupt("cleanup keyboard")),
    ],
)
def test_cleanup_base_exception_never_replaces_primary_cancellation(
    primary: BaseException,
    cleanup: BaseException,
) -> None:
    response = _FakeResponse(
        {"organic_results": []},
        status_error=primary,
        close_error=cleanup,
    )

    caught: BaseException | None = None
    try:
        SerpApiClient(
            api_key="secret", session=_FakeSession(response)
        ).search("bounded")
    except BaseException as exc:
        caught = exc

    assert response.closed is True
    assert caught is primary


@pytest.mark.parametrize(
    "cleanup",
    [
        KeyboardInterrupt("close keyboard"),
        SystemExit("close system"),
        GeneratorExit("close generator"),
    ],
)
def test_close_only_cancellation_propagates_unchanged(
    cleanup: BaseException,
) -> None:
    response = _FakeResponse(
        {"organic_results": []},
        close_error=cleanup,
    )

    caught: BaseException | None = None
    try:
        SerpApiClient(
            api_key="secret", session=_FakeSession(response)
        ).search("bounded")
    except BaseException as exc:
        caught = exc

    assert response.closed is True
    assert caught is cleanup
