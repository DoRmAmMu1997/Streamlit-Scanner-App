# ADR: VALID-005 forward-return integrity and retry scheduling

Status: accepted and implemented. Follows VALID-002/004 and migration `20260906obs004a`.

## Context

A signal can have a completed short horizon and an unresolved long horizon. The
previous worker recalculated both, so a later provider failure could erase a
completed measurement. Preparing candles before validation could also discard a
bad entry bar or conflicting duplicate and silently move the measured window.
The job held one transaction across provider calls, selected oldest signal IDs
repeatedly, and never revisited a completed stock leg with an unavailable index.

## Decision

The service accepts the shared `backend.storage.SessionFactory`, a callable
returning a committing/rolling-back session context. A short read returns frozen
`ForwardReturnWorkItem` values. Each contains signal ID, symbol, date, universe,
unresolved stock horizons and separate `BenchmarkForwardReturnWork` values with
stored entry/exit dates and stock return. Universe resolution, history loading,
and calculation happen after the read context closes. All work for one signal
is written in one short transaction; no provider call holds that transaction.

The repository selects each requested signal once, ordered by its oldest
effective unresolved attempt time. A missing horizon uses `ScanResult.created_at`;
an existing unresolved row uses `last_attempted_at`, falling back to that signal
creation time. Signal date then ID break ties. The CLI defaults to 500 distinct
signals. Retried signals move behind older untouched work. Selection currently
materializes candidates in Python for portable ordering of missing horizons;
very large histories may justify a SQL aggregation implementation later.

Stock writes require `status = pending` in the UPDATE statement itself. A
missing row is inserted under the existing unique signal/horizon constraint;
a concurrent insert conflict rolls back a savepoint and retries the conditional
update. Completed stock dates, prices, returns, excursions, benchmark facts and
`computed_at` are immutable on ordinary retries. A Python check alone would race
with another worker finishing while the provider request is in flight.

Benchmark-only writes omit every stock column and require a computed stock row,
a missing benchmark return, and an active retry flag. Consequently a stale failed
retry cannot overwrite another worker's successful benchmark. Failed or malformed
configured index data keeps `benchmark_retry_pending`; success clears it. A
universe with intentionally no configured benchmark clears the flag and leaves
the queue without manufacturing a return. Benchmark-only work skips universe
mapping and stock history entirely.

Both calculators validate raw dated OHLC before preparation. Invalid timestamps,
nonfinite values, impossible ranges and conflicting daily duplicates are rejected.
Identical OHLC duplicates are accepted and canonicalized to one trading date;
volume is optional because it does not enter either return calculation. Ordinary
holiday gaps preserve the existing bar-count methodology. Pure calculators report
unavailable data; the service records malformed provider stock data as PENDING
because a repaired fetch may later succeed. Stock and index requests are bounded
by `signal_date..as_of`; future signals stay pending without fetching.

Horizon and limit arguments must be positive `numbers.Integral` values. Booleans,
zero, negatives and fractional values fail before database/provider access.
Horizons are deduplicated in caller order; an empty sequence is a no-op and the
Python API permits `limit=None`. The exported legacy stock-selection helper
retains its contract; workers use the new detached-work API.

## Schema and failure behavior

Migration `20260909valid005` follows `20260906obs004a` and adds nullable UTC
`last_attempted_at` and non-null `benchmark_retry_pending` with a false default.
Attempt times backfill from `computed_at`, then receipt `created_at`. Legacy
computed rows missing a benchmark return enter the retry queue. Existing stock
facts are untouched; downgrade removes only the scheduling metadata.

`ForwardReturnRunSummary.total_signals` counts successfully committed signals,
including benchmark-only attempts. Other counters count processed unresolved
measurements. If a later signal fails fatally, its entire transaction rolls back
and `ForwardReturnBatchError.summary` carries earlier committed progress. The
CLI returns failure with that progress instead of reporting zero or claiming that
the rolled-back signal completed. Transient provider failures remain normal
pending work, not fatal batch failures.

## Validation

Synthetic calculators cover malformed dates, entry/intermediate/exit OHLC,
nonfinite prices, duplicate conflicts, identical daily rows, optional volume and
holiday gaps. Service regressions exercise mixed terminal/pending horizons across
loader/universe/mapping/malformed failures, no future requests, benchmark-only
recovery and no-config clearing, fair repeated limit-one batches, and an
independent writer during both stock and index requests. A concurrent terminal
winner retains its receipt. Injecting a second-horizon write failure proves
per-signal atomicity and earlier-commit summaries. SQLite migration tests exercise
legacy backfill, preservation, downgrade, and model/schema parity.
