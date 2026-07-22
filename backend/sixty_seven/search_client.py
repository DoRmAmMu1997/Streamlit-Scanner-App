"""SerpAPI-backed Google search client for 67 ka funda research.

Beginner note — what this is and why
------------------------------------
The "67 ka funda" AI verifier needs recent, real-world context about *why* a stock
fell 67%+ (news, quarterly results, sentiment). Rather than scrape Google directly
(fragile and against Google's terms), we go through **SerpAPI** — a paid API that
returns Google's organic results as clean JSON. This module is a tiny wrapper
around that single endpoint, and it is deliberately careful about two things:

- it only ever calls the fixed SerpAPI ``ENDPOINT`` (never an arbitrary URL), so
  there is no server-side request forgery (SSRF) surface here; result ``link``s
  are passed downstream as *data*, never fetched, and
- everything it returns is treated downstream as untrusted *evidence*, never as
  instructions (see the agent's system prompt and ``source_policy``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import requests

from backend.config import get_settings
from backend.security import redact_text

_MAX_RESPONSE_BYTES: Final = 1024 * 1024
_RESPONSE_CHUNK_BYTES: Final = 64 * 1024
_MAX_RESULTS: Final = 10
_MAX_RESULT_FIELD_CHARS: Final = 2_000


class SerpApiSetupError(RuntimeError):
    """Raised when SerpAPI is not configured for live web research."""


class SerpApiSearchError(RuntimeError):
    """Raised when SerpAPI cannot return usable search results."""


@dataclass(frozen=True)
class SearchResult:
    """One normalized Google organic result handed to the AI verifier as evidence.

    Frozen so a result can be safely passed around / cached. `to_dict()` is the
    JSON-friendly shape the agent tool embeds in its response.
    """

    query: str
    title: str
    link: str
    source: str
    snippet: str
    date: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "query": self.query,
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "snippet": self.snippet,
            "date": self.date,
        }


class SerpApiClient:
    """A small, dependency-light client for SerpAPI's Google Search endpoint.

    Construct once and reuse it (it keeps a pooled `requests.Session`). The API key
    comes from centralized settings unless one is passed in explicitly; the test
    suite injects a fake session instead of hitting the network.
    """

    # The ONLY endpoint this client talks to (a fixed URL → no SSRF surface).
    ENDPOINT = "https://serpapi.com/search"
    # Cap each request so one slow lookup cannot hang a whole scan.
    TIMEOUT_SECONDS = 20

    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        # Passing api_key explicitly is useful for tests and one-off scripts.
        # Normal app code passes nothing, so the key comes from the central
        # DEPLOY-004 settings object instead of this client reading os.environ.
        configured_key = get_settings().serpapi_api_key if api_key is None else api_key
        self.api_key = (configured_key or "").strip()
        # A requests.Session keeps HTTP connection pooling in one place and lets
        # tests inject a fake session so no live SerpAPI calls happen.
        self.session = session or requests.Session()

    def ensure_ready(self) -> None:
        """Fail fast with an actionable message when the SerpAPI key is missing.

        Called up front so a misconfiguration surfaces as clear guidance rather
        than a cryptic HTTP error deep inside a scan.
        """
        if not self.api_key:
            raise SerpApiSetupError(
                "SERPAPI_API_KEY is missing. Add it to Dependencies/.env or "
                "the process environment to enable 67 ka funda web research."
            )

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Return up to ``max_results`` normalized Google organic results.

        Returns an empty list for a blank query. Raises ``SerpApiSetupError`` when
        the key is missing and ``SerpApiSearchError`` on any network / API / decode
        failure, so callers (the screener) can degrade gracefully instead of
        crashing the whole scan.
        """
        self.ensure_ready()
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        result_limit = max(0, min(int(max_results), _MAX_RESULTS))
        if result_limit == 0:
            return []

        params = {
            "engine": "google",
            "q": normalized_query,
            # India-localized, English results: gl = geo-location country,
            # hl = host/UI language. The strategy is about NSE-listed companies.
            "gl": "in",
            "hl": "en",
            "api_key": self.api_key,
            "output": "json",
            "num": result_limit,
        }
        try:
            response = self.session.get(
                self.ENDPOINT,
                params=params,
                timeout=self.TIMEOUT_SECONDS,
                stream=True,
            )
        except requests.RequestException as exc:
            detail = redact_text(str(exc), extra_secrets=[self.api_key])
            raise SerpApiSearchError(f"SerpAPI request failed: {detail}") from exc

        try:
            response.raise_for_status()
            payload = _bounded_json(response)
            # API-level errors arrive with HTTP 200, so classify them before
            # cleanup and preserve them if closing the response also fails.
            if isinstance(payload, dict) and payload.get("error"):
                detail = redact_text(
                    str(payload["error"]), extra_secrets=[self.api_key]
                )
                raise SerpApiSearchError(detail)
        except requests.RequestException as exc:
            # A requests error can echo the full request URL — including the
            # api_key query param — so scrub through the same utility used by
            # Streamlit errors and scanner failure details.
            detail = redact_text(str(exc), extra_secrets=[self.api_key])
            _close_response(response, api_key=self.api_key, suppress_errors=True)
            raise SerpApiSearchError(f"SerpAPI request failed: {detail}") from exc
        except BaseException:
            # Cleanup must never replace a typed/redacted primary failure.
            _close_response(response, api_key=self.api_key, suppress_errors=True)
            raise
        else:
            _close_response(response, api_key=self.api_key, suppress_errors=False)

        organic = payload.get("organic_results", []) if isinstance(payload, dict) else []
        if not isinstance(organic, list):
            return []

        results: list[SearchResult] = []
        for item in organic[:result_limit]:
            if not isinstance(item, dict):
                continue
            result = _normalize_result(normalized_query, item)
            if result is not None:
                results.append(result)
        return results


