# LLD — Security (secret redaction + SSRF guardrails + AI cache integrity)

| | |
|---|---|
| **Component** | Cross-cutting security utilities |
| **Source** | [`backend/security/redaction.py`](../../../backend/security/redaction.py), [`backend/security/__init__.py`](../../../backend/security/__init__.py), [`backend/url_safety.py`](../../../backend/url_safety.py), [`backend/ai_cache_integrity.py`](../../../backend/ai_cache_integrity.py) |
| **Layer** | Foundation (leaf utilities, best-effort, never raise on the safety path) |
| **Status** | Stable (SEC-001 URL safety · SEC-002 redaction · PROV-003 AI cache integrity) |
| **Related** | [HLD](../high-level-design.md) · [configuration.md](configuration.md) · [observability.md](observability.md) · [scan-service-and-provenance.md](scan-service-and-provenance.md) · [storage-persistence.md](storage-persistence.md) · [technical-analysis-ai.md](technical-analysis-ai.md) · [sixty-seven-ka-funda-ai.md](sixty-seven-ka-funda-ai.md) |

## 1. Purpose & responsibilities

Three independent guardrails keep the app safe by default:

1. **Secret redaction (SEC-002)** — mask credentials before any text reaches a
   log handler, a UI error panel, or a persisted `scan_runs.error_message`.
2. **SSRF guardrails (SEC-001)** — decide whether a URL scraped from an untrusted
   public page is safe for the server to fetch, refusing loopback / private /
   link-local / metadata addresses.
3. **AI cache integrity (PROV-003)** — HMAC-sign each durable AI verdict-cache
   envelope and verify it before reuse, so a tampered/forged cache entry is
   rejected and recomputed instead of trusted. Plus the AI-evidence boundary:
   evidence URLs are sanitized (credentials/query/fragment stripped, SSRF-screened)
   and only hashes + labels are stored — never raw scraped/model text.

**Non-responsibilities**
- Does not own *what* gets logged (that is [observability.md](observability.md)) — only how it is masked.
- Does not store secrets — it reads them from [configuration.md](configuration.md) `secret_values()` defensively.

## 2. Position in the system

```mermaid
flowchart LR
    subgraph Redaction[SEC-002 redaction]
      CFG["config.secret_values()"] --> RT["redact_text()"]
      RT --> LOGS["log formatters"]
      RT --> UI["UI error panels"]
      RT --> DB["scan_runs.error_message"]
      FILT["SecretRedactionFilter"] --> RT
    end
    subgraph URLSafety[SEC-001 url_safety]
      SCRAPE["scraped link (untrusted)"] --> SAFE["is_safe_http_url()"]
      SAFE --> FETCH["server-side fetch"]
    end
    subgraph AICache[PROV-003 ai_cache_integrity]
      KEY["signing key (env or per-process)"] --> SIGN["sign_cache_envelope (HMAC-SHA256)"]
      SIGN --> DISK[("on-disk AI verdict cache")]
      DISK --> VERIFY["verify_cache_envelope"]
      VERIFY -->|invalid| RECOMPUTE["cache miss -> recompute"]
    end
```

## 3. Public interface

### Redaction — `backend/security/redaction.py`
| Symbol | Contract |
|---|---|
| `redact_text(text, *, extra_secrets=None)` | Mask configured secrets + token shapes. Non-strings returned unchanged. **Order: exact configured values → DB-URL password → `Authorization: Bearer` → generic `key=value`.** |
| `redact_exception(exc, *, extra_secrets=None)` | `"<ClassName>: <redacted message>"` — class name kept (useful + safe), message scrubbed. |
| `SecretRedactionFilter(logging.Filter)` | Masks `record.getMessage()`, clears `record.args`, precomputes redacted `exc_text`/`stack_info`. `add_secrets()` merges later-known secrets (e.g. OIDC). |
| `install_secret_redaction_filter(logger=None, *, extra_secrets=None)` | Idempotent; attaches one filter to the logger **and its handlers** (so child-logger records that propagate are still covered). |
| `is_secret_key_name(name)` | True when a *field name* looks credential-shaped (normalizes case/separators; matches `SECRET_KEY_NAME_PARTS` + suffix list). Used by PROV-001A to redact persisted result keys. |
| `SECRET_KEY_NAME_PARTS` | Canonical normalized vocabulary of secret-ish field names. |

