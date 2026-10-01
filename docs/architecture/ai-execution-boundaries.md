# ADR — AI execution authorization and Agent SDK containment

**Status:** Accepted
**Date:** 2026-09-14 (amended 2026-09-28 by SEC-005: verbatim prompt delivery)
**Deciders:** repo maintainer (approved modernization Task 4)
**Relates to:** [AUTH-003 role model](auth-003-role-model.md) · [shared AI runtime](refactor-003-ai-runtime.md) · [IPO extraction AI](components/ipo-extraction-ai.md)

## Context

The scanner has four Claude Agent SDK clients: Check Fundamentals, Technical
Analysis, 67 Ka Funda, and IPO financial extraction. Two separate authority
boundaries needed to be explicit.

First, Streamlit retains `scan_cache`, widget state, and per-session fundamentals
verdicts across reruns. A user can run a scan as an Analyst, then be demoted to
Viewer or receive the fail-closed Viewer fallback when the role store is
unavailable. The retained rows and cached verdict are still useful read-only
evidence, but their session provenance cannot authorize another model call.

Second, the Agent SDK distinguishes between `allowed_tools` and `tools`.
`allowed_tools` permits named calls, including the app's in-process MCP tools;
`tools` selects the built-in tool surface loaded for the run. Relying only on
`permission_mode="dontAsk"` plus an allowlist leaves behavior coupled to SDK
defaults instead of stating that built-in shell/filesystem tools do not exist.
The installed SDK version was inspected on 2026-09-14 and its
`ClaudeAgentOptions` constructor accepts `tools: list[str] | ToolsPreset | None`.

IPO extraction also consumed the last assistant/result text without treating
`ResultMessage.is_error`, structured rate/billing events, CLI absence, or CLI
process failure as authoritative. A failed result can contain JSON-looking text.
Parsing that text could create a review proposal even though the provider marked
the run failed.

## Decision

### Current role controls execution, cached evidence controls display

`app.main()` passes the trusted `current_role` and `current_email` resolved at
the start of each rerun through `ui.scan_view._render_scan_output()` to
`ui.fundamentals_panel._render_fundamentals_panel()`.

The Fundamentals panel always permits a valid cached verdict to render. It shows
neither the initial analysis button nor the force-refresh button unless the
current role holds `RUN_SCAN`. If a widget event is nevertheless queued or
forged, the handler calls `require_capability(..., RUN_SCAN, ...)` immediately
before obtaining the cached `FundamentalAgent` resource or invoking it. A stale
scan or verdict therefore conveys no execution authority.

### MCP-only agents load no SDK built-ins

All four `ClaudeAgentOptions` constructions set `tools=[]`. Each retains its
reviewed `mcp_servers`, exact MCP `allowed_tools`,
`permission_mode="dontAsk"`, and `setting_sources=[]` values. The empty built-in
selection and the named MCP allowlist are complementary parts of the contract.

### Prompts reach the CLI verbatim (SEC-005)

All four constructions also set `verbatim_prompts=True` (claude-agent-sdk
0.2.158+). By default the Claude CLI pre-processes each user message before the
model sees it: an `@path` mention anywhere in the text is expanded into that
file's contents (likewise `@server:resource` MCP mentions), and a message that
starts with `/` is dispatched as a slash command. That happens before any tool
call, so `tools=[]`, `allowed_tools`, and `dontAsk` never see it. The IPO
extraction prompt inlines the company name scraped from SEBI listings, so a
filing named like `@/etc/passwd` could otherwise make the CLI read a local file
into the model context. Slash dispatch is not reachable today, because every
prompt starts with fixed app text, but the option removes it too. The other
three prompts carry only app-owned values (symbol, run mode, model name,
candle-derived price facts), and they use the same setting so the contract has
no per-agent exceptions.

The control has two halves, and they fail differently:

- **SDK.** The option is not feature-detected. An SDK older than 0.2.158
  rejects the keyword with `TypeError`, so the run fails closed. A test builds
  the real `ClaudeAgentOptions` so a pin downgrade fails CI first.
- **CLI.** Claude Code 2.1.248 or later must honor the flag. An older CLI
  *ignores* it and the SDK only logs a warning, so prompts are expanded as
  before. The pinned SDK 0.2.159 bundles CLI 2.1.281 and prefers that bundled
  binary over any `claude` on `PATH`. Do not point `cli_path` at, or deploy
  without the bundled binary on, an older CLI.

### Failed IPO runs return typed receipts before parsing

The IPO SDK runner drains the stream, remembers any rejected structured rate
event, assistant `rate_limit`/`billing_error`, or failed `ResultMessage`, and
requires a successful terminal `ResultMessage` before returning model text. An
empty successful terminal result may confirm preceding assistant text; EOF with
no terminal result cannot. It maps failures to stable `IpoExtractionError.code`
values:

