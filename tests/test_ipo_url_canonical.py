"""Direct tests for the shared SEBI URL canonicalizer (IPO-006).

The listing scraper and the prospectus downloader exercise this function
end-to-end through their own suites (which pass unmodified); these tests lock
the shared implementation's knobs directly — the error factory, the optional
DNS answer check, and the PDF-path restriction — so a future edit cannot
weaken one caller's hardening without a failure here.
"""

from __future__ import annotations

import pytest

from backend.ipo.url_canonical import canonical_sebi_url

_ALLOWED = frozenset({"sebi.gov.in", "www.sebi.gov.in"})
_BASE = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"


class _Rejected(Exception):
    """Caller-supplied error type; the canonicalizer must raise exactly this."""


def _canonical(value: str, **overrides):
    """Call the canonicalizer with this suite's defaults, overriding per test.

    Bundling ``base_url``/``allowed_hosts``/``error`` here keeps each test
    focused on the one knob it varies, the way the two production wrappers
    bind their own fixed configuration.
    """
    kwargs = {
        "base_url": _BASE,
        "allowed_hosts": _ALLOWED,
        "error": _Rejected,
    }
    kwargs.update(overrides)
    return canonical_sebi_url(value, **kwargs)


def _resolver_answers(*addresses: str):
    """Build a getaddrinfo-shaped resolver returning the given IP answers."""

    def _resolver(_host, _port, **_kwargs):
        """Return one socket-address tuple per programmed answer."""
        return [(None, None, None, None, (address, 443)) for address in addresses]

    return _resolver


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_absolute_url_keeps_query_drops_fragment():
    """The fragment is browser-only state and must not reach fingerprints."""
    url = "https://www.sebi.gov.in/filings/jun-2026/demo.html?x=1#section-3"
    assert _canonical(url) == "https://www.sebi.gov.in/filings/jun-2026/demo.html?x=1"


def test_relative_url_resolves_against_base():
    """Listing pages emit relative hrefs; they resolve against the caller's base."""
    assert _canonical("/filings/demo.html") == "https://www.sebi.gov.in/filings/demo.html"


def test_empty_path_normalizes_to_slash():
    """A bare host canonicalizes with an explicit root path."""
    assert _canonical("https://sebi.gov.in") == "https://sebi.gov.in/"


def test_host_casefolds_and_explicit_443_is_dropped():
    """Mixed-case hosts and an explicit default port collapse to one form."""
    assert (
        _canonical("https://WWW.SEBI.GOV.IN:443/doc.pdf")
        == "https://www.sebi.gov.in/doc.pdf"
    )


# ---------------------------------------------------------------------------
# Rejections — every path raises the CALLER's error type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sebi.gov.in/doc.pdf",  # not https
        "https://evil.example.com/doc.pdf",  # host not allowlisted
        "https://user@www.sebi.gov.in/doc.pdf",  # embedded credentials
        "https://user:pass@www.sebi.gov.in/doc.pdf",  # embedded credentials
        "https://www.sebi.gov.in:8443/doc.pdf",  # non-443 port
        "https://www.sebi.gov.in:abc/doc.pdf",  # malformed port
        "https://[www.sebi.gov.in/doc.pdf",  # malformed bracketed host
        "https://www.sebi.gov.in\uff0fevil/doc.pdf",  # invalid NFKC netloc character
    ],
)
def test_unsafe_urls_raise_the_callers_error(url):
    """Every rejection surfaces as the injected error type, never a bare one.

    The malformed-port case matters most: ``urlsplit(...).port`` raises a raw
    ``ValueError``, and before IPO-006 the scraper's copy leaked it uncaught.
    """
    with pytest.raises(_Rejected):
        _canonical(url)


