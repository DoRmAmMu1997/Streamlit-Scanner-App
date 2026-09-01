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
    """Raised when SerpAPI cannot return usable search results.

    Beginner note — why the subclasses below exist:
        Callers log an exception's *class name* and never its message, because
        a provider message is untrusted upstream text. A single flat type
        therefore made every failure read identically in the logs: a quota that
        will not reset until next month looked exactly like a two-second
        network blip. The subclasses give the log something that can actually
        vary, which is the same fix ``SebiBlockedError`` applies to SEBI.

        They all inherit from this class, so an existing
        ``except SerpApiSearchError`` keeps catching everything it used to.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Record the HTTP status alongside the redacted message.

        A status code is safe metadata (unlike a response body), so it can be
        logged verbatim to tell a 429 apart from a 500.
        """
        super().__init__(message)
        self.status_code = status_code


class SerpApiQuotaError(SerpApiSearchError):
    """Raised when the SerpAPI plan has no searches left.

    Permanent for the billing period: re-running cannot help, and a caller that
    keeps issuing searches only wastes wall-clock time.
    """


class SerpApiRateLimitError(SerpApiSearchError):
    """Raised when SerpAPI throttles a burst of requests.

    Transient, unlike :class:`SerpApiQuotaError` — the same query may succeed
    after a pause, so a caller may reasonably continue with other work.
    """


class SerpApiAuthError(SerpApiSearchError):
    """Raised when SerpAPI rejects the credentials (HTTP 401/403).

    A configuration problem: every subsequent call will fail the same way until
    the key is fixed.
    """


# SerpAPI answers a query Google found nothing for with HTTP 200 and one of
# these exact ``error`` strings. This is an empty result set, not an outage.
#
# Beginner note:
#     These are complete normalized messages rather than substrings. A response
#     such as "...no results... account disabled" carries additional meaning
#     and must stay an error. The caller also verifies the HTTP status before
#     applying this allowlist, so body prose can never turn a 401 into success.
_NO_RESULTS_MESSAGES: Final = frozenset(
    {
        "google hasn't returned any results for this query.",
        "google has not returned any results for this query.",
    }
)
# Quota exhaustion wording, checked against the provider's error text.
#
# Every marker here must be unambiguously TERMINAL. A bare "search limit" was
# tried and removed: "you have exceeded your hourly search limit" is a throttle
# that clears on its own, and matching it classified a recoverable pause as a
# spent plan and aborted the whole enrichment stage. Ambiguous wording falls
# through to the status-based classifier, which reads it as transient.
_QUOTA_MARKERS: Final = (
    "run out of searches",
    "exceeded your searches",
    "account has no searches",
    "monthly search limit",
)


def _classify_status(message: str, status_code: int | None) -> SerpApiSearchError:
    """Pick the error type an HTTP status alone justifies.

    Beginner note:
        A 429 without a readable body is ambiguous — it could be a burst
        throttle or an exhausted plan — so it is reported as the *transient* of
        the two. Claiming exhaustion on thin evidence would stop a run that
        could have continued; the reverse merely lets it finish.
    """
    if status_code in (401, 403):
        return SerpApiAuthError(message, status_code=status_code)
    if status_code == 429:
        return SerpApiRateLimitError(message, status_code=status_code)
    return SerpApiSearchError(message, status_code=status_code)


def _classify_provider_error(
    folded: str, status_code: int | None
) -> SerpApiSearchError:
    """Pick the error type for a response whose body names the problem.

    The body is authoritative where it is explicit: SerpAPI says outright when
    an account is out of searches, which upgrades an otherwise ambiguous 429
    from "throttled, try later" to "spent, nothing will work until it resets".

    Beginner note:
        Provider prose is used only as a private classification input. It never
        becomes the exception message because this shared client also feeds an
        AI research tool; a fixed application-owned message prevents reflected
        queries, secrets, or instructions from crossing that model boundary.
    """
    # Auth status wins because it is already unambiguous. Other APIs sometimes
    # return application-level errors with HTTP 200, so explicit terminal quota
    # wording remains authoritative for success/429/other generic statuses.
    if status_code in (401, 403):
        return _classify_status("SerpAPI rejected the request.", status_code)
    if any(marker in folded for marker in _QUOTA_MARKERS):
        return SerpApiQuotaError(
            "SerpAPI quota is exhausted.", status_code=status_code
        )
    return _classify_status("SerpAPI rejected the request.", status_code)


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

        params: dict[str, str | int] = {
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
            # Read the body BEFORE checking the status. SerpAPI reports plan
            # exhaustion as HTTP 429 carrying a JSON body that names the cause,
            # so raising on the status first threw that explanation away and
            # left quota exhaustion indistinguishable from any other 4xx. The
            # read is already bounded to 1 MiB, so this costs nothing.
            try:
                payload = _bounded_json(response)
            except SerpApiSearchError as exc:
                # An error response does not have to be JSON. Any CDN, proxy or
                # WAF in front of SerpAPI answers a 401/403/429/5xx with an HTML
                # page, and decoding that raises here -- before the status is
                # ever inspected. Re-raising through the classifier keeps the
                # taxonomy working for exactly the responses that made it
                # necessary, instead of collapsing them back into the bare base
                # class with no status.
                raise _classify_status(
                    str(exc), getattr(response, "status_code", None)
                ) from exc
            provider_error = (
                str(payload["error"])
                if isinstance(payload, dict) and payload.get("error")
                else ""
            )
            status_code = getattr(response, "status_code", None)
            if provider_error:
                # Collapse whitespace for exact provider-shape matching without
                # deleting punctuation or additional words that change meaning.
                folded = " ".join(provider_error.casefold().split())
                if (
                    status_code is not None
                    and 200 <= status_code < 300
                    and folded in _NO_RESULTS_MESSAGES
                ):
                    # Not a failure: Google simply had nothing for this query.
                    # Returning empty lets the caller persist an honest "no
                    # observations" record instead of dropping the signal.
                    _close_response(
                        response, api_key=self.api_key, suppress_errors=False
                    )
                    return []
                raise _classify_provider_error(folded, status_code)
            # No error field, so a non-2xx status is the only thing left that
            # can make this response unusable.
            response.raise_for_status()
        except requests.HTTPError:
            # A response-derived HTTPError can carry provider-controlled reason
            # prose. The numeric status is enough to classify it; copying or
            # chaining the exception would let that text reach the 67-ka model.
            status_code = getattr(response, "status_code", None)
            _close_response(response, api_key=self.api_key, suppress_errors=True)
            raise _classify_status(
                "SerpAPI request failed.", status_code
            ) from None
        except requests.RequestException as exc:
            # Streaming/transport failures are locally generated diagnostics,
            # not HTTP reason prose. Preserve their redacted detail so an
            # operator can distinguish timeout/reset/stream failures, while
            # cleanup still cannot replace the primary exception.
            detail = redact_text(str(exc), extra_secrets=[self.api_key])
            status_code = getattr(response, "status_code", None)
            _close_response(response, api_key=self.api_key, suppress_errors=True)
            raise _classify_status(
                f"SerpAPI request failed: {detail}", status_code
            ) from exc
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
    except BaseException as exc:
        if suppress_errors:
            return
        # Cancellation remains cancellation when cleanup is the only failure.
        if not isinstance(exc, Exception):
            raise
        detail = redact_text(str(exc), extra_secrets=[api_key])
        raise SerpApiSearchError(
            f"SerpAPI response cleanup failed: {detail}"
        ) from exc