def _normalize_result(query: str, item: dict[str, Any]) -> SearchResult | None:
    """Coerce one raw SerpAPI organic-result dict into a tidy `SearchResult`.

    SerpAPI fields vary by result, so only scalar strings cross this trust
    boundary and each is capped before downstream scanning or persistence. A
    result with neither a title nor a snippet carries no evidence, so it is
    dropped (returns None).
    """
    title = _bounded_result_field(item.get("title"))
    link = _bounded_result_field(item.get("link"))
    snippet = _bounded_result_field(item.get("snippet"))
    if not title and not snippet:
        return None
    source = _bounded_result_field(item.get("displayed_link"))
    if not source:
        source = _bounded_result_field(item.get("source"))
    return SearchResult(
        query=query,
        title=title,
        link=link,
        source=source,
        snippet=snippet,
        date=_bounded_result_field(item.get("date")),
    )


def _bounded_json(response: requests.Response) -> Any:
    """Read and decode one response only after enforcing a one-MiB byte cap.

    Beginner note:
        ``Content-Length`` can be absent or dishonest, so it is only an early
        rejection. Counting streamed bytes is the authoritative check and also
        covers transport-decoded content before JSON can expand into objects.
    """
    raw_length = response.headers.get("Content-Length")
    try:
        advertised_length = int(raw_length) if raw_length is not None else None
    except (TypeError, ValueError):
        advertised_length = None
    if advertised_length is not None and advertised_length > _MAX_RESPONSE_BYTES:
        raise SerpApiSearchError("SerpAPI response exceeded the 1 MiB limit.")

    body = bytearray()
    for chunk in response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES):
        if not chunk:
            continue
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise SerpApiSearchError("SerpAPI response exceeded the 1 MiB limit.")
        body.extend(chunk)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SerpApiSearchError("SerpAPI returned non-JSON data.") from exc


def _bounded_result_field(value: Any) -> str:
    """Return a stripped, bounded provider string without coercing nested data."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:_MAX_RESULT_FIELD_CHARS]


def _close_response(
    response: requests.Response,
    *,
    api_key: str,
    suppress_errors: bool,
) -> None:
    """Close a streamed response without allowing cleanup to mask failures."""
    try:
        response.close()
    except Exception as exc:
        if suppress_errors:
            return
        detail = redact_text(str(exc), extra_secrets=[api_key])
        raise SerpApiSearchError(
            f"SerpAPI response cleanup failed: {detail}"
        ) from exc