def test_require_pdf_path_restricts_to_attachdocs():
    """The downloader-only knob confines PDF fetches to SEBI's attachment tree."""
    good = "https://www.sebi.gov.in/sebi_data/attachdocs/jun-2026/demo.pdf"
    assert _canonical(good, require_pdf_path=True).endswith("/demo.pdf")

    with pytest.raises(_Rejected):
        _canonical("https://www.sebi.gov.in/other/demo.pdf", require_pdf_path=True)
    # The same URL passes when the caller (the listing scraper) does not
    # request the restriction.
    assert _canonical("https://www.sebi.gov.in/other/demo.pdf")


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/sebi_data/attachdocs/../secret.pdf",
        "/sebi_data/attachdocs/%2e%2e/secret.pdf",
        "/sebi_data/attachdocs/%252e%252e/secret.pdf",
        "/sebi_data/attachdocs/demo%2f..%2fsecret.pdf",
        "/sebi_data/attachdocs/demo%252f..%252fsecret.pdf",
        "/sebi_data/attachdocs/demo%5c..%5csecret.pdf",
        r"/sebi_data/attachdocs/demo\..\secret.pdf",
    ],
)
def test_pdf_path_rejects_traversal_and_encoded_separators(unsafe_path):
    """Decode path segments before accepting SEBI's attachment directory.

    Beginner note:
    HTTP clients and reverse proxies can decode percent escapes at different
    times. Checking only the raw string therefore lets ``%2e%2e`` (``..``) or
    an encoded slash acquire a different meaning after this guard has run.
    """
    with pytest.raises(_Rejected):
        _canonical(
            f"https://www.sebi.gov.in{unsafe_path}",
            require_pdf_path=True,
        )


def test_malformed_base_url_raises_the_callers_error():
    """A bad join base is categorized instead of leaking ``ValueError``."""
    with pytest.raises(_Rejected):
        _canonical("relative.pdf", base_url="https://[")


# ---------------------------------------------------------------------------
# Optional DNS answer check (the downloader's anti-rebinding layer)
# ---------------------------------------------------------------------------


def test_resolver_accepting_public_answers_passes():
    """A host resolving only to public addresses is allowed through."""
    url = _canonical(
        "https://www.sebi.gov.in/doc.pdf", resolver=_resolver_answers("1.2.3.4")
    )
    assert url == "https://www.sebi.gov.in/doc.pdf"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.7", "::1"])
def test_resolver_returning_private_answer_fails_closed(address):
    """Loopback/private answers mean a poisoned resolution — reject the fetch."""
    with pytest.raises(_Rejected):
        _canonical("https://www.sebi.gov.in/doc.pdf", resolver=_resolver_answers(address))


def test_one_private_answer_among_public_ones_still_fails_closed():
    """A single private answer taints the whole set: ANY, not ALL, rejects."""
    with pytest.raises(_Rejected):
        _canonical(
            "https://www.sebi.gov.in/doc.pdf",
            resolver=_resolver_answers("1.2.3.4", "127.0.0.1"),
        )


def test_empty_or_failing_resolver_fails_closed():
    """No answers and resolver errors both reject rather than proceeding blind."""
    with pytest.raises(_Rejected):
        _canonical("https://www.sebi.gov.in/doc.pdf", resolver=_resolver_answers())

    def _broken_resolver(_host, _port, **_kwargs):
        """Model a DNS outage: getaddrinfo raising ``OSError``."""
        raise OSError("resolution failed")

    with pytest.raises(_Rejected):
        _canonical("https://www.sebi.gov.in/doc.pdf", resolver=_broken_resolver)


def test_malformed_resolver_answer_fails_closed():
    """An answer that does not parse as an IP address rejects, not raises."""
    with pytest.raises(_Rejected):
        _canonical(
            "https://www.sebi.gov.in/doc.pdf", resolver=_resolver_answers("not-an-ip")
        )


def test_no_resolver_skips_the_dns_check_entirely():
    """resolver=None (the scraper's configuration) must skip DNS, not null-run it.

    An empty answer set fails closed WITH a resolver (asserted above), so this
    passing proves the DNS layer is skipped rather than run with no answers.
    """
    url = _canonical("https://www.sebi.gov.in/doc.pdf", resolver=None)
    assert url == "https://www.sebi.gov.in/doc.pdf"
