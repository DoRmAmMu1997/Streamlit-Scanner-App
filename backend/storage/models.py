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
    Boolean,
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


# ---------------------------------------------------------------------------
# IPO-001 — IPO domain persistence
# ---------------------------------------------------------------------------
# These tables intentionally store normalized facts and immutable evaluation
# receipts only. Scraping and raw-metric interpretation belong to later tickets.


class IpoIssue(Base):
    """Ownership root for one Indian IPO and all of its evidence/history.

    CHECK constraints mirror strict domain enums, exact Numeric columns preserve
    INR values, and relationships cascade issue deletion to owned facts and
    evaluations. ``sebi_company_key`` stays nullable for pre-ingestion/manual rows.
    """

    __tablename__ = "ipo_issues"
    __table_args__ = (
        CheckConstraint(
            "issue_type IN ('mainboard', 'sme', 'unknown')", name="ck_ipo_issues_issue_type"
        ),
        CheckConstraint(
            "status IN ('drhp_filed', 'rhp_filed', 'open', 'closed', 'listed')",
            name="ck_ipo_issues_status",
        ),
        CheckConstraint(
            "open_date IS NULL OR close_date IS NULL OR close_date >= open_date",
            name="ck_ipo_issues_date_order",
        ),
        CheckConstraint(
            "(price_band_low IS NULL OR price_band_low >= 0) AND "
            "(price_band_high IS NULL OR price_band_high >= 0) AND "
            "(price_band_low IS NULL OR price_band_high IS NULL OR price_band_high >= price_band_low)",
            name="ck_ipo_issues_price_band",
        ),
        CheckConstraint("lot_size IS NULL OR lot_size > 0", name="ck_ipo_issues_lot_size"),
        CheckConstraint(
            "fresh_issue_amount IS NULL OR fresh_issue_amount >= 0",
            name="ck_ipo_issues_fresh_amount",
        ),
        CheckConstraint("ofs_amount IS NULL OR ofs_amount >= 0", name="ck_ipo_issues_ofs_amount"),
        CheckConstraint(
            "source_confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_issues_source_confidence",
        ),
        Index("ix_ipo_issues_status_open_date", "status", "open_date"),
        Index("ux_ipo_issues_sebi_company_key", "sebi_company_key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sebi_company_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    open_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    close_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    price_band_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    price_band_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fresh_issue_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    ofs_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
    )

    documents: Mapped[list[IpoDocument]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )
    financials: Mapped[list[IpoFinancial]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )
    subscriptions: Mapped[list[IpoSubscription]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )
    scores: Mapped[list[IpoScore]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )
    manual_extractions: Mapped[list[IpoManualExtraction]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )
    extraction_proposals: Mapped[list[IpoExtractionProposal]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )
    enrichment_signals: Mapped[list[IpoEnrichmentSignal]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", passive_deletes=True
    )


class IpoDocument(Base):
    """Store filing identity separately from verified downloaded-byte provenance.

    IPO-002 owns URL/date/``record_hash`` metadata. IPO-003 owns the grouped
    content hash, relative path, UTC download time, and parse status. Database
    checks keep those trusted cache fields wholly present or wholly absent.
    """

    __tablename__ = "ipo_documents"
    __table_args__ = (
        UniqueConstraint(
            "issue_id", "document_type", "document_url", name="uq_ipo_documents_issue_type_url"
        ),
        CheckConstraint(
            "source_confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_documents_source_confidence",
        ),
        CheckConstraint(
            "record_hash IS NULL OR length(record_hash) = 64",
            name="ck_ipo_documents_record_hash_length",
        ),
        # Validate that content_sha256 is a 64-char lowercase hex digest at the DB
        # level. SQLite has no built-in regex, so instead of a pattern match we
        # strip every hex digit (0-9, a-f) with nested replace() calls and assert
        # the remainder is empty -- i.e. the string contained only hex characters.
        # This portable trick runs identically on SQLite and PostgreSQL. Keep this
        # SQL byte-identical to migration 20260630ipo003 so the ORM/Alembic parity
        # test passes.
        CheckConstraint(
            "content_sha256 IS NULL OR (length(content_sha256) = 64 "
            "AND content_sha256 = lower(content_sha256) "
            "AND replace(replace(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
            "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
            "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')",
            name="ck_ipo_documents_content_sha256",
        ),
        CheckConstraint(
            "parse_status IN ('not_downloaded', 'pending', 'download_failed')",
            name="ck_ipo_documents_parse_status",
        ),
        CheckConstraint(
            "page_count IS NULL OR page_count > 0",
            name="ck_ipo_documents_page_count",
        ),
        CheckConstraint(
            "(parse_status = 'pending' AND content_sha256 IS NOT NULL "
            "AND downloaded_at IS NOT NULL AND file_path IS NOT NULL "
            "AND page_count IS NULL) OR "
            "(parse_status IN ('not_downloaded', 'download_failed') "
            "AND content_sha256 IS NULL AND downloaded_at IS NULL "
            "AND file_path IS NULL AND page_count IS NULL)",
            name="ck_ipo_documents_download_metadata",
        ),
        Index("ix_ipo_documents_filing_date", "filing_date"),
        Index("ux_ipo_documents_record_hash", "record_hash", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_type: Mapped[str] = mapped_column(String(50), nullable=False)
    document_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    filing_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    record_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ``record_hash`` identifies SEBI listing metadata. ``content_sha256`` is
    # deliberately separate: it proves which exact PDF bytes reached disk.
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downloaded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Paths are stored relative to DATA_DIR, so moving a deployment volume does
    # not invalidate database rows and an absolute host path never leaks.
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_downloaded", server_default="not_downloaded"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )

    issue: Mapped[IpoIssue] = relationship(back_populates="documents")
    financials: Mapped[list[IpoFinancial]] = relationship(back_populates="source_document")
    manual_extractions: Mapped[list[IpoManualExtraction]] = relationship(
        back_populates="source_document"
    )
    extraction_proposals: Mapped[list[IpoExtractionProposal]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class IpoFinancial(Base):
    """Store one issue period with flexible, secret-safe normalized metrics.

    JSON is the deliberate evolution seam until raw extraction stabilizes. An
    optional source-document foreign key becomes NULL when that metadata row is
    deleted, preserving the financial period without false dangling ownership.
    """

    __tablename__ = "ipo_financials"
    __table_args__ = (
        UniqueConstraint(
            "issue_id", "period_end", "period_type", name="uq_ipo_financials_issue_period"
        ),
        CheckConstraint(
            "period_type IN ('annual', 'quarterly')", name="ck_ipo_financials_period_type"
        ),
        CheckConstraint(
            "source_confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_financials_source_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_end: Mapped[dt.date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_document_id: Mapped[int | None] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
    )

    issue: Mapped[IpoIssue] = relationship(back_populates="financials")
    source_document: Mapped[IpoDocument | None] = relationship(back_populates="financials")


class IpoManualExtraction(Base):
    """Store one immutable, administrator-entered prospectus revision.

    Beginner note:
    This header stores facts that occur once per IPO rather than once per fiscal
    year. The selected document's URL and hashes are copied into the revision so
    provenance survives a later metadata-row deletion. Child period and peer
    rows are deleted only when their owning issue/revision is deliberately
    removed; no update API exists for corrections.
    """

    __tablename__ = "ipo_manual_extractions"
    # Every rule the Python domain enforces is mirrored here as a database CHECK, so
    # the evidence stays trustworthy even if a future non-UI caller ever inserts rows
    # directly. Keep each SQL string byte-identical to migration 20260701ipo004 or the
    # ORM/Alembic parity test (test_scan_storage_migrations.py) will fail.
    __table_args__ = (
        # The three unit columns are closed vocabularies. We store the *reported*
        # scale (crore, lakh, ...) rather than pre-multiplying to rupees so the row
        # stays faithful to the prospectus; conversion happens later in the domain
        # record, never in the database.
        CheckConstraint(
            "financial_amount_unit IN ('inr', 'thousand_inr', 'lakh_inr', "
            "'million_inr', 'crore_inr')",
            name="ck_ipo_manual_extractions_financial_unit",
        ),
        CheckConstraint(
            "issue_amount_unit IN ('inr', 'thousand_inr', 'lakh_inr', "
            "'million_inr', 'crore_inr')",
            name="ck_ipo_manual_extractions_issue_unit",
        ),
        CheckConstraint(
            "equity_share_unit IN ('shares', 'thousand_shares', 'lakh_shares', "
            "'million_shares', 'crore_shares')",
            name="ck_ipo_manual_extractions_share_unit",
        ),
        # The copied content digest must be a 64-char lowercase hex string. SQLite has
        # no regex, so the nested replace() calls strip every hex digit (0-9, a-f) and
        # assert the remainder is empty -- a portable "is this pure hex?" test that
        # runs identically on SQLite and PostgreSQL. Byte-identical to the IPO-003
        # document check so both stay under one reviewed pattern.
        CheckConstraint(
            "length(source_content_sha256) = 64 AND "
            "source_content_sha256 = lower(source_content_sha256) AND "
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "source_content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
            "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
            "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''",
            name="ck_ipo_manual_extractions_content_hash",
        ),
        # The SEBI filing fingerprint is optional (a manually attached document may
        # lack one), but when present it is the same 64-char width as everywhere else.
        CheckConstraint(
            "source_record_hash IS NULL OR length(source_record_hash) = 64",
            name="ck_ipo_manual_extractions_record_hash",
        ),
        # Fields that cannot be negative under this contract. Net worth, EBITDA, cash
        # flow, EPS, and NAV are deliberately *absent* here because a genuine loss or
        # negative book value is truthful evidence we must be able to record.
        CheckConstraint(
            "total_debt >= 0 AND cash >= 0 AND equity_shares > 0 AND "
            "fresh_issue_amount >= 0 AND ofs_amount >= 0",
            name="ck_ipo_manual_extractions_nonnegative",
        ),
        # Promoter holdings are percentages, so both must fall within 0..100.
        CheckConstraint(
            "promoter_holding_pre_issue >= 0 AND promoter_holding_pre_issue <= 100 AND "
            "promoter_holding_post_issue >= 0 AND promoter_holding_post_issue <= 100",
            name="ck_ipo_manual_extractions_promoter_range",
        ),
        # Every value carries a prospectus page citation, and a page number is always
        # positive. Enforcing it per column makes page-level provenance non-optional
        # at the database, not just in the form.
        CheckConstraint(
            "net_worth_page > 0 AND total_debt_page > 0 AND cash_page > 0 AND "
            "cash_flow_from_operations_page > 0 AND equity_shares_page > 0 AND "
            "eps_page > 0 AND nav_book_value_page > 0 AND objects_of_issue_page > 0 AND "
            "fresh_issue_amount_page > 0 AND ofs_amount_page > 0 AND "
            "promoter_holding_pre_issue_page > 0 AND promoter_holding_post_issue_page > 0",
            name="ck_ipo_manual_extractions_pages",
        ),
        # IPO-005 columns are nullable only for legacy IPO-004 revisions. The
        # all-null/all-present check prevents a direct SQL caller from attaching a
        # value without its page or creating a half-complete valuation snapshot.
        CheckConstraint(
            "(total_assets IS NULL AND total_assets_page IS NULL AND "
            "current_liabilities IS NULL AND current_liabilities_page IS NULL AND "
            "post_issue_equity_shares IS NULL AND post_issue_equity_shares_page IS NULL) OR "
            "(total_assets IS NOT NULL AND total_assets_page IS NOT NULL AND "
            "current_liabilities IS NOT NULL AND current_liabilities_page IS NOT NULL AND "
            "post_issue_equity_shares IS NOT NULL AND post_issue_equity_shares_page IS NOT NULL AND "
            "total_assets >= 0 AND current_liabilities >= 0 AND "
            "post_issue_equity_shares > 0 AND total_assets_page > 0 AND "
            "current_liabilities_page > 0 AND post_issue_equity_shares_page > 0)",
            name="ck_ipo_manual_extractions_ratio_inputs",
        ),
        # "Latest revision for an issue" is the hot read path (form prefill + the
        # scoring-data bridge). This composite index serves the issue_id filter plus
        # the submitted_at DESC, id DESC ordering without a separate sort step.
        Index(
            "ix_ipo_manual_extractions_issue_submitted",
            "issue_id",
            "submitted_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    # The owning IPO. ON DELETE CASCADE means removing an issue also removes its
    # manual revisions -- they have no meaning without their parent company.
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False
    )
    # The cached DRHP/RHP these values were transcribed from. ON DELETE SET NULL lets
    # an operator prune the document-metadata row later without erasing the revision;
    # the URL and hashes copied just below preserve provenance after the FK is NULLed.
    source_document_id: Mapped[int | None] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Frozen source snapshot: the document URL, the optional SEBI filing fingerprint,
    # and the exact PDF content digest are copied in at submission time so a revision
    # stays self-describing regardless of what later happens to the document row.
    source_document_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # The scales the values were reported in (validated by the CHECK vocabularies
    # above); the frozen domain record uses these to convert to canonical INR/shares.
    financial_amount_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    issue_amount_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    equity_share_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    # Money and share counts use Numeric(24, 4): exact base-10 arithmetic (never
    # float, so rupees never drift) with room for large crore-scale figures and four
    # decimal places. Each value is paired with a *_page column citing the prospectus.
    net_worth: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    net_worth_page: Mapped[int] = mapped_column(Integer, nullable=False)
    total_debt: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    total_debt_page: Mapped[int] = mapped_column(Integer, nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    cash_page: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_flow_from_operations: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    cash_flow_from_operations_page: Mapped[int] = mapped_column(Integer, nullable=False)
    equity_shares: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    equity_shares_page: Mapped[int] = mapped_column(Integer, nullable=False)
    eps: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    eps_page: Mapped[int] = mapped_column(Integer, nullable=False)
    nav_book_value: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    nav_book_value_page: Mapped[int] = mapped_column(Integer, nullable=False)
    objects_of_issue: Mapped[str] = mapped_column(Text, nullable=False)
    objects_of_issue_page: Mapped[int] = mapped_column(Integer, nullable=False)
    fresh_issue_amount: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    fresh_issue_amount_page: Mapped[int] = mapped_column(Integer, nullable=False)
    ofs_amount: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    ofs_amount_page: Mapped[int] = mapped_column(Integer, nullable=False)
    # Promoter holdings are percentages, so Numeric(7, 4) (max 999.9999) is ample and
    # far tighter than the crore-scale money columns above.
    promoter_holding_pre_issue: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    promoter_holding_pre_issue_page: Mapped[int] = mapped_column(Integer, nullable=False)
    promoter_holding_post_issue: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    promoter_holding_post_issue_page: Mapped[int] = mapped_column(Integer, nullable=False)
    # Nullable additions preserve previously submitted IPO-004 revisions. New
    # submissions always populate the whole group through the strict domain DTO.
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    total_assets_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    current_liabilities_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    post_issue_equity_shares: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True
    )
    post_issue_equity_shares_page: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Actor and time come from the authenticated server session and the server clock,
    # never from the browser form, so a revision cannot be back-dated or attributed to
    # another administrator. submitted_at is timezone-aware and stored as UTC.
    entered_by_email: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    issue: Mapped[IpoIssue] = relationship(back_populates="manual_extractions")
    source_document: Mapped[IpoDocument | None] = relationship(
        back_populates="manual_extractions"
    )
    # The three annual periods and the peer rows are wholly owned by this revision.
    # delete-orphan + passive_deletes lets the database FK cascade remove them as one
    # unit when a revision (or its issue) is deleted, so no half-written history remains.
    periods: Mapped[list[IpoManualFinancialPeriod]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan", passive_deletes=True
    )
    peers: Mapped[list[IpoManualPeerValuation]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan", passive_deletes=True
    )


class IpoManualFinancialPeriod(Base):
    """Store one of the three annual revenue/EBITDA/PAT rows in a revision.

    Beginner note:
    A prospectus reports three fiscal years, so the header owns exactly three of
    these child rows. ``ordinal`` (1..3) records their chronological slot while
    ``period_end`` records the actual date; the unique constraints below stop a form
    bug from writing two "year 1" rows or repeating the same period twice.
    """

    __tablename__ = "ipo_manual_financial_periods"
    __table_args__ = (
        # One row per ordinal slot and one row per date within a revision: together
        # these guarantee three distinct, correctly-numbered fiscal years.
        UniqueConstraint(
            "extraction_id", "ordinal", name="uq_ipo_manual_periods_ordinal"
        ),
        UniqueConstraint(
            "extraction_id", "period_end", name="uq_ipo_manual_periods_date"
        ),
        # Belt-and-braces for the ordinal range; the domain also enforces "exactly 3".
        CheckConstraint("ordinal >= 1 AND ordinal <= 3", name="ck_ipo_manual_periods_ordinal"),
        # Revenue cannot be negative; EBITDA and PAT can (a loss-making year is real).
        CheckConstraint("revenue >= 0", name="ck_ipo_manual_periods_revenue"),
        # Each of the three values keeps its own positive prospectus page citation.
        CheckConstraint(
            "revenue_page > 0 AND ebitda_page > 0 AND pat_page > 0",
            name="ck_ipo_manual_periods_pages",
        ),
        # Historical rows have all four fields NULL; IPO-005 rows have both
        # values and both citations. Finance cost cannot be negative, while PBT
        # may be negative for a loss-making company.
        CheckConstraint(
            "(profit_before_tax IS NULL AND profit_before_tax_page IS NULL AND "
            "finance_cost IS NULL AND finance_cost_page IS NULL) OR "
            "(profit_before_tax IS NOT NULL AND profit_before_tax_page IS NOT NULL AND "
            "finance_cost IS NOT NULL AND finance_cost_page IS NOT NULL AND "
            "finance_cost >= 0 AND profit_before_tax_page > 0 AND finance_cost_page > 0)",
            name="ck_ipo_manual_periods_ratio_inputs",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    # ON DELETE CASCADE: a period only exists as part of its parent revision.
    extraction_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_manual_extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Same Numeric(24, 4) money type and page-citation pairing as the header columns.
    revenue: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    revenue_page: Mapped[int] = mapped_column(Integer, nullable=False)
    ebitda: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    ebitda_page: Mapped[int] = mapped_column(Integer, nullable=False)
    pat: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    pat_page: Mapped[int] = mapped_column(Integer, nullable=False)
    profit_before_tax: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    profit_before_tax_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finance_cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    finance_cost_page: Mapped[int | None] = mapped_column(Integer, nullable=True)

    extraction: Mapped[IpoManualExtraction] = relationship(back_populates="periods")


class IpoManualPeerValuation(Base):
    """Store one peer row and its allowlisted flexible valuation metrics.

    Beginner note:
    A prospectus peer table lists other listed companies and a handful of ratios.
    The number of ratios varies, so they live in ``metrics_json`` rather than fixed
    columns -- but only the allowlisted keys (EPS, P/E, NAV, RoNW, EV/EBITDA,
    Price/Sales) are accepted, and each value is stored as an exact decimal string.
    ``company_key`` is a normalized form of the display name used purely to reject
    accidental duplicates like "Example Ltd" vs "example-limited".
    """

    __tablename__ = "ipo_manual_peer_valuations"
    __table_args__ = (
        # De-duplicate peers within a revision by their normalized key, not their raw
        # display name, so two spellings of the same company cannot both be stored.
        UniqueConstraint(
            "extraction_id", "company_key", name="uq_ipo_manual_peers_company"
        ),
        CheckConstraint("source_page > 0", name="ck_ipo_manual_peers_page"),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    # ON DELETE CASCADE: a peer row only exists as part of its parent revision.
    extraction_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_manual_extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_page: Mapped[int] = mapped_column(Integer, nullable=False)
    # Allowlisted metric -> exact decimal string map (see the domain's IpoPeerMetric).
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    extraction: Mapped[IpoManualExtraction] = relationship(back_populates="peers")


class IpoSubscription(Base):
    """Capture demand multiples at one UTC instant without overwriting history."""

    __tablename__ = "ipo_subscriptions"
    __table_args__ = (
        UniqueConstraint("issue_id", "captured_at", name="uq_ipo_subscriptions_issue_capture"),
        CheckConstraint(
            "(qib_multiple IS NULL OR qib_multiple >= 0) AND "
            "(nii_multiple IS NULL OR nii_multiple >= 0) AND "
            "(retail_multiple IS NULL OR retail_multiple >= 0) AND "
            "(total_multiple IS NULL OR total_multiple >= 0)",
            name="ck_ipo_subscriptions_nonnegative",
        ),
        CheckConstraint(
            "source_confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_subscriptions_source_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    qib_multiple: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    nii_multiple: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    retail_multiple: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_multiple: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )

    issue: Mapped[IpoIssue] = relationship(back_populates="subscriptions")


class IpoScore(Base):
    """Persist one immutable seven-factor calculation and its audit breakdown.

    Nullable factor columns preserve missing evidence, while JSON contributions,
    reasons, and missing labels reproduce the exact deterministic score receipt.
    Corrections append a new row instead of editing this one.
    """

    __tablename__ = "ipo_scores"
    __table_args__ = (
        *(
            CheckConstraint(
                f"{name} IS NULL OR ({name} >= 0 AND {name} <= 100)",
                name=f"ck_ipo_scores_{name}_range",
            )
            for name in (
                "business_quality",
                "financial_growth",
                "return_ratios",
                "valuation",
                "qib_subscription",
                "promoter_quality",
                "gmp_sentiment",
            )
        ),
        CheckConstraint(
            "total_score >= 0 AND total_score <= 100", name="ck_ipo_scores_total_range"
        ),
        CheckConstraint(
            "inputs_fingerprint IS NULL OR length(inputs_fingerprint) = 64",
            name="ck_ipo_scores_inputs_fingerprint_length",
        ),
        Index("ix_ipo_scores_issue_scored_at", "issue_id", "scored_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    business_quality: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    financial_growth: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    return_ratios: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    valuation: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    qib_subscription: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    promoter_quality: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    gmp_sentiment: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    total_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    contributions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    missing_data_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # IPO-006: SHA-256 over exactly the evidence the scoring service consumed
    # (extraction revision, price band, subscription snapshot, enrichment ids,
    # model versions). A matching fingerprint on the latest evaluation lets the
    # screener job skip an identical re-score, which is what makes re-running
    # ``run_ipo_screener`` idempotent. Legacy ipo-001-v1 rows keep NULL.
    inputs_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scored_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )

    issue: Mapped[IpoIssue] = relationship(back_populates="scores")
    recommendation: Mapped[IpoRecommendation | None] = relationship(
        back_populates="score", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class IpoRecommendation(Base):
    """Persist the fail-closed verdict paired one-to-one with a score receipt.

    The unique score foreign key prevents conflicting recommendations for the
    same calculation; deleting that score cascades to this dependent half of the
    evaluation pair.
    """

    __tablename__ = "ipo_recommendations"
    __table_args__ = (
        CheckConstraint(
            "recommendation IN ('Recommended', 'Not Recommended')",
            name="ck_ipo_recommendations_binary",
        ),
        CheckConstraint(
            "recommendation_type IN ('Apply confidently and consider holding if allotted', "
            "'Apply primarily for listing gains', 'Skip', 'Insufficient verified data')",
            name="ck_ipo_recommendations_type",
        ),
        CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_recommendations_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    score_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_scores.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    recommendation: Mapped[str] = mapped_column(String(32), nullable=False)
    recommendation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    missing_data_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_documents_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # IPO-006: the full seven-flag caution report as [{name, status, evidence}]
    # dicts, in the fixed catalog order. The list is complete on every new row
    # (including never-triggered and not-evaluable flags) so a reader can audit
    # what was checked, not only what fired. Legacy ipo-001-v1 rows keep the
    # server-default empty list because flags did not exist when they were scored.
    caution_flags_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )

    score: Mapped[IpoScore] = relationship(back_populates="recommendation")


class IpoExtractionProposal(Base):
    """Hold one AI-proposed prospectus extraction awaiting human review (IPO-010).

    The payload mirrors the manual-extraction submission shape — every value
    paired with a prospectus page citation — but it is only a *proposal*.
    Scoring never reads this table: an administrator must approve the proposal,
    which replays the exact manual-extraction validation path and records the
    resulting immutable revision in ``manual_extraction_id``.

    Beginner note: the review-metadata CHECK encodes the fail-closed lifecycle
    directly in the database. A pending row cannot carry reviewer fields, and a
    reviewed row must say who reviewed it and when, so no code path can quietly
    mark AI output as trusted without leaving an attributable audit trail.
    """

    __tablename__ = "ipo_extraction_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_ipo_extraction_proposals_status",
        ),
        CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_extraction_proposals_confidence",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by_email IS NULL AND reviewed_at IS NULL "
            "AND review_note IS NULL AND manual_extraction_id IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewed_by_email IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_ipo_extraction_proposals_review_metadata",
        ),
        CheckConstraint(
            "status != 'approved' OR manual_extraction_id IS NOT NULL",
            name="ck_ipo_extraction_proposals_approval_link",
        ),
        CheckConstraint(
            "page_count > 0", name="ck_ipo_extraction_proposals_page_count"
        ),
        # Same hex-digest validation pattern as the IPO-003/IPO-004 hash columns:
        # SQLite has no regex, so nested replace() strips every hex digit and the
        # remainder must be empty. Keep this SQL byte-identical to migration
        # 20260713ipo006 so the ORM/Alembic parity test passes.
        CheckConstraint(
            "length(source_content_sha256) = 64 AND "
            "source_content_sha256 = lower(source_content_sha256) AND "
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "source_content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
            "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
            "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''",
            name="ck_ipo_extraction_proposals_content_hash",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    # The IpoManualExtractionData-shaped dict the agent proposed. Approval
    # re-runs the strict domain validation on this payload, so a corrupted or
    # tampered proposal can never become an immutable revision.
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    # Reviewer-facing notes from the deterministic verifier: which cited values
    # could not be string-matched on their cited pages, and why confidence was
    # lowered. Empty list means every value was independently verified.
    needs_review_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_model: Mapped[str] = mapped_column(String(64), nullable=False)
    # Copied from the verified content-addressed cache entry the agent read, so
    # the proposal stays traceable to exact PDF bytes even if the document row
    # is later refreshed.
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )
    reviewed_by_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_extraction_id: Mapped[int | None] = mapped_column(
        BigIntPrimaryKey,
        ForeignKey("ipo_manual_extractions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    issue: Mapped[IpoIssue] = relationship(back_populates="extraction_proposals")
    document: Mapped[IpoDocument] = relationship(back_populates="extraction_proposals")


class IpoEnrichmentSignal(Base):
    """Persist one low-confidence web enrichment observation (IPO-009).

    Rows come from SerpAPI discovery queries (GMP, news, promoter reputation,
    litigation red flags, and similar sentiment-only topics). They are stored
    with their query, capture instant, and a stamped ``source_policy`` so every
    consumer can see this is web-sourced, low-confidence evidence.

    Beginner note: this table deliberately has no path into financial
    statements. Signals may only feed the optional GMP/sentiment factor and the
    litigation caution flag; official document evidence always wins. A snippet
    that tripped the prompt-injection scanner is stored with ``quarantined``
    true and its text replaced by the blocked-evidence marker, never verbatim.
    """

    __tablename__ = "ipo_enrichment_signals"
    __table_args__ = (
        UniqueConstraint(
            "issue_id",
            "signal_type",
            "captured_at",
            name="uq_ipo_enrichment_signals_issue_type_capture",
        ),
        CheckConstraint(
            "signal_type IN ('gmp', 'news', 'promoter_reputation', 'litigation_red_flag', "
            "'anchor_commentary', 'brokerage_review', 'peer_discovery')",
            name="ck_ipo_enrichment_signals_signal_type",
        ),
        CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_enrichment_signals_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        BigIntPrimaryKey, ForeignKey("ipo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    query_text: Mapped[str] = mapped_column(String(255), nullable=False)
    # Normalized search results (title/link/source/snippet/matched keywords).
    # Links are provenance data only — nothing in the app ever fetches them.
    payload_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # Conservatively parsed numeric value when the signal type defines one
    # (GMP as a percent of the issue price). NULL means "not parseable", which
    # downstream factor derivation treats as missing rather than guessing.
    parsed_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    source_policy: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.UTC)
    )

    issue: Mapped[IpoIssue] = relationship(back_populates="enrichment_signals")


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
