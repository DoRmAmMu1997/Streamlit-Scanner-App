from __future__ import annotations

import requests
import pytest

from backend.sixty_seven.search_client import (
    SerpApiClient,
    SerpApiSearchError,
    SerpApiSetupError,
)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse | Exception):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, *, params, timeout):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
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