### URL safety — `backend/url_safety.py`
| Symbol | Contract |
|---|---|
| `is_safe_http_url(url, *, allowed_hosts=None, resolve_dns=False)` | Require http(s), no embedded credentials, optional exact host allowlist, public host. `resolve_dns=True` before real fetches. |
| `hostname_looks_public(hostname)` | Cheap pre-DNS screen: rejects `localhost`/`*.localhost`, IP literals in non-global ranges. |
| `hostname_resolves_public(hostname)` | Resolves via `getaddrinfo`; **every** answer must be global (closes DNS-rebinding). Resolution failure ⇒ unsafe. |

### AI cache integrity — `backend/ai_cache_integrity.py`
| Symbol | Contract |
|---|---|
| `sign_cache_envelope(envelope, *, key)` | Return a copy carrying `integrity_hmac_sha256` over the canonical JSON of the whole envelope (signature field removed first). |
| `verify_cache_envelope(envelope, *, key)` | `True` only on a length/type-checked, constant-time (`hmac.compare_digest`) match; non-dict / missing sig / non-finite JSON → `False`. |
| `get_ai_cache_signing_key()` | Operator key from `SCANNER_AI_CACHE_SIGNING_KEY`, else a per-process random key (secure by default; a restart invalidates the disk cache). |

### Evidence sanitization — `backend/scanning/result_contract.py`
| Symbol | Contract |
|---|---|
| `sanitize_evidence_url(value)` | Redact, SSRF-screen (`is_safe_http_url`), and strip credentials/query/fragment — the only URL form stored in an AI receipt. |

## 4. Key design decisions & trade-offs

| Decision | Rationale | Alternative rejected |
|---|---|---|
| **Redaction is best-effort, never raises** | `_configured_secret_values()` wraps the settings import in `try/except` so redaction still works even when settings parsing is the thing failing. | Strict — would turn the safety net into a new crash. |
| **Longest-secret-first replacement** | Masking a long DB URL before a short client id embedded in it avoids half-redacted output. | Arbitrary order — partial leaks. |
| **Specific shapes before generic** | DB-URL/`Bearer` passes keep useful context (scheme, host, header name) that a blanket `key=value` pass would mangle. | Single regex — loses operator context. |
| **High-signal `_SECRET_NAME` vocabulary** | Including vague words like "api key" would hide *useful* messages such as "Invalid API key". | Broad list — over-redaction. |
| **Min secret length ≥ 4 (`_clean_secret`)** | Tiny accidental values create more false positives than protection. | Mask everything — noise. |
| **Filter masks `getMessage()` + clears `args`** | A `LogRecord` stores template + args separately; redacting only `msg` lets a handler re-interpolate the secret later. | Redact `msg` only — leak via deferred formatting. |
| **URL safety fails closed on resolution error** | A legitimate production fetch uses a resolvable, public host. | Allow on error — SSRF bypass. |
| **One shared `is_secret_key_name`** | Same definition protects log redaction *and* persisted scan history — add a name once, both benefit. | Two vocabularies — drift. |
| **AI verdict cache is HMAC-signed** | A disk cache is writable; an HMAC over the full envelope (constant-time verify) means a forged/edited entry is rejected and recomputed, never served as a real verdict. The signing key is in `secret_values()` so it is itself redacted. | Unsigned cache — forgeable "approved" verdicts. |
| **Store hashed evidence, not raw text** | AI receipts persist SHA-256 hashes + sanitized URLs + labels; raw scraped pages / model responses are never written to durable history. | Persist raw evidence — durable leak, unverifiable. |
| **Receipt cross-checked against the verdict** | Persistence rejects a receipt whose `validated_verdict_json` contradicts the trusted fields, so model output can't rewrite the audit record. | Trust model JSON — forgeable audit. |

## 5. Failure modes / degradation

- Settings import failing → redaction silently proceeds with only `extra_secrets` + regex shapes.
- Unknown/exotic value types passed to `redact_text` → returned unchanged (defensive in UI paths where input may be `None`).
- `is_safe_http_url` on a malformed URL → `False`.

## 6. Testing

- [`tests/test_secret_redaction.py`](../../../tests/test_secret_redaction.py) — key/value, Bearer, DB-URL password, logging filter, `is_secret_key_name`.
- [`tests/test_url_safety.py`](../../../tests/test_url_safety.py) — loopback/private/link-local rejection, allowlist, DNS resolution path.
- [`tests/test_ai_cache_integrity.py`](../../../tests/test_ai_cache_integrity.py) — tamper detection, key binding, non-finite rejection.
- [`tests/test_supply_chain_policy.py`](../../../tests/test_supply_chain_policy.py) — dependency posture.

## 7. Extension points

Add a new credential shape by extending `_SECRET_NAME` / the specific regexes in `redaction.py`, and add the field-name form to `SECRET_KEY_NAME_PARTS`. Add a new allowed scrape host by passing `allowed_hosts=` at the call site, not by widening the global policy.
