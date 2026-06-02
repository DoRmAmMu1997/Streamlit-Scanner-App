"""SerpAPI-backed Google result client for 67 ka funda research."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


class SerpApiSetupError(RuntimeError):
    """Raised when SerpAPI is not configured for live web research."""


class SerpApiSearchError(RuntimeError):
    """Raised when SerpAPI cannot return usable search results."""


@dataclass(frozen=True)
class SearchResult:
    """Normalized organic search evidence handed to the AI verifier."""

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
    """Small requests client for SerpAPI's Google search endpoint."""

    ENDPOINT = "https://serpapi.com/search"
    TIMEOUT_SECONDS = 20

    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("SERPAPI_API_KEY", "")).strip()
        self.session = session or requests.Session()

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise SerpApiSetupError(
                "SERPAPI_API_KEY is missing. Add it to Dependencies/.env or "
                "the process environment to enable 67 ka funda web research."
            )

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Return normalized Google organic results for one query."""
        self.ensure_ready()
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []

        params = {
            "engine": "google",
            "q": normalized_query,
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
            raise SerpApiSearchError(f"SerpAPI request failed: {exc}") from exc
        except ValueError as exc:
            raise SerpApiSearchError("SerpAPI returned non-JSON data.") from exc

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
