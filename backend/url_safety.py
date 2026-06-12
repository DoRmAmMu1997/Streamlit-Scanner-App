"""Small helpers for deciding whether a third-party URL is safe to fetch.

Why this exists:
Several app features scrape links from public pages and then fetch those links
server-side. A poisoned page could otherwise point the Streamlit process at
`localhost`, a LAN address, or a cloud metadata endpoint. These helpers keep the
policy in one place so every downloader can fail closed the same way.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_LOCALHOST_NAMES = {"localhost", "localhost.localdomain"}


def _is_public_ip(address: str) -> bool:
    """Return True only for globally routable IP addresses.

    `ipaddress` marks loopback, private, link-local, multicast, and reserved
    ranges as non-global. Those are exactly the ranges a server-side fetcher
    should refuse when the URL came from untrusted HTML.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global


def hostname_looks_public(hostname: str) -> bool:
    """Cheap hostname/IP-literal screen before any network request.

    This catches the dangerous cases that do not require DNS resolution:
    `127.0.0.1`, `10.x.x.x`, `169.254.x.x`, `localhost`, and similar. Domain
    names that are not obviously local pass this cheap check; callers that use
    real network sessions can optionally add DNS resolution below.
    """
    normalized = (hostname or "").strip().rstrip(".").lower()
    if not normalized or normalized in _LOCALHOST_NAMES or normalized.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(normalized).is_global
    except ValueError:
        # Not an IP literal. It may still resolve to a private address; callers
        # with real network access can use `hostname_resolves_public(...)`.
        return True


def hostname_resolves_public(hostname: str) -> bool:
    """Resolve `hostname` and require every returned address to be public.

    DNS can map an innocent-looking name to 127.0.0.1 or a private subnet. When
    the app is about to perform a real fetch, this extra check closes that DNS
    rebinding-style bypass. Resolution failure is treated as unsafe because a
    legitimate production fetch should use a resolvable host.
    """
    normalized = (hostname or "").strip().rstrip(".").lower()
    if not hostname_looks_public(normalized):
        return False
    try:
        infos = socket.getaddrinfo(normalized, None)
    except socket.gaierror:
        return False
    addresses = {info[4][0] for info in infos if info and info[4]}
    return bool(addresses) and all(_is_public_ip(address) for address in addresses)


def is_safe_http_url(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
    resolve_dns: bool = False,
) -> bool:
    """Return whether `url` is an HTTP(S) URL the app may fetch.

    `allowed_hosts` is for same-origin app helpers such as screener.in HTMX
    endpoints. When it is provided, the host must match one of those names
    exactly. `resolve_dns=True` should be used before real, non-test network
    fetches so private DNS answers are rejected too.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return False
    if allowed_hosts is not None:
        normalized_allowed = {value.strip().rstrip(".").lower() for value in allowed_hosts}
        if host not in normalized_allowed:
            return False
    if resolve_dns:
        return hostname_resolves_public(host)
    return hostname_looks_public(host)
