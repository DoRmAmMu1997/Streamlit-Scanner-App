# ADR: Pin transcript downloads to validated public destinations

Status: Accepted implementation of the approved transcript egress security package.

## Context and decision

`read_recent_concall_text` takes transcript URLs scraped from third-party pages.
Previously `download_pdf` enabled automatic redirects, then checked the final URL
after a request had already reached it. Supplying a Session also disabled DNS
validation. A separate preflight DNS lookup did not bind the eventual connection.

The downloader now authorizes each canonical HTTP(S) URL before opening a
response. Userinfo, malformed ports, controls, backslashes, scoped addresses,
non-public destinations, resolution failures, and mixed public/private DNS
answers fail closed. A relative redirect resolves against the current URL and
passes the same checks. Three redirects permit at most four requests; missing
Locations, loops and a fourth redirect stop without contacting another target.

The dedicated `backend/fundamentals/pdf_transport.py` helper records the original
hostname separately from the chosen public numeric address. Its requests adapter
creates a urllib3 pool at that address, preserving the original HTTP Host header,
TLS `server_hostname` and certificate `assert_hostname`. CA verification remains
enabled. The connection never resolves the original hostname again, and no
process-global resolver patch or caller-session mutation is used.

Each production hop owns a fresh Session with `trust_env=False` and empty proxy
settings. Prepared requests carry only the downloader's explicit headers. The
adapter receives `verify=True`, `proxies={}`, streaming and the existing 30-second
timeout. No caller cookies, auth, proxy configuration, or custom adapters cross
this boundary. Resources close on success, rejected status/redirect, size/type
failure, DNS failure, TLS failure and stream errors.

## Why call the adapter directly

The locally verified requests 2.34.2 Session path calls `resolve_redirects` to
construct `Response._next` even with `allow_redirects=False`. That consumes the
entire redirect body before returning it. A regression reproduced this behavior
with an unreadable offline redirect stream. Using `HTTPAdapter.send` directly
retains requests' TLS/response handling and urllib3's `redirect=False`, while
returning control without consuming the body or following the redirect.

Existing PDF acceptance rules remain: at most 25 MiB of streamed decoded bytes,
PDF magic, and an allowed/missing media type. Redirect bodies are closed without
being read. This package does not change PDF parsing or the existing cache key.

## Alternatives and compatibility

Preflight validation followed by a normal hostname connection leaves a DNS
time-of-check/time-of-use gap. Globally patching DNS risks unrelated concurrent
requests. Mutating a supplied Session introduces adapter/proxy races and carries
unrelated credentials. All three alternatives were rejected.

`download_pdf(..., session=...)` keeps its legacy signature, but session-only
injection returns `None`, logs an explicit unsupported-injection reason and
makes no network request. The same behavior propagates through the legacy
`read_recent_concall_text(..., session=...)` option. Default application callers
do not supply a Session and retain ordinary public transcript downloads.

Offline callers migrate to explicit trusted `resolver` and `transport` keyword
arguments on `download_pdf`. Resolver results are still checked and transport
receives only an already-validated `PublicTarget`. These code-level injection
objects are not exposed to URL content or the model. A caller supplying both a
legacy Session and an explicit transport selects the explicit transport; the
legacy object never contributes network configuration.

## Verification and limits

`tests/test_pdf_egress.py` proves refused destinations have zero target requests,
manual public redirects succeed, mixed DNS fails, redirect loops/hop limits stop,
response resources close, and the production adapter supplies the validated IP
with original Host/SNI/certificate identity. It inspects the actual urllib3 pool
and socket-creation arguments without probing a live endpoint. Existing download
tests retain cache, size, content-type and PDF magic coverage.

The existing timeout is per connection/read, not an overall workflow deadline.
The resolver uses the operating system's DNS timeout. Arbitrary public HTTP URLs
remain allowed and therefore do not authenticate document content; HTTPS verifies
server identity. Network-level egress policy remains useful defense in depth.
This ADR scopes enforcement to transcript downloads; other URL fetchers are not
implicitly hardened by this helper.

Related: [fundamentals LLD](components/fundamentals-ai.md),
[security LLD](components/security.md).
