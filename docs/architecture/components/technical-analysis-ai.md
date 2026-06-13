# LLD — Technical Analysis (AI) subsystem (`backend/technical`)

| | |
|---|---|
| **Component** | Claude-agent technical confirmation + price-action detectors |
| **Source** | [`backend/technical/technical_agent.py`](../../../backend/technical/technical_agent.py), [`patterns.py`](../../../backend/technical/patterns.py), [`knowledge.py`](../../../backend/technical/knowledge.py), [`tools.py`](../../../backend/technical/tools.py) |
| **Layer** | AI subsystem (`backend/`) |
| **Status** | Stable (ta-screener-expansion) |
| **Related** | [HLD](../high-level-design.md) · [screener-catalog.md](screener-catalog.md) (`technical_analysis`) · [indicators.md](indicators.md) · [fundamentals-ai.md](fundamentals-ai.md) · [sixty-seven-ka-funda-ai.md](sixty-seven-ka-funda-ai.md) · [configuration.md](configuration.md) |

## 1. Purpose & responsibilities

Confirm a **bullish** technical setup for the candidates the `technical_analysis`
screener's cheap pivot/pattern gate shortlists. A Claude Agent SDK agent calls
three deterministic in-process tools, then emits one validated `TechnicalVerdict`.

**Four files, one job each:**
- **`patterns.py`** — pure-pandas deterministic detectors: Fair Value Gaps (filled tracking), Double Top/Bottom (neckline-breakout confirmation), Order Blocks (mitigation tracking), Market structure (swing trend + BOS/CHoCH).
- **`knowledge.py`** — the agent's externalized expertise (role, level-relevance, setups, structure/HTF, tool guide, decision rules) composed by `build_system_prompt()` + the strict `FINAL_OUTPUT_INSTRUCTION`.
- **`tools.py`** — the in-process MCP server exposing `level_map`, `price_patterns`, `market_structure` over a per-call `TechnicalToolContext`.
- **`technical_agent.py`** — the agentic loop + `TechnicalVerdict` Pydantic schema + per-day verdict cache.

## 2. Position in the system

```mermaid
sequenceDiagram
    participant Gate as technical_analysis gate
    participant Agent as TechnicalAnalysisAgent.analyze
    participant Cache as FundamentalsCache (::technical)
    participant SDK as Claude Agent SDK
    participant Tools as in-process MCP tools
    Gate->>Agent: analyze(symbol, candles, major_levels, params)
    Agent->>Cache: get_verdict(symbol, model::technical::hash, date)
    alt cache hit
        Cache-->>Agent: TechnicalVerdict
    else miss
        Agent->>SDK: query(system+user prompt, allowed_tools, dontAsk)
        SDK->>Tools: market_structure / level_map / price_patterns
        Tools-->>SDK: deterministic JSON (per-call context)
        SDK-->>Agent: final JSON message
        Agent->>Agent: parse+validate TechnicalVerdict, normalize symbol
        Agent->>Cache: set_verdict(...)
    end
    Agent-->>Gate: TechnicalVerdict
```

## 3. Public interface

| Symbol | Contract |
|---|---|
| `TechnicalAnalysisAgent(model, cache=None, *, runner=None, fast_mode=False)` | `runner` injectable for tests (no CLI); `MAX_TURNS=8`. |
| `.analyze(symbol, candles, levels, *, params=None, force_refresh=False) -> TechnicalVerdict` | Cache-first; builds per-call `TechnicalToolContext`; runs one agentic pass; validates JSON. |
| `TechnicalVerdict` | `symbol, pattern (7 literals), confirmed, key_levels, confidence(0-10 via validator), trend, htf_alignment, relevant_levels[], caution, reasoning, signal_date, model_used`. |
| `tools.TechnicalToolContext.build(symbol, candles, levels, params)` | Resamples weekly + relevance-scores both timeframes; immutable per call. |
| `tools.build_technical_mcp_server(ctx)` | `(mcp_servers, allowed_tool_names)`; lazy SDK import; no-arg tools closing over ctx. |
| `knowledge.build_system_prompt()` / `FINAL_OUTPUT_INSTRUCTION` | Compose the prompt. |
| `patterns.detect_fair_value_gaps / detect_double_patterns / detect_order_blocks / detect_market_structure` | Deterministic detectors. |

## 4. Key design decisions & trade-offs

| Decision | Rationale | Alternative rejected |
|---|---|---|
| **Cheap gate first, AI only on survivors** | Bounds cost/latency — the SDK runs on a handful of candidates, not the whole universe. | AI on every stock — slow, expensive. |
| **Tools, not a candle dump** | Deterministic level/pattern/structure facts beat eyeballing a CSV and keep the verdict reproducible. | Pre-chew everything into the prompt — brittle, no agency. |
| **Per-call `TechnicalToolContext` (no agent mutable state)** | The screener confirms candidates in parallel on a shared agent; per-call context is race-free. | Agent instance state — data races. |
| **Cache key folds candles + levels + `params`** | Tool outputs are pure functions of these; a changed detector setting must invalidate the verdict (not just the date). `::fast` suffix isolates fast-mode verdicts. | Date-only key — stale verdicts on setting change. |
| **`allowed_tools` + `permission_mode="dontAsk"` + `setting_sources=[]`** | The agent can reach ONLY the 3 technical tools — never built-in filesystem/bash in a headless run. | Default tools — unsafe in a server. |
| **Externalized `knowledge.py`** | The agent's "brain" is reviewable prose, edited without touching Python. | Inline mega-string — unmaintainable. |
| **Confidence via `@field_validator`, not `Field(ge/le)`** | Keeps `minimum`/`maximum` out of the JSON schema (Claude rejects them on ints). | `Field(ge,le)` — schema Claude refuses. |
| **Long-only; bearish → `caution`** | Screener is long-only; bearish structure tempers, never triggers, a BUY. | Allow shorts — out of scope. |
| **Reuses fundamentals' SDK plumbing/cache/error types** | One Windows-safe async bridge, one usage-limit path, one on-disk cache (namespaced `::technical`). See [fundamentals-ai.md](fundamentals-ai.md). | Duplicate SDK code — drift. |

## 5. Failure modes / degradation

- SDK not installed / CLI missing → `FundamentalsAgentError` with setup hint; the screener degrades to **gate-only** (`source="deterministic"`).
- Plan usage limit → `FundamentalsUsageLimitError` (code `usage_limit_reached`); UI shows reset time.
- Unparseable final JSON → `FundamentalsAgentError` with a preview.
- `pattern="none"` can never be `confirmed` (normalized).

## 6. Configuration & dependencies

`CLAUDE_AGENT_MODEL` (default `claude-sonnet-4-6`); `SCANNER_AGENT_FAST_MODE` (disables extended thinking). **`ANTHROPIC_API_KEY` must stay UNSET** (Claude-subscription billing). External: `claude-agent-sdk` (lazy), `pydantic`.

## 7. Testing

- [`tests/test_technical_analysis_agent.py`](../../../tests/test_technical_analysis_agent.py) — agent loop via injected `runner`, JSON validation, normalization, caching.
- [`tests/test_technical_tools.py`](../../../tests/test_technical_tools.py) — tool payloads/context.
- [`tests/test_patterns.py`](../../../tests/test_patterns.py) — detectors vs synthetic fixtures.

## 8. Extension points

A new bullish setup = a `PatternName` literal + detector in `patterns.py` + prose in `knowledge.py` (+ gate trigger in the screener). New tool = add to `tools.py` and `TOOL_NAMES`. PROV-003 can record the model/prompt/tool evidence into `provenance_json`.
