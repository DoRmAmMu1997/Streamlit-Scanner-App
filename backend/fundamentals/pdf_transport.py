"""Public-address-pinned HTTP transport for untrusted transcript links.

Beginner note: validating DNS and then giving the hostname to an HTTP library
allows a second DNS lookup to return an internal address. The connection pool
below receives the already-validated numeric IP, while TLS still authenticates
the original hostname. No process-wide DNS or socket state is modified.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool

from backend.url_safety import is_safe_http_url

Resolver = Callable[[str, int], Iterable[str]]


@dataclass(frozen=True)
class PublicTarget:
    """One canonical URL and the numeric destination authorized for its request.

    Beginner note: keep hostname identity separate from the connection address.
    Substituting an IP into the TLS hostname would authenticate the wrong peer.
    """

    url: str
    hostname: str
    port: int
    address: str
    host_header: str


class Transport(Protocol):
    """Explicit trusted transport seam; URL content cannot supply this object."""

    def __call__(
        self, target: PublicTarget, *, headers: dict[str, str], timeout: int,
    ) -> AbstractContextManager[requests.Response]:
        """Open one response, closing it when the caller leaves the context."""
        ...


def _resolve_addresses(hostname: str, port: int) -> Iterable[str]:
    """Resolve all TCP addresses once; errors propagate to the fail-closed caller.

    Beginner note: inspecting every answer prevents mixed public/private DNS
    records from becoming a lottery in which an internal address sometimes wins.
    """
    return [str(info[4][0]) for info in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]


def _public_address(address: str) -> bool:
    """Reject non-public and scoped addresses on both supported Python versions."""
    if "%" in address:
        return False
    parsed = ipaddress.ip_address(address)
    # Multicast can report is_global=True: it is never a unicast web endpoint.
    return parsed.is_global and not (parsed.is_multicast or parsed.is_reserved)


def resolve_public_target(url: str, *, resolver: Resolver | None = None) -> PublicTarget:
    """Canonicalize and authorize a URL before constructing any HTTP request.

    Args:
        url: Untrusted HTTP(S) URL, including a resolved redirect Location.
        resolver: Explicit trusted resolver injection for offline verification.

    Returns:
        Immutable URL, Host identity, and one validated numeric address.

    Raises:
        ValueError: Malformed URL, missing DNS results, or any non-public answer.
        OSError: DNS resolution failed; the downloader treats this as refusal.

    Beginner note: validation applies even with a custom transport. Preparing
    the URL first makes validation and requests agree on IDNA/percent encoding;
    raw controls, whitespace and backslashes are rejected before normalization.
    """
    if not url or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise ValueError("Malformed transcript URL")
    if not is_safe_http_url(url):
        raise ValueError("Unsafe transcript URL")
    original = urlsplit(url)
    if "%" in original.netloc or "@" in original.netloc or original.port == 0:
        raise ValueError("Invalid transcript authority")
    prepared = requests.Request("GET", url).prepare()
    canonical = (prepared.url or "").split("#", 1)[0]
    if not is_safe_http_url(canonical):
        raise ValueError("Unsafe canonical transcript URL")
    parsed = urlsplit(canonical)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = list((resolver or _resolve_addresses)(hostname, port))
    else:
        addresses = [str(literal)]
    if not addresses or not all(_public_address(address) for address in addresses):
        raise ValueError("Transcript DNS contains a non-public address")
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        host_header += f":{port}"
    return PublicTarget(canonical, hostname, port, str(ipaddress.ip_address(addresses[0])), host_header)


class _PinnedAdapter(HTTPAdapter):
    """Connect only to one authorized IP and authenticate the original hostname."""

    def __init__(self, target: PublicTarget) -> None:
        """Keep the immutable target local to this single-hop adapter."""
        self.target = target
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(
        self, request: requests.PreparedRequest, verify: Any, proxies: Any = None, cert: Any = None,
    ) -> HTTPConnectionPool:
        """Build an IP-keyed pool with original-host SNI/certificate matching.

        Beginner note: requests 2.34 exposes this adapter extension point. Its
        normal TLS pool attributes preserve CA verification, but the pool host
        is replaced before urllib3 creates a socket. Neither a DNS retry nor an
        environment proxy can change that numeric destination.
        """
        if request.url != self.target.url or verify is not True or proxies or cert is not None:
            raise requests.RequestException("Unexpected transcript transport configuration")
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, True, None)
        # types-requests models a narrower TypedDict than urllib3's documented
        # HTTPSConnectionPool keyword interface. Keep the widening local.
        tls_options: dict[str, Any] = dict(pool_kwargs)
        host_params["host"] = self.target.address
        host_params["port"] = self.target.port
        if host_params["scheme"] == "https":
            tls_options["server_hostname"] = self.target.hostname
            tls_options["assert_hostname"] = self.target.hostname
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=tls_options)


@contextmanager
def open_pinned_response(
    target: PublicTarget, *, headers: dict[str, str], timeout: int,
) -> Iterator[requests.Response]:
    """Open exactly one direct, verified request and close all owned resources.

    Beginner note: a fresh session carries no caller cookies, auth, adapters, or
    proxy settings. trust_env=False also prevents netrc credentials and ambient
    CA/proxy configuration from silently altering this security boundary.
    Redirects remain the downloader's responsibility, before the next request.
    """
    with requests.Session() as session:
        session.trust_env = False
        adapter = _PinnedAdapter(target)
        session.mount(f"{urlsplit(target.url).scheme}://", adapter)
        request = session.prepare_request(requests.Request(
            "GET", target.url, headers={**headers, "Host": target.host_header},
        ))
        # Session.send builds a hypothetical next request even when passed
        # allow_redirects=False, eagerly consuming the entire redirect body.
        # HTTPAdapter.send uses urllib3 redirect=False and never does that work.
        with adapter.send(
            request,
            timeout=timeout,
            stream=True,
            verify=True,
            proxies={},
        ) as response:
            yield response