| Condition | Code |
|---|---|
| Rejected rate event, billing/rate assistant error, HTTP 429, or quota-shaped failed process/result | `usage_limit_reached` |
| Bundled Claude CLI absent | `cli_not_found` |
| Claude CLI exits unsuccessfully for another reason | `agent_process_failed` |
| `ResultMessage.is_error` without a quota signal, or no terminal result | `agent_run_failed` |

The terminal-result rule is not IPO-specific. The Fundamentals, Technical and
67-ka-Funda runners call the shared `_require_terminal_result` helper
(`backend/fundamentals/fundamental_agent.py`) after draining their stream, so
a stream that ends with assistant text but no `ResultMessage` raises
`FundamentalsAgentError` (retried under each agent's existing policy) instead of
becoming a verdict. All four runners also share one unstructured usage-limit
classifier, `backend/agent_usage_limits.py` (`USAGE_LIMIT_MARKERS`,
`mentions_usage_limit`), so a billing refusal is recognized everywhere rather
than only by IPO extraction.

The public `propose_extraction()` boundary converts these errors to
`IpoExtractionErrorReceipt`. Receipts carry only the exception type and stable
code; provider text, stderr, paths, and model output are excluded. Because the
runner raises before returning text, `extract_json_object`, schema validation,
and proposal persistence are unreachable on these failures.

## Options considered

### Keep authorization only in the top-level Run button

Rejected. The Fundamentals model call is a separate action beneath retained
scan results, and Streamlit session state outlives role changes. Hiding its
buttons without a handler check would also trust widget state as authority.

### Clear all cached results after demotion

Rejected. Viewers are allowed to read results. Deleting useful evidence is not
needed to revoke execution; passing current authority separately preserves the
read-only product behavior.

### Rely on `allowed_tools` and SDK defaults

Rejected. Permission and tool loading are different SDK controls. Explicit
`tools=[]` makes the no-built-ins property reviewable and regression-testable.

### Scrub `@` and `/` from interpolated prompt text instead of `verbatim_prompts`

Rejected. An app-side escape list would have to track the CLI's expansion
syntax release by release, and it would mangle legitimate names. The SDK option
turns the pre-processing step off at its source.

### Feature-detect `verbatim_prompts` like `ThinkingConfigDisabled`

Rejected. Fast mode is an optimization, so a missing toggle can safely fall back
to the default. Verbatim delivery is a security control, and silently running
without it would reopen the pre-tool file-read path. Failing closed on an old
SDK is the intended behavior.

### Parse failed IPO output if it happens to validate

Rejected. Schema validity says nothing about whether the provider completed the
run. Terminal SDK status is the higher-authority fact, so failed text is never a
proposal candidate.

## Consequences

- Role changes take effect on the next Streamlit rerun without erasing retained
  scan rows or valid cached fundamentals verdicts.
- MCP tool availability remains unchanged, while SDK built-in capabilities are
  explicitly absent for all four agents.
- Prompt text can no longer trigger CLI file expansion or slash commands. The
  environment running the agents must carry claude-agent-sdk 0.2.158 or newer
  (the `constraints.txt` pin); an older install fails each AI run with
  `TypeError` until it is upgraded. The CLI half has no such tripwire (see
  above), which is why the bundled binary matters.
- Verbatim turns also skip the CLI's turn-start attachment pass (skill/tool
  listings and per-turn reminders arrive after the first tool call instead).
  This is expected to be harmless here: instructions come from the system
  prompt, `setting_sources=[]` already drops CLAUDE.md, and each agent has at
  most three small MCP tools. That expectation is checked only by a live agent
  run, because the unit tests replace the SDK with fakes.
- IPO batch jobs receive stable, secret-safe operational codes and continue to
  isolate one document's failure from sibling documents.
- Adding a new agent requires an explicit built-in `tools` selection,
  `verbatim_prompts=True`, and a test that captures the complete SDK options.
  A runner that consumes streamed results must establish terminal success
  before returning parseable text.

## Verification contract

- App orchestration tests retain `scan_cache` while passing a freshly resolved
  Viewer identity downstream.
- Fundamentals panel tests cover Viewer display with and without a cached
  verdict, both hidden controls, and denial before agent construction for an
  initial or forced action.
- Each agent test captures `ClaudeAgentOptions` and asserts `tools=[]` beside the
  unchanged MCP allowlist, `dontAsk`, empty setting sources, and
  `verbatim_prompts=True`.
- `tests/test_agent_terminal_result.py` constructs the real pinned
  `ClaudeAgentOptions(verbatim_prompts=True)`, because the runner tests' fake
  options classes accept any keyword and cannot detect an SDK downgrade.
- IPO tests feed valid proposal JSON through every failed SDK/CLI scenario,
  including assistant-text EOF and an empty stream, and assert a typed code plus
  an empty proposal table. A successful empty terminal result separately proves
  that prior assistant text remains a supported fallback.
