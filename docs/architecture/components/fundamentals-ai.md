# LLD — Check Fundamentals (AI) subsystem (`backend/fundamentals`)

| | |
|---|---|
| **Component** | Per-stock fundamental-analysis agent + screener.in scraper + PDF reader + cache |
| **Source** | [`fundamental_agent.py`](../../../backend/fundamentals/fundamental_agent.py), [`screener_in_client.py`](../../../backend/fundamentals/screener_in_client.py), [`pdf_reader.py`](../../../backend/fundamentals/pdf_reader.py), [`fundamentals_cache.py`](../../../backend/fundamentals/fundamentals_cache.py) |
| **Layer** | AI subsystem (`backend/`) — the shared SDK plumbing reused by the other two AI agents |
| **Status** | Stable (migrated to Claude Agent SDK; structured forward outlook) |
| **Related** | [HLD](../high-level-design.md) · [technical-analysis-ai.md](technical-analysis-ai.md) · [sixty-seven-ka-funda-ai.md](sixty-seven-ka-funda-ai.md) · [ui-pages.md](ui-pages.md) · [security.md](security.md) · [configuration.md](configuration.md) |

## 1. Purpose & responsibilities

Power the UI's **Check Fundamentals** button: a Claude Agent SDK agent that
scrapes a stock's screener.in page and returns a structured `AgentVerdict`
(0–10 holistic rating, criteria breakdown, observations, three-part forward
outlook). This package is also the **reference implementation** the Technical
Analysis and 67 Ka Funda agents reuse (SDK runner, error types, usage-limit
detection, Windows-safe async bridge, on-disk cache).

**Four files:**
- **`fundamental_agent.py`** — the agent, its two in-process tools, the `AgentVerdict` Pydantic schema, criteria/universal modes, usage-limit handling, and the shared `AgentRunResult` / error classes.
- **`screener_in_client.py`** — `requests`+BeautifulSoup scraper (ratios, history, **HTMX peer table**, shareholding, announcements, concall metadata, median P/E).
- **`pdf_reader.py`** — concall-transcript download + text extraction (`pdfplumber` → `pypdf` fallback), size/page-capped.
- **`fundamentals_cache.py`** — the on-disk JSON cache (data + verdicts) shared by all three AI agents.

## 2. Position in the system

```mermaid
sequenceDiagram
    participant UI as Streamlit row → Check Fundamentals
    participant Agent as FundamentalAgent.check(symbol, mode)
    participant Cache as FundamentalsCache
    participant SDK as Claude Agent SDK
    participant T1 as fetch_company_data
    participant T2 as read_recent_concall_transcript
    UI->>Agent: check(symbol, mode=criteria|universal)
    Agent->>Cache: get_verdict(symbol, model::mode[::fast], data_date)
    alt cache hit
        Cache-->>Agent: AgentVerdict
    else miss
        Agent->>SDK: query(mode-aware prompt, 2 tools, dontAsk)
        SDK->>T1: fetch_company_data(symbol)  --> screener.in (cached)
        SDK->>T2: read_recent_concall_transcript(symbol)  [optional]
        T2->>T2: pdf_reader (pdfplumber→pypdf, capped)
        Note over T1,T2: TEST-003 — record raw evidence (audit) then prompt-injection scan; a hit returns a generic blocked response, never the hostile text
        SDK-->>Agent: final AgentVerdict JSON
        Agent->>Agent: parse+validate, normalize (stamp mode/total_criteria)
        Agent->>Agent: fail closed on poisoned evidence (PromptInjectionEvidence) — no verdict/cache
        Agent->>Cache: set_verdict
    end
    Agent-->>UI: AgentVerdict
```

## 3. Public interface

