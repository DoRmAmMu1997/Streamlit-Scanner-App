# ADR: Bound transcript PDF parsing and Windows IPO workers

Status: accepted implementation of the approved parser containment package.

## Problem and decision

Download byte limits do not bound a PDF parser's object expansion or runtime.
Both transcript parsers, `pdfplumber` and optional `pypdf`, now execute in one
short-lived interpreter with a shared 60-second wall budget and 512 MiB memory
budget. The parent never retries parsing in-process. Failure returns unavailable
transcript text, preserving the existing agent behavior.

`extract_text` retains at most the first 30 pages and 40,000 characters. Callers
can request smaller limits but cannot increase these ceilings. New bounded text
caches use `.transcript-v1.txt`; legacy `.txt` files cannot prove page coverage
and are ignored. Cache reads and child result reads are bounded independently.

## Process and OS boundaries

- The transcript launcher starts the fixed lightweight worker script with the
  current interpreter's isolated `-I` mode. It does not import the broad
  fundamentals facade or application main module in the child.
- On Linux the child installs `RLIMIT_AS` before importing either parser. It
  also limits individual file size to the 256 KiB result ceiling.
- On Windows the parent creates a Job Object with a 512 MiB process commit
  limit and kill-on-close. The child waits for a three-byte start token; the
  parent sends it only after successful assignment. Setup or assignment failure
  kills the waiting child without opening the PDF.
- IPO retains its existing multiprocessing/JSON pipe, page/table/object budgets,
  and receipt contract. It reuses the same Windows Job Object helper and waits
  on a parent-owned start event before parsing. Its existing configurable
  `linux_address_space_bytes` value also supplies its Windows process limit.
- Transcript output is primitive JSON in a private temporary directory. The
  parent waits for child exit and reads at most 256 KiB plus one sentinel byte,
  then validates the exact text-only shape and character bound. This avoids a
  partial pipe frame holding the parent inside a blocking receive.
- Parser stdout/stderr are discarded. Cleanup kills and reaps a lingering
  transcript child before removing its private output directory. Job handles
  are closed on success and failure.

The Windows ctypes declarations use pointer-sized handles and `SIZE_T` fields.
Assignment requests only `PROCESS_SET_QUOTA | PROCESS_TERMINATE`. This follows
[Microsoft's assignment contract](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)
and [extended-limit structure](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information).

## Limits and verification

This is parser resource containment, not an OS security sandbox. Linux address
space and Windows committed memory are different OS measures. The worker still
has the launching user's filesystem/network privileges. Legitimate unusually
expensive PDFs can become unavailable instead of exhausting the app. Unsupported
transcript platforms or unsuccessful limit installation fail closed.

The pinned dependency set includes `pdfplumber`; `pypdf` remains optional and is
not newly installed. When absent, a failed primary parse yields empty text. The
fallback extraction route is tested using an explicit benign reader injected
inside the child; no requests Session controls parser containment.

Tests exercise a real 31-page PDF, character truncation, bounded and malformed
receipts, stalled child termination, temporary-file cleanup, failed limit setup,
failed Windows assignment, actual Windows limit readback and kill-on-close, and
the IPO start gate. A Linux-only integration test reads the actual child's
`RLIMIT_AS` value. Windows runs skip that test; hosted Linux must execute it.
No memory bombs, provider traffic, or production database are used in tests.
