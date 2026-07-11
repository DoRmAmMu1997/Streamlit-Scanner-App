"""One canonical SEBI URL gate for the two IPO fetch surfaces (IPO-006).

Beginner note:
The listing scraper (``backend/ipo/sources/sebi.py``) and the prospectus
downloader (``backend/ipo/documents/downloader.py``) each canonicalized and
allowlisted SEBI URLs with a private copy of the same logic. The copies had
already drifted: the downloader's gained a malformed-port guard, an optional
DNS-answer check (against a poisoned hosts file or DNS response pointing
SEBI's name at private infrastructure), and a PDF-path restriction that the
scraper's copy lacked. This module is the single implementation; each caller
keeps a thin private wrapper that binds its own base URL, error type, and
hardening options, so both modules' public behavior and test suites stay
unchanged.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import socket
from collections.abc import Callable
from typing import Any, Never
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_MAX_PERCENT_DECODING_PASSES = 4


def _reject(error: Callable[[], Exception]) -> Never:
    """Raise the caller's error from a helper frame.

    Raising via a call (rather than a ``raise`` statement lexically inside an
    ``except`` clause) preserves the implicit exception context exactly the
    way both pre-IPO-006 copies did with their own raiser helpers.
    """
    raise error()


def _validate_pdf_path(path: str, error: Callable[[], Exception]) -> None:
    """Require one unambiguous path inside SEBI's attachment directory.

    Beginner note:
    ``urlsplit`` intentionally leaves percent escapes untouched. A downstream
    HTTP client, proxy, or web server may decode them later, so a raw prefix
    check is not enough: ``%2e%2e`` becomes ``..`` and ``%2f`` becomes ``/``.
    We decode repeatedly to expose single- and double-encoded traversal, reject
    encoded separators at every layer, and then compare normalized segments.
    """
    decoded = path
    for _pass in range(_MAX_PERCENT_DECODING_PASSES):
        # Backslashes are separators on some servers. Percent-encoded slashes
        # and backslashes are rejected rather than decoded because otherwise
        # different network layers could disagree about the segment boundary.
        if "\\" in decoded or _ENCODED_PATH_SEPARATOR.search(decoded):
            raise error()
        try:
            next_value = unquote(decoded, errors="strict")
        except (UnicodeDecodeError, ValueError):
            _reject(error)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        # Excessively nested encodings are ambiguous and have no legitimate
        # use in an official attachment path, so fail closed.
        raise error()

    segments = decoded.split("/")
    if (
        len(segments) < 4
        or segments[:3] != ["", "sebi_data", "attachdocs"]
        or any(segment in {".", ".."} for segment in segments)
        or posixpath.normpath(decoded) != decoded
    ):
        raise error()


def canonical_sebi_url(
    value: str,
    *,
    base_url: str,
    allowed_hosts: frozenset[str],
    error: Callable[[], Exception],
    resolver: Callable[..., Any] | None = None,
    require_pdf_path: bool = False,
) -> str:
    """Canonicalize one official SEBI HTTPS URL or raise the caller's error.

    Every rejection path raises ``error()`` so each caller keeps its own error
    taxonomy (``SebiSourceError`` with a fixed message for the scraper, a
    secret-safe ``unsafe_url`` code for the downloader) without this module
    knowing either type.

    Checks, in order:
    1. Resolve ``value`` against ``base_url`` and split it. A malformed port
       (e.g. ``https://host:abc/``) is rejected rather than leaking a bare
       ``ValueError`` to the caller.
    2. Require exactly ``https``, a host in ``allowed_hosts``, no embedded
       credentials, and port 443 (or none).
    3. With ``require_pdf_path=True``, additionally require SEBI's attachment
       prefix (``/sebi_data/attachdocs/``) — the downloader's PDF fetches only.
    4. With a ``resolver`` (the downloader passes ``socket.getaddrinfo`` or a
       test double; the scraper passes none), resolve the allowlisted host and
       reject any non-public answer. Host allowlisting alone blocks ordinary
       SSRF; this extra step also catches DNS answers that point the trusted
       name at loopback/private ranges.

    The result drops the fragment and normalizes an empty path to ``/`` so a
    record fingerprint identifies a server resource rather than browser-only
    navigation state.
    """
    try:
        # Keep every stdlib parsing accessor inside this boundary. ``urljoin``
        # and ``urlsplit`` can reject malformed bracketed hosts or Unicode
        # netlocs, while ``hostname`` and ``port`` perform additional checks.
        candidate = urljoin(base_url, str(value).strip())
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        _reject(error)
    if (
        parsed.scheme.casefold() != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise error()
    if require_pdf_path:
        _validate_pdf_path(parsed.path, error)

    if resolver is not None:
        try:
            answers = resolver(host, 443, type=socket.SOCK_STREAM)
            addresses = {str(answer[4][0]) for answer in answers}
            # An empty answer set or ANY non-global address fails closed; the
            # ip_address() parse itself is inside the try so a malformed
            # resolver answer is rejected, not raised.
            unsafe = not addresses or any(
                not ipaddress.ip_address(address).is_global for address in addresses
            )
        except (OSError, TypeError, ValueError, IndexError):
            _reject(error)
        if unsafe:
            raise error()
    try:
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (TypeError, UnicodeError, ValueError):
        _reject(error)