| Symbol | Contract |
|---|---|
| `FundamentalAgent(model, cache=None, *, runner=None, fast_mode=False)` · `.check(symbol, *, force_refresh=False, mode="criteria"|"universal") -> AgentVerdict` | `MAX_TURNS=8`; cache-first; `force_refresh` invalidates then refetches. |
| `AgentVerdict` | `symbol, mode, rating(0-10 via validator), passed_criteria_count, total_criteria, criteria_breakdown[], additional_observations[], summary_comments, forward_outlook, data_freshness, model_used`. |
| `ForwardOutlook` | `announcements_conclusion`, `concall_conclusion`, `overall_summary` (legacy string auto-migrated to `overall_summary`). |
| `CriterionResult` / `Observation` | Per-criterion verdict / agent-chosen observation (incl. mandatory Valuation P/E-vs-median). |
| `AgentRunResult`, `FundamentalsAgentError` (`code`), `FundamentalsUsageLimitError` (`resets_at`, `rate_limit_type`) | **Shared** across all three AI agents. |
| `screener_in_client.fetch_company_data(symbol)` / `ScreenerInFetchError` | Scrape one company page → structured dict (capped response text, HTMX peer fragment, announcements/concalls). |
| `pdf_reader.read_recent_concall_text(concalls)` / `download_pdf` / `extract_text` | Most-recent transcript text; `pdfplumber`→`pypdf`; char/page caps; `_looks_like_pdf` content sniff. |
| `FundamentalsCache` | `get/set_data` (30-day TTL, `SCANNER_FUNDAMENTALS_TTL_DAYS`), `get/set_verdict` (keyed `<SYMBOL>_verdict_<modelhash>_<data_date>`), `invalidate`. |

## 4. Two evaluation modes

- **Criteria mode** (Hemant Super 45 ∪ Nifty 100): all **nine** criteria (`total_criteria=9`).
- **Universal mode** (any other stock): the **seven** universal criteria, skipping Business Age + Market Leader (`total_criteria=7`), via `_UNIVERSAL_PROMPT_ADDENDUM`.

The mode is chosen by the UI from the row's universe; `_normalize_verdict` **enforces** `total_criteria` so a model miscount can't mislead the "X / Y passed" metric.

## 5. Key design decisions & trade-offs

| Decision | Rationale | Alternative rejected |
|---|---|---|
| **Claude Agent SDK on the Claude subscription** | Usage draws on the plan's monthly Agent SDK credit, not per-token API. **`ANTHROPIC_API_KEY` must stay UNSET** or the SDK silently bills the API account. | Per-token API key / OpenRouter — billing surprise / dead key. |
| **Two tools, `read_recent_concall_transcript` only when needed** | The transcript is ~8–15K tokens; the agent skips it when structured data is decisive (cost control). | Always read transcript — expensive. |
| **Tool outputs are untrusted evidence; symbol-bound** | System prompt + tool reject a mismatched symbol; scraped text is never followed as instructions (AI-003). | Trust scraped text — prompt-injection. |
| **External evidence quarantined before model exposure (TEST-003)** | Both tools record the raw screener JSON / concall transcript in a request-local audit collector, then scan it via the shared [`backend.security.prompt_injection`](../../../backend/security/prompt_injection.py) engine (Unicode/homoglyph normalization + instruction patterns). A hit returns a generic blocked response to the model — never the hostile text — and `check` then fails closed with `PromptInjectionEvidence` (no verdict, no cache write, no retry). Context is copied across the worker thread so the collector actually populates. | Let hostile transcript/scrape text enter the model context or rely on the system prompt alone — injection risk. |
| **`@field_validator` for `rating`/counts, not `Field(ge/le)`** | Keeps `minimum`/`maximum` out of the JSON schema (Claude rejects them on ints). | `Field(ge,le)` — Claude rejects schema. |
| **Holistic 0–10 rating ≠ pass count** | Expert weighted judgment (a stock can pass all yet rate 5, or fail two yet rate 8). | Count passes — naive. |
| **Three-part `forward_outlook` with provenance** | Separates announcements vs concall vs integrated view; empty `concall_conclusion` when the transcript wasn't read (no speculation). Legacy string migrated via `mode="before"` validator. | Free-form string — no provenance, breaks old caches. |
| **ContextVars for per-check symbol/refresh** | Cross `asyncio.to_thread` safely; a cached agent can't leak one session's choice into another. | Instance mutable state — cross-session leak. |
| **Windows ProactorEventLoop bridge** | The SDK spawns the Claude CLI subprocess; Streamlit/Tornado's SelectorEventLoop can't (`NotImplementedError`). | `asyncio.run()` — fails on Windows. |
| **Structured usage-limit detection** | `RateLimitEvent`/`AssistantMessage.error`/HTTP 429 → typed `FundamentalsUsageLimitError` (not string matching); UI shows reset time, cached verdicts keep working. | String matching only — brittle. |
| **`allowed_tools` + `dontAsk` + `setting_sources=[]`** | Agent reaches ONLY the two tools; never built-in fs/bash; ignores user CLAUDE.md. | Default tools/settings — unsafe headless. |
| **Bounded validation-retry on malformed output (AI-004)** | `check()` re-runs the agentic loop up to `SCANNER_AI_MAX_ATTEMPTS` (default 2) via the shared `parse_with_retry` when the verdict is unparseable/invalid, then raises `AIValidationError` (a `RuntimeError` the UI already catches). Only parse/validation retries — never SDK/CLI/usage-limit. | Reject on first malformed reply — wastes a recoverable click; retry SDK errors — wastes Agent SDK credit. |

