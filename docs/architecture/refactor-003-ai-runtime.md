# ADR — REFACTOR-003: one shared sync bridge for the Claude-agent subsystems

**Status:** Accepted — amended by AI-006 (2026-07-10, see the Amendment section)
**Date:** 2026-07-07
**Deciders:** repo maintainer (PR review); authored by Claude (Fable) during the July 2026 whole-app review
**Relates to:** [audit-2026-06.md](audit-2026-06.md) "Deferred follow-ups" (Agent SDK boilerplate dedup) · TEST-003 (context-propagation bug class)

## Context

The three Claude-agent subsystems each carry a private `_run_sync` staticmethod that bridges
sync (Streamlit) code into the Agent SDK's async world:

- `backend/fundamentals/fundamental_agent.py` — copies the caller's `contextvars` context
  (symbol binding, force-refresh flag, prompt-injection evidence collector) into the worker.
- `backend/sixty_seven/agent.py` — same shape, same context copy.
- `backend/technical/technical_agent.py` — same shape, **without** the context copy; its
  docstring still says "Identical to the fundamentals agent's bridge", which stopped being true
  when TEST-003 fixed the context bug in the other two.

All three build a Windows `ProactorEventLoop` explicitly (the SDK spawns the Claude CLI as a
subprocess; Tornado's selector loop can't) inside a single-worker `ThreadPoolExecutor`. The June
2026 audit deferred deduplication because "a shared base class is a behavior-risk refactor that
deserves its own ticket". This is that ticket.

The concrete risk of the status quo is not aesthetics: the context-copy subtlety was a **real
bug once** (TEST-003 found `_REQUESTED_SYMBOL` and the evidence collector silently unset in
worker threads, which would have let a prompt injection bypass the fail-closed check). Three
private copies mean the next fix or subtlety lands in one file and quietly misses the others —
exactly how the technical agent's copy drifted.

## Decision

Extract **only the sync bridge** into `backend/ai_runtime.py` as
`run_agent_coroutine(coro)`, generic over the result type. Each agent keeps a one-line
delegating `_run_sync` staticmethod so its public/test surface is unchanged
(`tests/test_sixty_seven_agent.py` invokes `SixtySevenAgent._run_sync` directly).

The shared bridge **always** copies the caller's context — including for the technical agent,
which today has no context-bound tools. Copying an empty context is a no-op, and it means a
future context-bound tool on any agent inherits the safe behavior instead of the 2026-06 bug.

## Options considered

### Option A: shared bridge function only (chosen)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — ~40 lines move, three 3-line delegates remain |
| Behavior risk | Low — byte-equivalent logic; technical agent gains a no-op-today context copy |
| Test surface | Unchanged; existing agent suites remain the net |

**Pros:** kills the drift-prone copy of the one block with a real bug history; smallest diff.
**Cons:** the ~50-line lazy-SDK-import/options-construction blocks stay per-agent.

### Option B: full shared runner base class (options construction, retry loop, SDK import)

| Dimension | Assessment |
|-----------|------------|
| Complexity | High — the three agents' options, error taxonomies, and retry policies differ materially |
| Behavior risk | High — precisely the "behavior-risk refactor" the June audit warned about |

**Pros:** maximum dedup. **Cons:** forces unification of things that are genuinely different
(fundamentals' evidence errors vs technical's parse fallbacks vs 67's cache semantics); a big
review burden for marginal safety gain.

### Option C: leave as is

**Pros:** zero risk today. **Cons:** the technical agent's bridge has already drifted from the
other two once; the next `_run_sync` subtlety repeats TEST-003's near-miss.

## Trade-off analysis

The bridge is the only block that is (a) semantically identical across all three agents,
(b) security-relevant (context loss disables the injection quarantine's fail-closed path), and
(c) historically bug-prone. Options construction fails test (a) — the differences are real
domain differences, not copy-paste drift — so deduplicating it would trade review risk for no
safety gain. Option A takes exactly the shared-and-dangerous part and nothing else.

## Consequences

- Easier: future event-loop/context fixes land once; a new agent subsystem imports the bridge
  instead of copying 40 lines.
- Harder: nothing measurable; one extra import edge (`fundamentals`/`technical`/`sixty_seven`
  → `ai_runtime`, a leaf module with no backend imports).
- Revisit: if a fourth agent appears and the options-construction blocks converge naturally,
  Option B can be re-evaluated with evidence.

## Action items

1. [x] `backend/ai_runtime.py` with `run_agent_coroutine` (context copy + Proactor loop + single-worker executor).
2. [x] `tests/test_ai_runtime.py`: contextvar propagation, result/exception passthrough, sequential reuse.
3. [x] Migrate agents one commit each (technical → sixty_seven → fundamental), keeping delegating `_run_sync` staticmethods.
4. [x] Full gate suite + the three agent test suites green.

## Amendment — AI-006 (2026-07-10): the JSON extractor joins the shared runtime

Option A's accepted con was that everything outside the bridge "stays per-agent".
For the options-construction blocks that remains the right call (they differ for real
domain reasons — see the trade-off analysis above). But one of the leftover blocks,
`_extract_json_object`, failed the same three-part test that justified moving the
bridge: it was (a) **logic-identical** across all three agents (verified line-by-line
before the move — only docstrings differed), (b) **relied on for correctness** of every
verdict parse, and (c) **drift-prone in exactly the bridge's way** — two of the three
copies carried a "kept local so the agents stay independent" comment while being
character-for-character the same logic, which is copy-paste drift waiting for its
first inconsistent fix.

AI-006 therefore moved the single implementation into `backend/ai_runtime.py` as
`extract_json_object(text)`. Each agent imports it under its old private name
(`from backend.ai_runtime import extract_json_object as _extract_json_object`), so
call sites, per-agent parse-fallback behavior, and the agent test suites are all
unchanged. Extractor unit tests (fenced block, missing language tag, surrounding
prose, no-JSON/invalid-JSON/reversed-brace edges) live in `tests/test_ai_runtime.py`.

This does **not** reopen Option B: the extractor, like the bridge, is shared-and-
identical; the options-construction/retry/error-taxonomy blocks remain genuinely
different per agent and stay where they are.
