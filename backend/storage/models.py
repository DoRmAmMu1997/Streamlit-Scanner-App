"""SCAN-001 — ORM schema for persisted scan runs and their results.

What this module is (and is NOT)
--------------------------------
This file is the **design deliverable for ticket SCAN-001**: it defines the
*shape* of two database tables using SQLAlchemy's ORM:

- ``scan_runs``    — one row per scan execution (who/what/when/outcome).
- ``scan_results`` — one row per shortlisted stock inside a run.

Together they let the app answer *"why did it shortlist this stock on date D?"*
long after the scan finished — without re-running today's (possibly changed)
data, universe, or model. That auditability is the whole point of SCAN-001.

This module deliberately contains **schema only**. It has:
- NO database engine, NO connection string, NO `Session`,
- NO `create_all()` call, NO Alembic migration,
- NO repository / query helpers, NO business logic.

All of that is **SCAN-002 (owner: Codex)**. See the big "NEXT: SCAN-002" comment
block at the bottom of this file for an exact, fill-in-the-blanks checklist.

Beginner note on the ORM (Object-Relational Mapper)
---------------------------------------------------
SQLAlchemy lets us describe a database table as a normal Python class. Each
``Mapped[...]`` attribute becomes a column. At runtime SQLAlchemy can both
(a) create the matching table in SQLite/Postgres, and (b) turn query rows back
into ``ScanRun`` / ``ScanResult`` objects. We get one definition that serves as
the schema, the validation surface, and the Python type — instead of hand-written
SQL scattered across the app.

Portability note (SQLite first, Postgres later)
-----------------------------------------------
The tech-lead's roadmap is "SQLite locally, Postgres in deployment". Every type
choice below is intentionally portable across both engines (see the per-column
comments). Where the two databases disagree, the difference is called out inline.
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """The shared declarative base for every ORM model in the app.

    Beginner note: SQLAlchemy collects every table defined on this ``Base`` into
    ``Base.metadata``. SCAN-002's migration/`create_all()` step will hand exactly
    that metadata object to the database to create the tables. Keeping a single
    ``Base`` here means sibling tables can live alongside these and still be
    created together — the OBS-003 ``audit_logs``/``app_config`` tables below are
    exactly that (future tables: users, watchlists, ...).
    """


class ScanStatus(enum.Enum):
    """Lifecycle state of one scan run.

    Why an enum instead of a free-form string: a scan can only ever be in one of
    these states, and typos like ``"finished"`` vs ``"complete"`` would quietly
    break the history page's filters. The enum makes the allowed set explicit and
    self-documenting.

    The stored database value is the lowercase ``.value`` (``"running"``), not the
    Python name (``"RUNNING"``) — see ``values_callable`` on the column below.
    """

    RUNNING = "running"  # Scan started; not finished yet.
    SUCCESS = "success"  # Every symbol scanned without a fatal error.
    PARTIAL = "partial"  # Finished, but some symbols failed (details in error_message / results).
    FAILED = "failed"    # Aborted before producing a usable result set.


class ForwardReturnStatus(enum.Enum):
    """Lifecycle state of one forward-return measurement (VALID-001).

    A forward return cannot always be computed the moment a signal is stored: the
    holding window may not have elapsed yet, or the future bars an entry/exit need
    may simply not exist (a delisted or halted stock). Modelling that as an explicit
    status — rather than a bare NULL price — is what lets the VALID-002 calculator be
    safely *re-run*: ``pending`` rows are retried as data arrives, ``computed`` rows
    are skipped, and ``insufficient_data`` rows are recorded as a permanent fact
    instead of being mistaken for "not tried yet".

    The no-lookahead rule lives in this enum: a row only becomes ``computed`` once the
    exit bar genuinely exists in history. Like ``ScanStatus``, the stored value is the
    lowercase ``.value`` (see ``values_callable`` on the column below).
    """

    PENDING = "pending"                      # Window not elapsed yet; retry later.
    COMPUTED = "computed"                     # Entry+exit bars existed; return measured.
    INSUFFICIENT_DATA = "insufficient_data"   # No entry/exit bar (delisted/halted/gap).


# A primary-key integer type that adapts to the database engine:
#   * BIGINT on "real" databases (Postgres) so the id space is effectively unlimited.
#   * plain INTEGER on SQLite so the column becomes an alias of SQLite's built-in
#     ``rowid`` and auto-increments as beginners expect.
# Without the SQLite variant, "BIGINT PRIMARY KEY" is NOT treated as a rowid alias
# in SQLite and would not auto-increment the way INTEGER PRIMARY KEY does. Defining
# the rule once here keeps both tables consistent.
BigIntPrimaryKey = BigInteger().with_variant(Integer, "sqlite")


class ScanRun(Base):
    """One execution of one screener over one universe — the audit header.

    Read this row as a sentence: *"At ``started_at`` we ran ``screener_key`` over
    ``universe_key`` with ``params_json`` against data dated ``data_snapshot_date``;
    it ended ``finished_at`` with ``status``."* Everything needed to reproduce or
    explain the run lives here; the per-stock hits live in ``ScanResult``.
    """

    __tablename__ = "scan_runs"

    # Surrogate primary key. Auto-increments; callers never set it by hand.
    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)

    # When the run began / ended. Stored timezone-aware in UTC so timestamps from a
    # laptop, a cron box, and a cloud server all compare correctly.
    # ``finished_at`` is NULL while a run is still in progress or if it crashed.
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="UTC start time of the run"
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="UTC end time; NULL while running"
    )

    # Lifecycle state. Indexed because the history page filters by it ("show failed runs").
    # native_enum=False stores the value as a small VARCHAR + CHECK constraint on BOTH
    # engines; that avoids Postgres's native ENUM type, whose values can only be changed
    # with an ALTER TYPE migration. Easier to evolve, identical behaviour for our needs.
    status: Mapped[ScanStatus] = mapped_column(
        Enum(
            ScanStatus,
            name="scan_status",
            native_enum=False,
            # SQLAlchemy only emits the VARCHAR CHECK constraint when this flag
            # is true. The design doc promises database-level status protection,
            # so SCAN-002 pins that behavior with a regression test.
            create_constraint=True,
            length=16,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ScanStatus.RUNNING,
        index=True,
        comment="running | success | partial | failed",
    )

    # WHAT was run. Indexed because the history page groups/filters by screener and
    # universe ("all Technical-Analysis runs", "all nifty_500 runs").
    screener_key: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True, comment="Screener registry key, e.g. 'envelope'"
    )
    universe_key: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True, comment="Universe key, e.g. 'nifty_500'"
    )

    # SCAN-004: how many symbols the universe contained when the run started.
    # "Scanned" means "handed to the screener" — the shortlisted count is derived
    # from scan_results instead. Nullable because runs recorded before this column
    # existed have no value; the history page shows those as "—".
    symbols_scanned: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Universe size handed to the screener; NULL for pre-SCAN-004 runs",
    )

    # The exact screener parameters used (thresholds, lookback, max_symbols, ...).
    # Stored as JSON so any screener's parameter shape fits without a schema change.
    # This is half of "reproducibility": same params + same data snapshot = same result.
    params_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Screener params snapshot for replay"
    )

    # DATA-001B: versioned candle-quality receipt for this run. Kept nullable so
    # old runs and non-scan bootstrap tests do not need a synthetic empty object.
    data_quality_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Candle data-quality receipt (DATA-001)"
    )

    # The date the underlying candle data was current as of. The OTHER half of
    # reproducibility: it lets an auditor say "this used data through 2026-06-03",
    # so a stale-data symbol can never masquerade as a fresh signal.
    data_snapshot_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True, comment="Trading date the candle data was current to"
    )

    # Code provenance: which app version / git commit produced this run. Invaluable
    # when an AI-generated change quietly alters indicator math between runs.
    app_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="App/release version string"
    )
    git_commit_sha: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="Full 40-char git commit SHA"
    )

    # Who/what started the run: "ui:user@example.com", "cron", "cli", etc. Free text
    # by design; OBS-003's dedicated ``audit_logs`` table now records richer
    # per-action identity, so this column stays a lightweight run-origin label.
    triggered_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Origin of the run (ui/cron/cli/...)"
    )

    # Human-readable failure detail for PARTIAL/FAILED runs. Text (not String) because
    # a traceback summary or a list of failed symbols can be long.
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Why the run failed / what partially failed"
    )

    # ORM relationship: ``run.results`` gives the list of ScanResult rows.
    # cascade="all, delete-orphan" means deleting a run through the ORM also deletes its
    # results (no orphaned children). ``passive_deletes=True`` lets the database's own
    # ON DELETE CASCADE (see the FK below) do the work in one statement when available.
    results: Mapped[list[ScanResult]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    ai_evaluations: Mapped[list[AIEvaluation]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # Friendly output in logs / the Python shell.
        return (
            f"ScanRun(id={self.id!r}, screener_key={self.screener_key!r}, "
            f"universe_key={self.universe_key!r}, status={self.status.value!r})"
        )


class ScanResult(Base):
    """One shortlisted stock produced by a scan run — the audit line item.

    The first five fields mirror the app's existing screener output contract
    (``backend.scanner_base.COMMON_RESULT_COLUMNS`` = symbol, rating, signal_date,
    close, reason), so persisting a screener row is a near 1:1 copy. The two JSON
    columns then capture everything else — which is how one table serves BOTH
    deterministic screeners AND the Claude-Agent-SDK screeners.
    """

    __tablename__ = "scan_results"

    # Composite index for the forward-return workload (VALID-001/VALID-002): the
    # validator looks up "every signal for symbol S on/after date D" to fetch the bars
    # that follow each signal. The single-column ``symbol`` index alone can't serve that
    # date-bounded scan efficiently. SCAN-001 deliberately parked this index until the
    # VALID-* work actually queried by date — this is that moment.
    __table_args__ = (
        Index("ix_scan_results_symbol_signal_date", "symbol", "signal_date"),
    )

    # Surrogate primary key (auto-increments).
    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)

    # Link back to the parent run. Indexed (the most common query is "give me every
    # result for run X"). ondelete="CASCADE" tells the DATABASE to delete these rows
    # when the parent run is deleted — see also the relationship cascade above.
    # SQLite only enforces this when `PRAGMA foreign_keys=ON` is set on the connection;
    # SCAN-002 should set that pragma (the ORM-level cascade works regardless).
    run_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent scan_runs.id",
    )

    # The stock. Indexed so "every time RELIANCE was shortlisted, across all runs"
    # (the signal-history / validation use case) stays fast.
    symbol: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="Trading symbol, e.g. 'RELIANCE'"
    )

    # The candle date the signal fired on. Nullable because a few AI/insight rows are
    # not tied to a single bar. The (symbol, signal_date) composite index that the
    # forward-return workload needs is declared in __table_args__ above (VALID-001).
    signal_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True, comment="Date the signal triggered"
    )

    # Price at the signal. Numeric (fixed-point), never float: money math with binary
    # floats accumulates rounding error. Numeric(18, 4) handles Indian large-caps with
    # paisa precision and room to spare.
    close_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="Close price at the signal"
    )

    # Verdict label (BUY / SELL / WATCH / ...). Kept as a short string rather than an
    # enum because each screener — especially the AI ones — may use its own vocabulary.
    rating: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="Screener verdict label, e.g. 'BUY'"
    )

    # Optional ranking score populated by RANK-002. It remains nullable because
    # a row can be unscorable or come from historical data recorded before RANK-002.
    final_score: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 2), nullable=True, comment="Composite rank score (RANK-002)"
    )

    # Short human explanation ("oversold reversal with improving volume").
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Plain-English reason for the shortlist"
    )

    # The complete, unmodified screener output row (all the EXTRA_RESULT_COLUMNS too).
    # JSON keeps EVERY screener's bespoke fields without a per-screener table. This is
    # what makes one schema fit both deterministic and AI screeners.
    raw_result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Full raw screener output row"
    )

    # The "receipts" for this BUY (the PROV-001 contract): triggered rules, indicator
    # values, and — for AI rows — model name, prompt version, source labels, evidence
    # hashes. Stored as JSON so PROV-001 can evolve the contract without a migration.
    provenance_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Provenance / evidence (PROV-001 contract)"
    )

    # When this row was written. ORM-side default (applied on flush) so it is always
    # tz-aware UTC regardless of database. SCAN-002 may add a DB server_default too.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        comment="UTC row-creation time",
    )

    # ``result.run`` walks back to the parent ScanRun object.
    run: Mapped[ScanRun] = relationship(back_populates="results")

    # VALID-001: the forward-return measurements for this signal (one per horizon —
    # 20/60/120 trading days). Same cascade contract as ScanRun.results: deleting a
    # signal removes its forward-return rows so no orphans survive.
    forward_returns: Mapped[list[SignalForwardReturn]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"ScanResult(id={self.id!r}, run_id={self.run_id!r}, "
            f"symbol={self.symbol!r}, rating={self.rating!r})"
        )


class SignalForwardReturn(Base):
    """One forward-return measurement for one stored signal at one horizon (VALID-001).

    Read this row as a sentence: *"Signal ``result_id`` was entered at ``entry_price``
    on ``entry_date`` and, ``horizon_days`` trading days later, was worth ``exit_price``
    on ``exit_date`` — a ``forward_return_pct`` move, versus ``benchmark_return_pct`` for
    its index, for ``excess_return_pct`` of alpha."* The parent ``ScanResult`` says what
    was shortlisted and why; this table says **what happened next**, which is the whole
    point of EPIC 5.

    One ``ScanResult`` fans out to several rows here — one per horizon (20 / 60 / 120
    trading days). The ``(result_id, horizon_days)`` uniqueness lets the VALID-002
    calculator be re-run idempotently: it upserts a ``pending`` row to ``computed`` once
    the window elapses, rather than appending a duplicate each pass.

    Design boundary (mirrors SCAN-001): this module is **schema only**. The VALID-001
    migration creates the table; VALID-002 owns the forward-return *math* (next-open
    entry, Nth-bar exit, MAE/MFE, benchmark alignment), the service that loads candles
    and fills these rows, and the repository helpers. See the design doc for the methodology
    (``docs/architecture/valid-001-forward-return-validation.md``) and the handoff brief
    (``docs/architecture/valid-002-handoff.md``).
    """

    __tablename__ = "signal_forward_returns"

    # A signal is measured at most once per horizon. The unique constraint both enforces
    # that and — because (result_id, horizon_days) leads with result_id — serves the
    # "all horizons for this signal" lookup, so no separate result_id index is needed.
    __table_args__ = (
        UniqueConstraint("result_id", "horizon_days", name="uq_forward_return_result_horizon"),
    )

    # Surrogate primary key (auto-increments).
    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)

    # The signal this measurement belongs to. ondelete="CASCADE" so deleting a run (and
    # thus its results) also clears these rows; the ORM relationship cascade matches.
    result_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("scan_results.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent scan_results.id",
    )

    # The horizon, in TRADING days (not calendar days), counted off the symbol's own
    # candle frame so market holidays never inflate the count. 20 / 60 / 120 per EPIC 5.
    horizon_days: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Forward window in trading days (e.g. 20/60/120)"
    )

    # Lifecycle. Indexed because the calculator's core query is "give me the pending rows".
    # native_enum=False stores a small VARCHAR + CHECK on both engines (same rationale as
    # ScanRun.status) so the allowed set is evolvable without an ALTER TYPE migration.
    status: Mapped[ForwardReturnStatus] = mapped_column(
        Enum(
            ForwardReturnStatus,
            name="forward_return_status",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ForwardReturnStatus.PENDING,
        index=True,
        comment="pending | computed | insufficient_data",
    )

    # Entry = the bar AFTER the signal (next-day open); exit = the bar `horizon_days`
    # trading days on (its close). Both NULL until the row is computed. Storing the dates
    # (not just the prices) makes the measurement auditable and lets the benchmark be
    # aligned to the exact same window.
    entry_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True, comment="Date of the entry bar (next trading day after signal)"
    )
    exit_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True, comment="Date of the exit bar (horizon_days trading days later)"
    )

    # Numeric (fixed-point), never float, for every price/percentage — money math with
    # binary floats accumulates rounding error. Numeric(18,4) matches close_price.
    entry_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="Open of the entry bar"
    )
    exit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="Close of the exit bar"
    )

    # Signed percentage move from entry to exit. Numeric(9,4) holds returns far beyond
    # any realistic single-name move (±99999.9999%).
    forward_return_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4), nullable=True, comment="(exit-entry)/entry * 100"
    )

    # The benchmark leg (VALID-001 "benchmark-relative return"). benchmark_key records
    # WHICH index was used (resolved per-universe) so the comparison is reproducible even
    # if the benchmark mapping changes later. All NULL when no benchmark data is available
    # for the window — the forward return is still valid; only the relative leg degrades.
    benchmark_key: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Benchmark used, e.g. 'nifty_50'"
    )
    benchmark_entry_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="Benchmark price on entry_date"
    )
    benchmark_exit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="Benchmark price on exit_date"
    )
    benchmark_return_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4), nullable=True, comment="Benchmark return over the same window"
    )
    excess_return_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4), nullable=True, comment="forward_return_pct - benchmark_return_pct"
    )

    # Path metrics over the holding window [entry, exit], relative to entry_price
    # (EPIC 5 "max adverse / favorable excursion"). MAE is the worst drawdown the trade
    # would have shown; MFE the best unrealised gain.
    max_adverse_excursion_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4), nullable=True, comment="Worst intra-window move vs entry (MAE)"
    )
    max_favorable_excursion_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4), nullable=True, comment="Best intra-window move vs entry (MFE)"
    )

    # When the measurement was last (re)computed; NULL while still pending. Distinct from
    # created_at so a re-run that flips pending → computed is visible in the audit trail.
    computed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="UTC time the row was last computed"
    )

    # When this row was first written. tz-aware UTC, ORM-side default, same as ScanResult.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        comment="UTC row-creation time",
    )

    # ``forward_return.result`` walks back to the parent ScanResult.
    result: Mapped[ScanResult] = relationship(back_populates="forward_returns")

    def __repr__(self) -> str:
        return (
            f"SignalForwardReturn(id={self.id!r}, result_id={self.result_id!r}, "
            f"horizon_days={self.horizon_days!r}, status={self.status.value!r})"
        )


class AIEvaluation(Base):
    """One validated AI verdict receipt associated with a scan run."""

    __tablename__ = "ai_evaluations"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('approved', 'rejected', 'error')",
            name="ck_ai_evaluations_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("scan_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    signal_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    verdict_label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    validated_verdict_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
    )

    run: Mapped[ScanRun] = relationship(back_populates="ai_evaluations")


class AuditLog(Base):
    """OBS-003 — one row per recorded user action (the audit trail).

    Where ``scan_runs`` answers *"what scans happened?"*, ``audit_logs`` answers
    *"who did what, and when?"* — logins, manual scans, config changes, CSV
    exports, and admin-page access. Each row carries the actor's email, a UTC
    timestamp, and a small JSON ``metadata_json`` blob that the recorder has
    already passed through the app's secret redactor, so a token can never become
    durable audit evidence.

    System actions that run before anyone signs in (the startup data refresh)
    record ``user_email = NULL``; the viewer renders those as "system".

    This is exactly the kind of sibling table the ``Base`` docstring anticipated:
    it shares the same declarative base, so one migration pass creates it
    alongside ``scan_runs``/``scan_results``.
    """

    __tablename__ = "audit_logs"

    # Surrogate primary key (auto-increments). Reuses the SCAN-001 BigInt/SQLite
    # variant so the id behaves the same on SQLite and Postgres.
    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)

    # When the action happened. Indexed because the audit viewer always orders
    # newest-first and filters by time window. tz-aware UTC like every other
    # timestamp in this schema, so laptop/cron/cloud rows compare correctly.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        index=True,
        comment="UTC time the action occurred",
    )

    # The stable event name (e.g. 'login_success'). Indexed because the viewer
    # filters by event type. A short String, not an enum, so a new tracked action
    # is one constant in backend.observability — no migration, no ALTER TYPE.
    event: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="Audit event name"
    )

    # The actor. Indexed for "everything <email> did" queries. Nullable because
    # some tracked actions (the startup data refresh) run before authentication;
    # those are system events with no user. 320 is the maximum email length.
    user_email: Mapped[str | None] = mapped_column(
        String(320),
        nullable=True,
        index=True,
        comment="Actor email; NULL for system events",
    )

    # Already-redacted, JSON-safe context for the action (screener_key, file_name,
    # changed setting, ...). JSON keeps the shape flexible per event without a
    # migration, mirroring ``scan_runs.params_json``. NEVER store raw secrets
    # here — the recorder routes this through ``normalize_secret_safe_json`` first.
    # The attribute is ``metadata_json`` (not ``metadata``) because SQLAlchemy's
    # DeclarativeBase reserves the ``metadata`` attribute name.
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Redacted, JSON-safe action metadata"
    )

    def __repr__(self) -> str:
        return (
            f"AuditLog(id={self.id!r}, event={self.event!r}, "
            f"user_email={self.user_email!r})"
        )


class AppConfig(Base):
    """OBS-003 — durable overrides for the admin runtime-config form.

    The app's settings are read from environment variables (see
    ``backend.config.settings``). This tiny key/value table lets an admin change
    a small whitelist of *operational* settings (currently ``LOG_LEVEL`` /
    ``LOG_FORMAT``) at runtime: the value is stored here and re-applied into the
    process environment on startup, so ``get_settings()`` — which reads
    ``os.environ`` live — picks it up. The change itself is recorded as a
    ``config_changed`` audit row.

    Only non-secret operational keys are stored here on purpose; credentials and
    auth/infra settings are intentionally out of scope (see the OBS-003 design
    doc), so this table never becomes a secret store.
    """

    __tablename__ = "app_config"

    # The environment variable name being overridden (e.g. 'LOG_LEVEL'). The key
    # IS the identity, so it is the primary key — one override row per setting.
    key: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="Env var name being overridden"
    )

    # The override value as a raw env-style string; parsed/validated by the same
    # backend.config.settings parsers used at startup. Nullable so an override can
    # represent an explicit empty value.
    value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Raw env-style override value"
    )

    # Audit columns so the table is self-describing even outside audit_logs.
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
        comment="UTC time the override was last changed",
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(320), nullable=True, comment="Admin email who set the override"
    )

    def __repr__(self) -> str:
        return f"AppConfig(key={self.key!r}, updated_by={self.updated_by!r})"


class UserRole(Base):
    """AUTH-003 — one durable role assignment per user (viewer / analyst / admin).

    Where ``audit_logs`` records *what people did*, this table records *what a
    person is allowed to do*. AUTH-001 authenticates the Google identity; entry is
    authorized by the AUTH-002 env lists unioned with a valid row in this table.
    AUTH-003 then splits capabilities into read-only (viewer), produce-work
    (analyst), and operate-the-system (admin).

    The role store is database-driven on purpose: an admin can (re)assign roles at
    runtime from the admin Roles page without a redeploy. ``ADMIN_EMAILS`` (env)
    stays a bootstrap-admin floor so the very first admin always exists to populate
    this table — see ``backend.auth.roles.resolve_role`` for the precedence.

    Like ``app_config`` this is a tiny key/value-shaped table: ``email`` is the
    identity, so it is the primary key (one role per user). The allowed role names
    are pinned by a CHECK constraint on both engines (same rationale as
    ``ai_evaluations.outcome``) so a bad write can never store an unknown role.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        CheckConstraint(
            "role IN ('viewer', 'analyst', 'admin')",
            name="ck_user_roles_role",
        ),
    )

    # The normalized, lower-cased identity email — the exact form the auth gate
    # compares against ALLOWED_EMAILS/ADMIN_EMAILS. 320 is the maximum email
    # length, matching ``audit_logs.user_email``.
    email: Mapped[str] = mapped_column(
        String(320), primary_key=True, comment="Normalized lowercase user email"
    )

    # The role name. A short String + CHECK (not a native enum) so adding a future
    # role is a normal migration, not an ALTER TYPE — same pattern as scan_status.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="viewer | analyst | admin"
    )

    # The admin who last set this row (forensics for the role_changed audit event).
    # NULL for rows created by a seed/migration rather than a person.
    assigned_by: Mapped[str | None] = mapped_column(
        String(320), nullable=True, comment="Admin email who set this role"
    )

    # tz-aware UTC like every other timestamp in this schema. ORM-side defaults
    # (applied on flush) keep the value engine-independent, matching app_config.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        comment="UTC time the row was first written",
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
        comment="UTC time the role was last changed",
    )

    def __repr__(self) -> str:
        return f"UserRole(email={self.email!r}, role={self.role!r})"


