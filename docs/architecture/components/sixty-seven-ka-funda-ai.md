# LLD — 67 Ka Funda (AI) subsystem (`backend/sixty_seven`)

| | |
|---|---|
| **Component** | Deterministic drawdown gate + Claude evidence verifier |
| **Source** | [`backend/sixty_seven/shortlister.py`](../../../backend/sixty_seven/shortlister.py), [`search_client.py`](../../../backend/sixty_seven/search_client.py), [`agent.py`](../../../backend/sixty_seven/agent.py) |
| **Layer** | AI subsystem (`backend/`) |
| **Status** | Stable (Codex-authored · PROV-003 AI verdict receipts) |
| **Related** | [HLD](../high-level-design.md) · [screener-catalog.md](screener-catalog.md) · [scan-service-and-provenance.md](scan-service-and-provenance.md) · [storage-persistence.md](storage-persistence.md) · [security.md](security.md) · [configuration.md](configuration.md) |

## 1. Purpose & responsibilities

Find beaten-down stocks (≥67% off their available-history ATH, ≥100% upside back)
and let a Claude verifier approve a BUY **only** when web + Screener.in evidence
shows the fall is explained, resolved, and the company still has a profit record,
growth, and improving quarters.

**Two stages:**
- **`shortlister.py`** — cheap, deterministic, no network/LLM: pure price math producing `DrawdownCandidate`s.
- **`agent.py`** — Claude Agent SDK verifier with ONE tool (`research_company`) that fetches a Screener.in snapshot + SerpAPI Google snippets. `evaluate(...)` returns a `SixtySevenEvaluationResult` = the validated `SixtySevenVerdict` **plus a trusted PROV-003 `AIProvenance` receipt** (model, semantic prompt version, prompt/evidence/context SHA-256, sanitized URLs); `verify(...)` is a thin compat wrapper returning just the verdict.
- **`search_client.py`** — a tiny SerpAPI wrapper (fixed endpoint, no SSRF surface) returning normalized `SearchResult`s as untrusted evidence.

The screener (`screeners/sixty_seven_ka_funda.py`) streams an `AIEvaluationRecord` to the scan service's `ai_evaluation_callback` for **every** decision — approved, rejected, or error — so the `ai_evaluations` ledger is the durable audit trail (only an approved decision also becomes a shortlisted `scan_results` BUY).

## 2. Position in the system

```mermaid
sequenceDiagram
    participant Screener as sixty_seven_ka_funda
    participant Gate as shortlist_universe_frames
    participant Agent as SixtySevenAgent.verify
    participant Tool as research_company
    participant SI as screener.in
    participant Serp as SerpApiClient
    Screener->>Gate: frames {symbol: candles}
    Gate-->>Screener: DrawdownCandidate[] (≥67% down)
    loop each candidate
        Screener->>Agent: evaluate(symbol, candidate)
        Agent->>Tool: research_company(symbol) [bound symbol]
        Tool->>SI: fetch_company_data (cached)
        Tool->>Serp: 3 focused queries
        Tool-->>Agent: JSON evidence [source_policy: evidence only]
        Agent->>Agent: prompt-injection scan, hash evidence, HMAC-sign cache
        Agent-->>Screener: SixtySevenEvaluationResult [verdict + AIProvenance receipt]
        Screener-->>Screener: ai_evaluation_callback -> ai_evaluations [approved/rejected/error]
    end
```

## 3. Public interface

| Symbol | Contract |
|---|---|
| `shortlist_candidate(symbol, candles, *, drawdown_threshold_pct=67.0, upside_threshold_pct=100.0)` | `DrawdownCandidate | None`; ATH = highest high over available history. |
| `shortlist_universe_frames(frames, ...)` | `list[DrawdownCandidate]` preserving universe order (deterministic). |
| `DrawdownCandidate` | frozen: `symbol, ath_price, ath_date, latest_close, signal_date, drawdown_pct, upside_to_ath_pct` + `to_prompt_dict()`. |
| `SixtySevenAgent(model, cache=None, *, runner=None, search_client=None, fast_mode=False)` | `MAX_TURNS=6`; `get_cached_agent()` reuses one per (model, fast_mode); HMAC signing key from `ai_cache_integrity`. |
| `.evaluate(symbol, candidate, *, force_refresh=False, search_result_count=5) -> SixtySevenEvaluationResult` | Main entry: validated verdict **+ trusted `AIProvenance` receipt + `validated_verdict_json`**. Malformed/missing/injected evidence → `verdict=None` + an `error` receipt. Signs the verdict cache; `_cached_evaluation` verifies + cross-checks (prompt/context hash, model) on read, recomputing on tamper. |
| `.verify(symbol, candidate, ...) -> SixtySevenVerdict` | Compat wrapper over `evaluate`; raises `FundamentalsAgentError` on an error result. |
| `SixtySevenVerdict` / `SixtySevenEvaluationResult` | Verdict: `symbol, approved, fall_reason_category, 6 core flags, confidence, evidence[], rejection_reason, summary, model_used` (**`model_validator`: `approved` ⇒ all core flags True**). Result: `verdict|None`, `provenance` (`AIProvenance`), `validated_verdict_json`, `error_type`. |
| `sixty_seven_provenance_fingerprints(model, symbol, candidate)` | `(prompt_sha256, context_sha256)` — deterministic hashes stamped into the receipt; the cache key uses the prompt hash plus a stable candidate-facts digest. |
| `SerpApiClient(api_key=None, session=None)` · `.search(query, max_results=5)` | Fixed `ENDPOINT`; `SerpApiSetupError`/`SerpApiSearchError`; India-localized (`gl=in,hl=en`). |

## 4. Key design decisions & trade-offs