## 6. Failure modes / degradation

- SDK/CLI missing → `FundamentalsAgentError` with setup hint (button shows the message).
- Plan limit exhausted → `FundamentalsUsageLimitError` (gentle notice + reset time); cached verdicts still served.
- screener.in fetch fails → tool returns an error payload; the agent surfaces the limitation honestly.
- No transcript → `read_recent_concall_text` returns `""`; the agent falls back to announcements + sector knowledge.
- Unparseable / invalid final JSON → re-run up to `SCANNER_AI_MAX_ATTEMPTS` (AI-004); still failing → `AIValidationError`, caught and shown by the Check Fundamentals panel. The public error contains only the attempt count and final exception type; raw model text is never included in the message or traceback cause.
- Prompt injection in scraped/transcript evidence (TEST-003) → the tool hands the model a generic blocked response and `check` fails closed with a `PromptInjectionEvidence` error receipt: **non-retryable** (re-running would re-fetch the same poisoned evidence), no verdict, no cache write. A payload-free `logger.warning` records the event; the hostile text is never logged.

## 7. Configuration & dependencies

`CLAUDE_AGENT_MODEL` (default `claude-sonnet-4-6`), `SCANNER_AGENT_FAST_MODE`, `SCANNER_AI_MAX_ATTEMPTS` (default 2 — validation-retry budget), `SCANNER_FUNDAMENTALS_TTL_DAYS`; **`ANTHROPIC_API_KEY` unset**. External: `claude-agent-sdk` (lazy), `requests`+`beautifulsoup4`, `pdfplumber`/`pypdf` (optional), `pydantic`. Caches under `DATA_DIR/cache/fundamentals/`.

## 8. Testing

- [`tests/test_fundamental_agent.py`](../../../tests/test_fundamental_agent.py) — agent loop via injected `runner`, modes, verdict validation/normalization, usage-limit + legacy-outlook migration, and **prompt-injection quarantine** (both tools block hostile screener/transcript text, `check` fails closed without leaking the payload, benign near-neighbors pass).
- [`tests/test_prompt_injection.py`](../../../tests/test_prompt_injection.py) — the shared detection engine + corpus, reused by this agent and the 67 Ka Funda agent ([`tests/fixtures/ai_prompt_injection_cases.json`](../../../tests/fixtures/ai_prompt_injection_cases.json)).
- [`tests/test_screener_in_client.py`](../../../tests/test_screener_in_client.py) — scraper parsing (HTMX peers, announcements, concalls, median P/E).
- [`tests/test_pdf_reader.py`](../../../tests/test_pdf_reader.py) — download caps, `pdfplumber`/`pypdf` fallback, content sniff.
- [`tests/test_fundamentals_cache.py`](../../../tests/test_fundamentals_cache.py) — TTL, keys, invalidate.

## 9. Extension points

A new criterion = a prompt line + a `CriterionResult` (and count adjustment). A new tool = register in `_default_run` + `allowed_tools`. The shared `AgentRunResult`/error types and `FundamentalsCache` are the seam any future AI agent should reuse (as Technical Analysis and 67 Ka Funda already do).
