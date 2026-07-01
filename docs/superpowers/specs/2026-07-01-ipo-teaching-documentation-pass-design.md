# IPO subsystem teaching-documentation pass

## Purpose

Make the complete IPO-001, IPO-002, and IPO-003 implementation approachable to
a beginner without changing runtime behavior. The pass replaces terse or
mechanically generated descriptions with explanations that teach each boundary,
invariant, and failure mode while the reader follows the real code.

## Scope

The pass covers the complete IPO subsystem rather than only the newest downloader:

- every module under `backend/ipo/`;
- IPO persistence helpers and ORM models in `backend/storage/`;
- the IPO filing job and its observability/configuration integration;
- every IPO Alembic migration;
- all IPO-focused tests, fixtures, fake HTTP objects, and nested helper functions.

Unrelated scanner, authentication, notification, UI, and strategy code remains
outside scope. Shared files are edited only where an IPO-owned symbol or the
immediately surrounding explanation needs improvement.

## Documentation standard

Each in-scope module, class, function, method, nested helper, fixture, and test
must have a meaningful docstring. A useful docstring explains the symbol's role
in the IPO workflow rather than restating its name. Longer or security-sensitive
functions also describe relevant inputs, return values, raised errors, state
changes, or transaction/resource ownership.

Inline comments explain decisions that are not obvious from Python syntax,
especially:

- DTO-versus-ORM separation and detached return values;
- score weights, missing-data behavior, and fail-closed verdict policy;
- filing identity versus downloaded-content identity;
- category-level atomicity, idempotency, ownership conflicts, and status ordering;
- URL, DNS, redirect, hostile-content, and response-size controls;
- transaction closure around network I/O and source-identity compare-and-set;
- cache containment, symlink rejection, hashing, fsync, and atomic rename;
- migration portability, CHECK constraints, and guarded downgrade behavior;
- what each test double simulates and which invariant each test proves.

Comments must explain *why* a branch or boundary exists. They must not narrate
obvious assignments, duplicate the docstring, claim behavior the code does not
implement, or turn into a parallel specification that can drift.

## Behavior and compatibility

This is a documentation-only change. Public contracts, SQL, migrations, scoring,
HTTP behavior, cache paths, event names, and test expectations remain unchanged.
Formatting may wrap long statements only when required by the repository's
existing style tools.

## Enforcement

Extend the IPO documentation-policy test so a structural AST audit covers every
in-scope module, class, function, method, nested helper, fixture, and test. The
guard checks presence, while human review checks that descriptions are specific
and useful; length alone is not treated as proof of quality.

## Verification and delivery

- Run the structural documentation audit and IPO-focused tests first.
- Run the complete pytest coverage gate, Ruff, mypy, compileall, and `git diff --check`.
- Confirm the final diff contains no runtime logic changes.
- Commit with Codex co-authorship, push to the existing IPO-003 branch, and
  monitor PR #84's Python and Docker checks through completion.
