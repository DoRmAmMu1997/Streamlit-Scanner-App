"""Centralized secret redaction for logs, UI errors, and stored messages.

Beginner note:
Redaction has two jobs in this app:

1. Mask secrets we already know from configuration, such as Dhan tokens,
   SerpAPI keys, and database URLs.
2. Mask common secret-looking shapes that may arrive inside exception text,
   such as ``access_token=...`` or ``Authorization: Bearer ...``.

This is a safety net, not a replacement for careful error handling. Callers
should still avoid putting credentials in messages they control, but this module
keeps third-party SDK exceptions from accidentally becoming UI/log leaks.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable
from typing import Any

MASK = "***REDACTED***"

# Keep this list intentionally small and high-signal. Including vague phrases
# like "api key" would hide useful messages such as "Invalid API key", so the
# pattern focuses on assignment/header/query-param shapes that contain values.
# The nosec below suppresses Bandit's hardcoded-password heuristic because this
# is only a regex vocabulary list, not an actual credential.
_SECRET_NAME = (
    r"api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"  # nosec B105
    r"token|client[_-]?secret|password|passwd|pwd|secret"
)
# ``sep`` allows an optional quote on each side of the ``:``/``=`` so quoted and
# JSON/dict shapes like ``{"access_token": "..."}`` or ``'password': '...'`` are
# masked too, not just bare ``access_token=...`` assignments. Such JSON error
# bodies are a common shape in third-party SDK exceptions.
_KEY_VALUE_RE = re.compile(
    rf"(?i)\b(?P<key>{_SECRET_NAME})(?P<sep>[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\"'\s&),;]+)"
)
_AUTHORIZATION_BEARER_RE = re.compile(
    r"(?i)(Authorization\s*:\s*Bearer\s+)(?P<value>[A-Za-z0-9._~+/=-]+)"
)
_DATABASE_URL_PASSWORD_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)"
    r"(?P<password>[^@\s/]+)"
    r"(?P<suffix>@)",
    re.IGNORECASE,
)


def redact_text(text: Any, *, extra_secrets: Iterable[str] | None = None) -> Any:
    """Return ``text`` with configured secrets and common token patterns masked.

    ``extra_secrets`` lets a caller add values that do not live in process
    environment variables. The Streamlit app uses that for OIDC cookie/client
    secrets because those are stored in ``st.secrets`` instead of DEPLOY-004
    settings.

    Non-string values are returned unchanged. That keeps this helper easy to use
    in defensive UI/error paths where the input might already be ``None``.
    """
    if not isinstance(text, str) or not text:
        return text

    # Order matters. Exact configured values go first because they are the most
    # reliable signal we have: if the full Dhan token or database URL appears in
    # the text, replace that exact value before broader regexes start operating
    # on smaller pieces of the same string.
    redacted = _redact_known_values(text, extra_secrets=extra_secrets)
    # Database URLs hide the password between ``user:`` and ``@host`` rather
    # than behind a ``password=...`` key. This pass preserves the scheme, user,
    # host, and database name so operators still know which connection failed.
    redacted = _DATABASE_URL_PASSWORD_RE.sub(
        rf"\g<prefix>{MASK}\g<suffix>",
        redacted,
    )
    # Authorization headers are another common shape that does not look like a
    # normal key/value assignment. Keep the header name and auth scheme visible;
    # only the bearer value is sensitive.
    redacted = _AUTHORIZATION_BEARER_RE.sub(
        lambda match: f"{match.group(1)}{MASK}",
        redacted,
    )
    # Finally handle generic key/value and query-string tokens such as
    # ``api_key=...`` and ``client_secret: ...``. This is intentionally last so
    # the more specific URL/header redactions have already preserved useful
    # surrounding context.
    redacted = _KEY_VALUE_RE.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{MASK}",
        redacted,
    )
    return redacted


def redact_exception(
    exc: BaseException,
    *,
    extra_secrets: Iterable[str] | None = None,
) -> str:
    """Return a secret-safe one-line exception summary.

    The exception class name is useful operational context and is normally safe
    to show. The raw message can contain request URLs, broker responses, or API
    tokens, so it passes through ``redact_text`` before leaving this boundary.
    """
    message = redact_text(str(exc), extra_secrets=extra_secrets)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


class SecretRedactionFilter(logging.Filter):
    """Logging filter that masks secrets before a record reaches handlers.

    Python logging keeps the message template, arguments, and traceback data on
    the record. This filter converts the formatted message into redacted text,
    clears the original args, and precomputes a redacted traceback string. That
    means both ``logger.warning("token=%s", value)`` and ``logger.exception(...)``
    are covered by the same redaction path.
    """

    def __init__(
        self,
        *,
        extra_secrets: Iterable[str] | None = None,
        name: str = "",
    ) -> None:
        super().__init__(name)
        self.extra_secrets = tuple(_clean_secret(value) for value in extra_secrets or ())

    def add_secrets(self, extra_secrets: Iterable[str]) -> None:
        """Teach an already-installed filter about additional secret values.

        ``install_secret_redaction_filter`` is idempotent and reuses one filter
        instance, so this lets a later install merge in secrets that were not
        known at the first call (for example Streamlit OIDC values from
        ``st.secrets``) instead of dropping them. Duplicates are ignored.
        """
        known = set(self.extra_secrets)
        merged = list(self.extra_secrets)
        for value in extra_secrets or ():
            cleaned = _clean_secret(value)
            if cleaned and cleaned not in known:
                known.add(cleaned)
                merged.append(cleaned)
        self.extra_secrets = tuple(merged)

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate the log record in place and keep it enabled.

        Beginner note:
        A ``LogRecord`` stores the format string and the arguments separately.
        If we only redacted ``record.msg``, a handler could still interpolate
        the original secret-bearing ``record.args`` later. Calling
        ``record.getMessage()`` first gives us the fully formatted text; clearing
        ``record.args`` prevents Python logging from formatting it a second time.
        """
        record.msg = redact_text(record.getMessage(), extra_secrets=self.extra_secrets)
        record.args = ()
        if record.exc_info:
            # ``exc_info`` is the structured traceback tuple. Python formatters
            # turn it into text after filters run, so we precompute a redacted
            # ``exc_text`` string. The formatter will use this safe string
            # instead of rendering the raw traceback later.
            raw_traceback = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact_text(raw_traceback, extra_secrets=self.extra_secrets)
        if record.stack_info:
            record.stack_info = redact_text(
                record.stack_info,
                extra_secrets=self.extra_secrets,
            )
        return True


