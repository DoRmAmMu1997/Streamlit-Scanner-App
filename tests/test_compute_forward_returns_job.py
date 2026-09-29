"""VALID-004 headless forward-return compute job tests."""

from __future__ import annotations

import datetime as dt
import importlib
import io
from contextlib import contextmanager

import pytest

from backend.validation import ForwardReturnRunSummary


@contextmanager
def _fake_session_scope():
    yield "session"


def test_run_compute_forward_returns_bootstraps_schema_then_calls_service(monkeypatch):
    job = importlib.import_module("backend.jobs.compute_forward_returns")
    output = io.StringIO()
    calls: list[object] = []
    fake_client = object()

    def ensure_schema() -> None:
        calls.append("schema")

    def data_client_factory():
        calls.append("client")
        return fake_client

    def data_loader_factory(client):
        calls.append(("loader", client))
        return "loader"

    def compute_service(session, loader, **kwargs):
        calls.append(("compute", session, loader, kwargs))
        return ForwardReturnRunSummary(
            total_signals=3,
            computed=1,
            pending=1,
            insufficient=1,
            benchmark_computed=0,
            benchmark_missing=1,
        )

    outcome = job.run_compute_forward_returns(
        limit=25,
        as_of=dt.date(2026, 1, 31),
        horizons=(20, 60),
        ensure_schema=ensure_schema,
        session_factory=_fake_session_scope,
        data_client_factory=data_client_factory,
        data_loader_factory=data_loader_factory,
        compute_service=compute_service,
        output=output,
    )

    assert outcome.exit_code == 0
    assert calls == [
        "schema",
        "client",
        ("loader", fake_client),
        (
            "compute",
            _fake_session_scope,
            "loader",
            {
                "as_of": dt.date(2026, 1, 31),
                "horizons": (20, 60),
                "limit": 25,
            },
        ),
    ]
    assert "computed=1" in output.getvalue()
    assert "pending=1" in output.getvalue()
    assert "insufficient=1" in output.getvalue()


def test_main_parses_limit_as_of_and_repeatable_horizons(monkeypatch):
    job = importlib.import_module("backend.jobs.compute_forward_returns")
    captured: dict[str, object] = {}

    def job_runner(**kwargs):
        captured.update(kwargs)
        return job.ForwardReturnJobOutcome(
            summary=ForwardReturnRunSummary(total_signals=0),
            fatal=False,
            message="ok",
        )

    monkeypatch.setattr(job, "configure_logging", lambda: None)

    exit_code = job.main(
        [
            "--limit",
            "7",
            "--as-of",
            "2026-01-31",
            "--horizon",
            "20",
            "--horizon",
            "120",
        ],
        job_runner=job_runner,
    )

    assert exit_code == 0
    assert captured["limit"] == 7
    assert captured["as_of"] == dt.date(2026, 1, 31)
    assert captured["horizons"] == (20, 120)


def test_run_compute_forward_returns_reports_redacted_fatal_setup_errors(monkeypatch):
    job = importlib.import_module("backend.jobs.compute_forward_returns")
    output = io.StringIO()

    monkeypatch.setattr(
        job,
        "redact_exception",
        lambda exc: "RuntimeError: [REDACTED]",
    )

    def data_client_factory():
        raise RuntimeError("broker_token=super-secret")

    outcome = job.run_compute_forward_returns(
        ensure_schema=lambda: None,
        session_factory=_fake_session_scope,
        data_client_factory=data_client_factory,
        data_loader_factory=lambda client: client,
        compute_service=lambda *_args, **_kwargs: ForwardReturnRunSummary(),
        output=output,
    )

    assert outcome.exit_code == 1
    assert outcome.fatal is True
    assert "[REDACTED]" in output.getvalue()
    assert "super-secret" not in output.getvalue()


def test_job_preserves_committed_summary_after_later_failure():
    """A later rolled-back signal must not hide earlier committed progress."""
    from backend.validation.service import ForwardReturnBatchError

    job = importlib.import_module("backend.jobs.compute_forward_returns")
    progress = ForwardReturnRunSummary(total_signals=1, computed=2)

    def broken(*_args, **_kwargs):
        raise ForwardReturnBatchError(progress)

    outcome = job.run_compute_forward_returns(
        ensure_schema=lambda: None, data_client_factory=object,
        data_loader_factory=lambda client: client, compute_service=broken,
        output=io.StringIO(),
    )
    assert outcome.fatal
    assert outcome.summary == progress


@pytest.mark.parametrize("horizons,limit", [((True,), 500), ((1.2,), 500), ((0,), 500),
                                           ((-1,), 500), ((20,), True), ((20,), 0),
                                           ((20,), -1), ((20,), 1.5)])
def test_job_rejects_invalid_arguments_before_bootstrap(horizons, limit):
    """Coercion must not silently schedule a different horizon or open providers."""
    job = importlib.import_module("backend.jobs.compute_forward_returns")
    with pytest.raises(ValueError, match="positive integer"):
        job.run_compute_forward_returns(horizons=horizons, limit=limit,
            ensure_schema=lambda: pytest.fail("invalid argument initialized schema"))


def test_job_empty_horizons_does_not_initialize_schema_or_provider():
    """Empty requested work is a successful no-op, even without credentials."""
    job = importlib.import_module("backend.jobs.compute_forward_returns")
    result = job.run_compute_forward_returns(horizons=(),
        ensure_schema=lambda: pytest.fail("empty work initialized schema"), output=io.StringIO())
    assert not result.fatal
    assert result.summary.total_signals == 0
