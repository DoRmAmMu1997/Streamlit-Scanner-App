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

from dataclasses import dataclass
from typing import Any

import requests

from backend.config import get_settings


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
        configured_key = get_settings().serpapi_api_key if api_key is None else api_key
        self.api_key = (configured_key or "").strip()
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

        params = {
            "engine": "google",
            "q": normalized_query,
            # India-localized, English results: gl = geo-location country,
            # hl = host/UI language. The strategy is about NSE-listed companies.
            "gl": "in",
            "hl": "en",
            "api_key": self.api_key,
            "output": "json",
        }
        try:
            response = self.session.get(
                self.ENDPOINT,
                params=params,
                timeout=self.TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            # A requests error can echo the full request URL — including the
            # api_key query param — so scrub the key out of the message before it
            # reaches logs or the UI. (app._redact_secrets is a second layer.)
            detail = str(exc).replace(self.api_key, "***") if self.api_key else str(exc)
            raise SerpApiSearchError(f"SerpAPI request failed: {detail}") from exc
        except ValueError as exc:
            # response.json() raises ValueError/JSONDecodeError on a non-JSON body.
            raise SerpApiSearchError("SerpAPI returned non-JSON data.") from exc

        # SerpAPI reports API-level problems (bad key, quota) in an "error" field
        # with HTTP 200, so check the body even though raise_for_status() passed.
        if isinstance(payload, dict) and payload.get("error"):
            raise SerpApiSearchError(str(payload["error"]))

        organic = payload.get("organic_results", []) if isinstance(payload, dict) else []
        if not isinstance(organic, list):
            return []

        results: list[SearchResult] = []
        for item in organic[: max(0, int(max_results))]:
            if not isinstance(item, dict):
                continue
            result = _normalize_result(normalized_query, item)
            if result is not None:
                results.append(result)
        return results


def _normalize_result(query: str, item: dict[str, Any]) -> SearchResult | None:
    """Coerce one raw SerpAPI organic-result dict into a tidy `SearchResult`.

    SerpAPI fields vary by result, so every field is defensively coerced to a
    stripped string. A result with neither a title nor a snippet carries no
    evidence, so it is dropped (returns None).
    """
    title = str(item.get("title") or "").strip()
    link = str(item.get("link") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    if not title and not snippet:
        return None
    return SearchResult(
        query=query,
        title=title,
        link=link,
        source=str(item.get("displayed_link") or item.get("source") or "").strip(),
        snippet=snippet,
        date=str(item.get("date") or "").strip(),
    )
