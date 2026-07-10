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
import socket
from collections.abc import Callable
from typing import Any, Never
from urllib.parse import urljoin, urlsplit, urlunsplit


def _reject(error: Callable[[], Exception]) -> Never:
    """Raise the caller's error from a helper frame.

    Raising via a call (rather than a ``raise`` statement lexically inside an
    ``except`` clause) preserves the implicit exception context exactly the
    way both pre-IPO-006 copies did with their own raiser helpers.
    """
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
    candidate = urljoin(base_url, str(value).strip())
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        _reject(error)
    if (
        parsed.scheme.casefold() != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise error()
    if require_pdf_path and not parsed.path.startswith("/sebi_data/attachdocs/"):
        raise error()

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
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
