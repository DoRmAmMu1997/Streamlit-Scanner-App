# ADR: Preserve complete candle history in every cache writer

Status: accepted for the approved cache-preservation package.

## Context

The cache has one Parquet file per `(symbol, security_id)`. A direct historical
request may cover only a few days, while the same file serves a ten-year scanner
window. Replacing the file with that narrow response loses unrelated history.
Atomic rename prevents partial files but does not prevent two callers from
publishing different merges based on the same old snapshot. Prefetch and repair
must participate in the same protocol as direct downloads.

## Decision

`backend/candle_cache.py` owns the shared write lock, content revision, and atomic
Parquet publisher. All three writers use these primitives without new dependencies.

1. Fetch vendor data outside the write lock. A slow vendor must not block another
   caller's local transaction for the same symbol.
2. For a nonempty download, acquire the per-file lock and re-read the current
   Parquet. Keep old rows outside the inclusive requested interval and overlay
   the answer inside it. Keep exact duplicate removal, while retaining rows that
   share a date but disagree on OHLCV for DATA-001/DATA-002 inspection.
3. Direct `get_daily_history` clips the raw answer to the requested interval
   before overlay and returns only that interval. `ensure_daily_history` retains
   its established unsolicited-correction behavior: a vendor row before the
   requested tail remains alongside the old row so the quality gate sees any
   conflict. Full-window prefetch branches use the same locked merge.
4. Serialize to a uniquely named temporary file in the destination directory,
   close it, and publish with `os.replace`. Always remove the temporary file on
   ordinary failure. The old Parquet remains intact if serialization or rename
   fails. An abrupt process kill can leave an inert `.tmp` file.
5. An empty answer does not alter the Parquet or `.firstbar` evidence. Existing
   empty-tail `.checked` behavior in prefetch remains unchanged. Read failures
   propagate for downloads and become failed repair outcomes, preserving the
   unreadable file. Readable empty/missing-axis/all-NaT frames retain prefetch's
   existing full-window recovery behavior.

The lock combines a canonical-path Python thread lock and an OS advisory lock
on a permanent sibling `.lock` file: `msvcrt` byte zero on Windows and `flock` on
POSIX. Never unlink the lock file while writers may run. Locking the Parquet
itself would lock the old file identity after replacement. Descriptor close
releases the OS lock on process exit. Windows lock-acquisition failure propagates
rather than falling back to an unlocked write.

## Repair transactions

Repair reads and hashes its input while holding the shared lock, then releases
it to plan, fetch, and validate. Immediately before publishing it reacquires the
lock and compares SHA-256 content revisions. An intervening writer causes a
`skipped` result with a retry explanation; neither the candidate nor its
`.repaired` marker is written. Digest comparison catches equal-size rewrites even
when modification times match. The improvement and trading-day drop-budget
checks still apply. Publication and the corresponding retry marker share the
same critical section.

Beginner note: a repair is a decision about a particular input file. If that
input changes while the vendor is answering, publishing the old decision could
delete a newly downloaded day. Retrying against the new file is safer than
merging a repair whose improvement and drop-budget checks no longer describe
the current input.

## Evidence and compatibility

`.firstbar` evidence is derived from the raw vendor response, never from the
merged cache or the clipped direct-call slice. A future first bar cannot create
evidence. Existing chronology, TTL, probe depth, cache binding, and strict
`allow_unpublished_tail=False` behavior are unchanged. Sidecar reads remain
fail-closed if a writer is interrupted.

Regression coverage includes forced and missing-boundary narrow refreshes,
empty answers, raw vendor extras, duplicate conflicts, independent thread and
process writers, every prefetch download branch, repair/download interleaving,
unreadable originals, and interrupted serialization/replacement. The loader
timeout regression uses worker events instead of a fragile elapsed-time limit.

## Trade-offs and limits

Re-reading a full symbol file and hashing repair input adds local I/O, accepted
to preserve complete history. Locks coordinate cooperating writers on one
filesystem; external programs that ignore this protocol can still overwrite
data. POSIX advisory locking requires a filesystem that implements `flock`.
No distributed storage protocol or power-loss durability guarantee is added:
atomic replacement guarantees complete visible files, not persistence after
loss of power. Overlapping successful downloads serialize in completion order.

Alternatives rejected: locking only rename still loses updates; holding a lock
across vendor calls delays all writers; timestamp/size checks can miss a changed
repair input; separate protocols for repair and prefetch leave bypasses.

Related: [data acquisition](components/data-acquisition.md),
[candle-cache repair](data-002-candle-cache-repair.md).