def install_secret_redaction_filter(
    logger: logging.Logger | None = None,
    *,
    extra_secrets: Iterable[str] | None = None,
) -> SecretRedactionFilter:
    """Attach a ``SecretRedactionFilter`` to a logger and its current handlers.

    The helper is idempotent for the common case: calling it repeatedly will not
    stack multiple redaction filters onto the same logger or handler. It returns
    the filter so tests can verify installation and callers can inspect it if
    needed.

    Beginner note:
    Filters on a logger do not necessarily protect every record that reaches a
    handler through propagation. Attaching the same filter to existing handlers
    makes the final output sink safe even when records come from child loggers
    such as ``backend.daily_data_loader`` or ``screeners.*``.
    """
    target = logger or logging.getLogger()
    existing = _first_redaction_filter(target.filters)
    redaction_filter = existing or SecretRedactionFilter(extra_secrets=extra_secrets)
    if existing is None:
        target.addFilter(redaction_filter)
    elif extra_secrets:
        # The filter is already installed, but a later call may know secrets the
        # first one did not (e.g. OIDC values that load after initial logging
        # setup). Merge them in rather than silently dropping the new values.
        redaction_filter.add_secrets(extra_secrets)

    for handler in target.handlers:
        if _first_redaction_filter(handler.filters) is None:
            handler.addFilter(redaction_filter)
    return redaction_filter


def _redact_known_values(
    text: str,
    *,
    extra_secrets: Iterable[str] | None,
) -> str:
    """Mask exact configured/extra secret values, longest first.

    Longest-first replacement avoids partial leaks. For example, if a short
    client id is also embedded inside a longer database URL, masking the longer
    URL first prevents the shorter replacement from leaving the URL password or
    host context in a strange half-redacted state.
    """
    secrets: list[str] = []
    for raw_secret in (*_configured_secret_values(), *(extra_secrets or ())):
        cleaned = _clean_secret(raw_secret)
        if cleaned:
            secrets.append(cleaned)
    redacted = text
    for secret in sorted(set(secrets), key=len, reverse=True):
        redacted = redacted.replace(secret, MASK)
    return redacted


def _configured_secret_values() -> tuple[str, ...]:
    """Read settings secrets defensively so redaction never raises a new error.

    The import stays inside the function for two reasons: it avoids import-time
    cycles during app startup, and it lets redaction degrade gracefully if
    settings parsing is exactly the thing currently failing.
    """
    try:
        from backend.config import secret_values

        return tuple(secret_values())
    except Exception:  # noqa: BLE001 - redaction must be best-effort.
        return ()


def _clean_secret(value: Any) -> str:
    """Normalize one configured secret and ignore tiny accidental values."""
    cleaned = str(value or "").strip()
    # Very short values create more false positives than protection. Real API
    # keys, tokens, passwords, and client ids used by this app are longer.
    return cleaned if len(cleaned) >= 4 else ""


def _first_redaction_filter(filters: Iterable[object]) -> SecretRedactionFilter | None:
    """Return the first existing redaction filter from a logger/handler list.

    Typed as ``Iterable[object]`` because the stdlib types ``.filters`` as a mix
    of ``Filter`` objects and bare callables; the isinstance check below is the
    real contract.
    """
    for item in filters:
        if isinstance(item, SecretRedactionFilter):
            return item
    return None


# Canonical vocabulary for credential-shaped FIELD NAMES (mapping keys, column
# names, form fields). The regexes above redact secret VALUES inside running
# text; this set lets callers mask a value because of what its key is called.
# Entries are written in normalized form: lowercase with separators removed,
# which is how ``is_secret_key_name`` compares them.
SECRET_KEY_NAME_PARTS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authtoken",
        "clientsecret",
        "cookiesecret",
        "databaseurl",
        "passwd",
        "password",
        "pwd",
        "refreshtoken",
        "secret",
        "token",
    }
)

_SECRET_KEY_SUFFIXES = (
    "apikey",
    "accesstoken",
    "authtoken",
    "clientsecret",
    "password",
    "refreshtoken",
)


def is_secret_key_name(name: str) -> bool:
    """Return True when a field name looks like it stores a credential.

    Keeping this vocabulary next to the value-redaction patterns gives the app
    ONE definition of "secret-looking" — a new secret name added here protects
    log redaction and persisted scan history (PROV-001A) at the same time.
    Comparison normalizes case and separators, so ``API-Key``, ``api_key``, and
    ``ApiKey`` all match, and suffix checks catch names such as
    ``dhan_access_token`` or ``serpapi_api_key``.
    """
    normalized = re.sub(r"[^a-z0-9]+", "", str(name).lower())
    if normalized in SECRET_KEY_NAME_PARTS:
        return True
    return any(normalized.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)
