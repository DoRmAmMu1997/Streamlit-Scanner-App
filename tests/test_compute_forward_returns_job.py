"""VALID-004 headless forward-return compute job tests."""

from __future__ import annotations

import datetime as dt
import importlib
import io
from contextlib import contextmanager

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
            "session",
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
