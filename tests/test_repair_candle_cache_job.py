"""Tests for the headless candle cache-repair job (DATA-002)."""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import pytest

from backend.data_quality.cache_repair import CacheRepairSummary, SymbolRepairOutcome
from backend.jobs import repair_candle_cache as job


@pytest.fixture(autouse=True)
def _quiet_job(monkeypatch):
    """Keep the job offline: no dirs, no DB, no Dhan, no logging setup."""
    monkeypatch.setattr(job, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(job, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(job, "install_secret_redaction_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(job, "persist_repair_summary", lambda *_a, **_k: None)


def _universe(*symbols: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": symbol, "security_id": str(index), "mapping_status": "mapped"}
            for index, symbol in enumerate(symbols)
        ]
    )


def _summary(**overrides) -> CacheRepairSummary:
    defaults = {"symbols_checked": 1, "symbols_clean": 1}
    return CacheRepairSummary(**{**defaults, **overrides})


def test_job_reports_nothing_to_do_on_an_empty_universe(monkeypatch):
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: pd.DataFrame())
    out = io.StringIO()

    outcome = job.run_repair_candle_cache(out=out)

    assert outcome.exit_code == 0
    assert outcome.summary is None
    assert "nothing to repair" in out.getvalue()


def test_job_filters_to_the_requested_symbols(monkeypatch):
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A", "B", "C"))
    monkeypatch.setattr(job, "_build_loader", lambda _stream: object())
    seen: dict[str, object] = {}

    def fake_repair(_loader, rows, **_kwargs):
        seen["symbols"] = [row["symbol"] for row in rows]
        return _summary()

    monkeypatch.setattr(job, "repair_universe", fake_repair)

    job.run_repair_candle_cache(symbols=["b"], out=io.StringIO())

    # Matching is case-insensitive so `--symbol b` finds B.
    assert seen["symbols"] == ["B"]


def test_job_exits_non_zero_when_no_requested_symbol_is_mapped(monkeypatch):
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A"))
    out = io.StringIO()

    outcome = job.run_repair_candle_cache(symbols=["MISSING"], out=out)

    assert outcome.exit_code == 1
    assert "are mapped" in out.getvalue()


def test_dry_run_never_persists_a_receipt(monkeypatch):
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A"))
    monkeypatch.setattr(job, "_build_loader", lambda _stream: object())
    monkeypatch.setattr(job, "repair_universe", lambda *_a, **_k: _summary())
    persisted: list[str] = []
    monkeypatch.setattr(
        job, "persist_repair_summary", lambda *_a, **_k: persisted.append("written")
    )
    out = io.StringIO()

    job.run_repair_candle_cache(dry_run=True, out=out)

    assert persisted == []
    assert "dry run" in out.getvalue()


def test_normal_run_persists_a_receipt(monkeypatch):
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A"))
    monkeypatch.setattr(job, "_build_loader", lambda _stream: object())
    monkeypatch.setattr(job, "repair_universe", lambda *_a, **_k: _summary())
    persisted: list[str] = []
    monkeypatch.setattr(
        job,
        "persist_repair_summary",
        lambda _summary, **kwargs: persisted.append(str(kwargs.get("trigger"))),
    )

    job.run_repair_candle_cache(out=io.StringIO())

    assert persisted == ["cli"]


def test_vendor_dirty_symbols_do_not_fail_the_job(monkeypatch):
    """"Still dirty" is a reported condition, not a job failure."""
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A"))
    monkeypatch.setattr(job, "_build_loader", lambda _stream: object())
    monkeypatch.setattr(
        job,
        "repair_universe",
        lambda *_a, **_k: _summary(symbols_clean=0, symbols_unrepairable=1),
    )

    outcome = job.run_repair_candle_cache(out=io.StringIO())

    assert outcome.exit_code == 0


def test_an_unreadable_file_fails_the_job(monkeypatch):
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A"))
    monkeypatch.setattr(job, "_build_loader", lambda _stream: object())
    monkeypatch.setattr(
        job,
        "repair_universe",
        lambda *_a, **_k: _summary(symbols_clean=0, symbols_failed=1),
    )

    outcome = job.run_repair_candle_cache(out=io.StringIO())

    assert outcome.exit_code == 1


def test_missing_credentials_degrade_to_offline_repairs(monkeypatch):
    """No Dhan token still allows de-duplication; it must not abort the pass."""
    monkeypatch.setattr(job, "union_of_mapped_universes", lambda: _universe("A"))
    monkeypatch.setattr(
        job.DhanDataClient,
        "from_env",
        classmethod(
            lambda _cls: (_ for _ in ()).throw(
                RuntimeError("access_token=job-secret missing")
            )
        ),
    )
    monkeypatch.setattr(job, "repair_universe", lambda *_a, **_k: _summary())
    out = io.StringIO()

    outcome = job.run_repair_candle_cache(out=out)

    assert outcome.exit_code == 0
    printed = out.getvalue()
    assert "Offline repairs only" in printed
    assert "job-secret" not in printed
    assert "***REDACTED***" in printed


def test_summary_printer_names_symbols_that_are_still_dirty():
    summary = CacheRepairSummary(
        symbols_checked=2,
        symbols_repaired=1,
        symbols_unrepairable=1,
        outcomes=(
            SymbolRepairOutcome(
                symbol="POONAWALLA",
                status="repaired",
                actions=("DEDUPE_EXACT_ROWS",),
                rows_before=3_714,
                rows_after=2_476,
            ),
            SymbolRepairOutcome(
                symbol="MOTHERSON",
                status="unrepairable",
                before_codes=("DUPLICATE_DATE",),
                after_codes=("DUPLICATE_DATE",),
                message="no repair could be applied",
            ),
        ),
    )
    out = io.StringIO()

    job.print_repair_summary(summary, out=out)

    printed = out.getvalue()
    assert "POONAWALLA" in printed
    assert "-1238 row(s)" in printed
    assert "MOTHERSON" in printed
    assert "STILL DIRTY (DUPLICATE_DATE)" in printed
    assert "repaired=1" in printed


def test_dry_run_printer_uses_conditional_language():
    out = io.StringIO()

    job.print_repair_summary(
        CacheRepairSummary(
            symbols_checked=1,
            symbols_repaired=1,
            outcomes=(
                SymbolRepairOutcome(
                    symbol="ABREL", status="repaired", actions=("DEDUPE_EXACT_ROWS",)
                ),
            ),
        ),
        out=out,
        dry_run=True,
    )

    assert "would repair" in out.getvalue()


def test_main_wires_cli_flags_through(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return job.RepairJobOutcome(summary=None, exit_code=0)

    monkeypatch.setattr(job, "run_repair_candle_cache", fake_run)

    exit_code = job.main(
        ["--dry-run", "--force", "--symbol", "ABREL", "--symbol", "LTF", "--as-of", "2026-08-17"]
    )

    assert exit_code == 0
    assert captured["dry_run"] is True
    assert captured["force"] is True
    assert captured["symbols"] == ["ABREL", "LTF"]
    assert captured["today"] == dt.date(2026, 8, 17)


def test_main_rejects_a_malformed_as_of_date(monkeypatch):
    monkeypatch.setattr(
        job, "run_repair_candle_cache", lambda **_k: job.RepairJobOutcome(None, 0)
    )

    with pytest.raises(SystemExit):
        job.main(["--as-of", "17-08-2026"])
