"""Tests for the SSRF guard in backend/url_safety.py.

These helpers are the single policy point deciding whether a URL scraped from
untrusted HTML may be fetched server-side. They previously had no direct tests;
the cases below pin the fail-closed behavior for schemes, credentials,
localhost names, private/link-local/metadata IP literals, allowed-host pinning,
and the DNS-resolution backstop (with ``socket.getaddrinfo`` monkeypatched so
no test performs a real lookup).
"""

from __future__ import annotations

import socket

import pytest

from backend.url_safety import (
    hostname_looks_public,
    hostname_resolves_public,
    is_safe_http_url,
)

# ---------------------------------------------------------------------------
# is_safe_http_url: scheme, credentials, host shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.screener.in/company/TCS/",
        "http://example.com/page",
        "https://example.com:8443/path?q=1",
    ],
)
def test_accepts_plain_public_http_urls(url):
    assert is_safe_http_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "gopher://example.com/",
        "",
        "not a url",
    ],
)
def test_rejects_non_http_schemes_and_garbage(url):
    assert is_safe_http_url(url) is False


def test_rejects_embedded_credentials():
    # user:pass@host is a classic trick to confuse host parsing downstream.
    assert is_safe_http_url("https://user:secret@example.com/") is False
    assert is_safe_http_url("https://user@example.com/") is False


def test_rejects_missing_host():
    assert is_safe_http_url("https:///path-only") is False


# ---------------------------------------------------------------------------
# is_safe_http_url: local and private targets must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://localhost.localdomain/",
        "http://foo.localhost/",
        "http://127.0.0.1:8501/",
        "http://10.0.0.5/",
        "http://172.16.1.1/",
        "http://192.168.1.10/router",
        # Cloud metadata endpoint - the canonical SSRF target.
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
    ],
)
def test_rejects_local_and_private_targets(url):
    assert is_safe_http_url(url) is False


def test_trailing_dot_and_case_do_not_bypass_the_check():
    assert is_safe_http_url("http://LOCALHOST./") is False
    assert is_safe_http_url("http://127.0.0.1./") is False


# ---------------------------------------------------------------------------
# is_safe_http_url: allowed-host pinning
# ---------------------------------------------------------------------------


def test_allowed_hosts_pins_exact_hostnames():
    allowed = {"www.screener.in", "screener.in"}
    assert is_safe_http_url("https://www.screener.in/x", allowed_hosts=allowed) is True
    # Subdomains and look-alikes are not the pinned host.
    assert is_safe_http_url("https://evil.screener.in/x", allowed_hosts=allowed) is False
    assert is_safe_http_url("https://screener.in.evil.com/x", allowed_hosts=allowed) is False


def test_allowed_hosts_comparison_normalizes_case_and_trailing_dot():
    allowed = {"WWW.Screener.IN."}
    assert is_safe_http_url("https://www.screener.in/x", allowed_hosts=allowed) is True


# ---------------------------------------------------------------------------
# hostname_looks_public: the cheap pre-DNS screen
# ---------------------------------------------------------------------------


def test_hostname_looks_public_accepts_domains_and_global_ips():
    assert hostname_looks_public("example.com") is True
    assert hostname_looks_public("8.8.8.8") is True


@pytest.mark.parametrize(
    "hostname",
    ["", "localhost", "sub.localhost", "127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"],
)
def test_hostname_looks_public_rejects_local_shapes(hostname):
    assert hostname_looks_public(hostname) is False


# ---------------------------------------------------------------------------
# hostname_resolves_public: the DNS-rebinding backstop (getaddrinfo patched)
# ---------------------------------------------------------------------------


def _addrinfo(*addresses: str):
    """Build minimal getaddrinfo-shaped tuples for the given IP strings."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
        for address in addresses
    ]


def test_resolves_public_when_all_answers_are_global(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: _addrinfo("93.184.216.34"))
    assert hostname_resolves_public("example.com") is True


def test_rejects_when_any_answer_is_private(monkeypatch):
    # A rebinding attack returns one public and one private answer; every
    # address must be public for the fetch to proceed.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: _addrinfo("93.184.216.34", "192.168.1.10"),
    )
    assert hostname_resolves_public("example.com") is False


def test_rejects_when_resolution_fails(monkeypatch):
    def boom(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert hostname_resolves_public("does-not-resolve.invalid") is False


def test_rejects_when_resolution_returns_nothing(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [])
    assert hostname_resolves_public("example.com") is False


def test_local_name_short_circuits_before_dns(monkeypatch):
    def must_not_resolve(*_args, **_kwargs):
        raise AssertionError("localhost must be rejected before any DNS lookup")

    monkeypatch.setattr(socket, "getaddrinfo", must_not_resolve)
    assert hostname_resolves_public("localhost") is False


def test_is_safe_http_url_with_resolve_dns_uses_the_backstop(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: _addrinfo("127.0.0.1"),
    )
    # Looks like a normal domain, but resolves to loopback: must be rejected.
    assert is_safe_http_url("https://rebound.example.com/", resolve_dns=True) is False