# ============================================================================
# NEXT: SCAN-002 (owner: Codex) — implement the database layer on top of this
# schema. This file gives you the tables; SCAN-002 gives the app a way to talk
# to them. Suggested fill-in-the-blanks checklist:
#
# 1. backend/storage/database.py
#       - Read DATABASE_URL from the environment. Default to a local SQLite file:
#         `sqlite:///<DATA_DIR>/scanner.db` where DATA_DIR is
#         `backend.config.DATA_DIR` (so the DB lives beside the existing caches).
#         (`data/*.db` is already in .gitignore so the file never gets committed.)
#       - Create `engine = create_engine(DATABASE_URL, future=True)`.
#       - Create `SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)`.
#       - For SQLite, enable FK enforcement so the ON DELETE CASCADE above fires:
#             @event.listens_for(engine, "connect")
#             def _fk_on(dbapi_conn, _):
#                 dbapi_conn.execute("PRAGMA foreign_keys=ON")
#
# 2. Alembic migrations
#       - `alembic init` and point `target_metadata = Base.metadata` (import Base
#         from THIS module).
#       - Autogenerate the initial migration; confirm it creates `scan_runs` +
#         `scan_results` with the indexes declared above, then commit it.
#       - SQLite is the dev/test DB; Postgres is the deploy DB (DEPLOY-004).
#
# 3. backend/storage/repository.py  (REFACTOR-002 also references this)
#       - Thin functions so the UI/service never write raw SQL, e.g.:
#             create_scan_run(session, ...) -> ScanRun
#             finish_scan_run(session, run, status, error_message=None) -> None
#             save_scan_results(session, run, rows: list[dict]) -> None
#             get_latest_scan_runs(session, limit=50) -> list[ScanRun]
#             get_scan_results(session, run_id) -> list[ScanResult]
#
# 4. Wiring
#       - SCAN-003's scan service calls the repository to create a run, save results,
#         and mark the run finished. The Streamlit UI/history page reads via the
#         repository only.
#
# Constraints/types to preserve when implementing: the BigIntPrimaryKey SQLite
# variant, the tz-aware UTC datetimes, Numeric (not float) for prices, and the
# JSON columns. The schema round-trip test in
# tests/test_scan_persistence_models.py shows how to spin up a throwaway SQLite
# database from these models — reuse that pattern for SCAN-002's tests.
# ============================================================================