| Decision | Rationale | Alternative rejected |
|---|---|---|
| **Cheap gate → AI only on survivors** | 67% drawdown is pure price math; only a handful of stocks reach the costly verifier. | AI on all — expensive. |
| **All scraped/search text is *evidence*, never instructions** | Explicit `source_policy` in the tool payload + system prompt — prompt-injection posture (AI-003). | Treat as context to follow — injection risk. |
| **External evidence scanned for prompt injection** | Before trusting research output, `evaluate` scans the scraped/search fields (not the app's own `source_policy`) for model-directed instructions and fails closed (`error` receipt) on a hit. | Rely on the system prompt alone — injection risk. |
| **Tool bound to ONE symbol; mismatched request rejected** | The model can't redirect research to a different company. | Trust the model's symbol arg — wrong-company analysis. |
| **`approved` requires ALL core flags (`model_validator`)** | An approved verdict can never be self-contradictory at the schema level. | Trust the boolean — inconsistent verdicts. |
| **ContextVars + `copy_context()` across the thread bridge** | The SDK tool API can't take extra args; bound symbol/refresh/count ride ContextVars, and `_run_sync` copies the context across the worker thread (a fresh thread starts empty). | Module globals / no copy — silent wrong binding. |
| **Fixed SerpAPI endpoint, links passed as data** | No arbitrary-URL fetch ⇒ no SSRF here; result links are never fetched server-side. | Scrape Google / fetch links — fragile + SSRF. |
| **Shared `FundamentalsCache` (`::sixty-seven` namespace)** | Reuses one Screener.in data cache + verdict store across all three AI agents without collisions. | Separate caches — duplicate fetches. |
| **`force_refresh` skips the read, rewrites at end** | A run that fails partway never destroys a good cached verdict. | Delete-then-refetch — data loss on failure. |
| **Tamper-evident PROV-003 receipt + HMAC cache** | The receipt stores hashed evidence + sanitized URLs + a semantic prompt version (never raw text); the disk verdict cache is HMAC-signed and re-validated (prompt/context hash, model) on read, recomputing on tamper. See [security.md](security.md). | Raw evidence / unsigned cache — leak + forgeable. |
| **Every decision is auditable** | `evaluate` always returns a receipt (approved/rejected/error); the screener persists each to `ai_evaluations`. | Persist approvals only — no audit of rejects/errors. |
| **Reuses fundamentals SDK plumbing/errors** | One Windows-safe bridge, one usage-limit path. See [fundamentals-ai.md](fundamentals-ai.md). | Duplicate — drift. |
| **Bounded validation-retry, fresh research per attempt (AI-004)** | A malformed/incomplete verdict is re-run up to `SCANNER_AI_MAX_ATTEMPTS` (default 2) via the shared `parse_with_retry`; each attempt **clears the `_RESEARCH_COLLECTOR`** so it re-researches cleanly and the "exactly one payload" invariant still holds. Only verdict-JSON parse/validation retries — **research-evidence failures (missing / malformed / prompt-injection) and SDK/usage-limit errors never retry** (a re-fetch yields the same evidence; injection must not be re-run). | Retry without resetting the collector — mixes payloads, breaks the one-payload invariant; retry research/injection failures — same bad evidence, re-running an injection. |

## 5. Failure modes / degradation

- Missing `SERPAPI_API_KEY` / SDK absent / usage-limit hit → `evaluate` returns an `error` result; the screener emits an `error` `AIEvaluationRecord` (persisted), records a compute failure, and **skips that candidate** (→ partial run). Unlike Technical Analysis it has **no** gate-only fallback, so a gate-passing stock produces no BUY row when AI research is unavailable — but the attempt stays auditable in `ai_evaluations`.
- Screener.in fetch fails → tool returns an error payload; the model rejects.
- Search fails → screener data still returned + error noted; verdict proceeds.
- SDK/CLI missing or usage limit → `FundamentalsAgentError` / `FundamentalsUsageLimitError` (shared); not retried.
- Malformed/invalid verdict JSON → re-run up to `SCANNER_AI_MAX_ATTEMPTS` (AI-004); still failing → `error` receipt `error_type="AIValidationError"`, screener tags `phase="ai_validation"`. Missing/malformed/injected research evidence → `MissingResearchEvidence` / `MalformedResearchEvidence` / `PromptInjectionEvidence`, **never retried**.

## 6. Configuration & dependencies

`SERPAPI_API_KEY`, `CLAUDE_AGENT_MODEL`, `SCANNER_AGENT_FAST_MODE`, `SCANNER_AI_MAX_ATTEMPTS` (default 2 — validation-retry budget), optional `SCANNER_AI_CACHE_SIGNING_KEY` (restart-stable verdict cache); **`ANTHROPIC_API_KEY` unset**. External: `requests` (SerpAPI), `claude-agent-sdk` (lazy), `pydantic`. Cache: shared `FundamentalsCache` (HMAC-signed verdict envelopes).

## 7. Testing

- [`tests/test_sixty_seven_shortlister.py`](../../../tests/test_sixty_seven_shortlister.py) — drawdown math, thresholds, malformed frames.
- [`tests/test_sixty_seven_search_client.py`](../../../tests/test_sixty_seven_search_client.py) — SerpAPI normalization, key redaction, error paths (fake session).
- [`tests/test_sixty_seven_agent.py`](../../../tests/test_sixty_seven_agent.py) — verifier via injected `runner`, the `approved`-invariant, symbol binding, caching.

## 8. Extension points

A new checklist flag = a `SixtySevenVerdict` field (+ the `model_validator` `required` tuple) + a prompt line. PROV-003 receipts are already persisted to `ai_evaluations`; richer evidence rides in `EvidenceReference`/`AIProvenance` with no schema change.
