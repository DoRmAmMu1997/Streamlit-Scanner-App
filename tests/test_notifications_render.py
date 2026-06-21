"""ALERT-001 message rendering (pure, secret-safe)."""

from __future__ import annotations

from backend.notifications.render import render_email, render_telegram
from backend.notifications.report import DailyScanReport, RankedRow, ScreenerLine


def _report(*, ok: bool = True, message: str = "") -> DailyScanReport:
    return DailyScanReport(
        ok=ok,
        screeners=(
            ScreenerLine(
                screener_key="bollinger_band_reversal",
                universe_key="fno",
                status="success",
                shortlisted=2,
                message=message,
            ),
        ),
        total_symbols_scanned=120,
        total_shortlisted=3,
        failed_count=0 if ok else 1,
        failed_symbols_or_findings=4,
        top_results=(
            RankedRow("RELIANCE", "BUY", 87.5, "bollinger_band_reversal", "final_score"),
            RankedRow("TCS", "BUY", 73.25, "bollinger_band_reversal", "confidence"),
            RankedRow("WIPRO", "BUY", None, "bollinger_band_reversal", "unscored"),
        ),
        app_url="https://scanner.example.com",
    )


def test_telegram_includes_all_summary_fields() -> None:
    text = render_telegram(_report())
    assert "Daily scan complete" in text
    assert "Symbols scanned: 120" in text
    assert "Shortlisted: 3" in text
    assert "Failed screeners: 0" in text
    assert "Failed symbols/findings: 4" in text
    assert "1. RELIANCE BUY - score 87.50" in text  # scored row, 2dp
    assert "2. TCS BUY - confidence 73.25" in text
    assert "3. WIPRO BUY - unscored" in text
    assert "Open the app: https://scanner.example.com" in text


def test_failed_report_reads_as_failure() -> None:
    text = render_telegram(_report(ok=False))
    assert "Daily scan FAILED" in text
    subject, _ = render_email(_report(ok=False))
    assert "FAILED" in subject


def test_email_subject_and_body() -> None:
    subject, body = render_email(_report())
    assert subject == "[Scanner] Daily scan OK - 3 shortlisted"
    assert "Top results:" in body
    assert "RELIANCE" in body


def test_rendered_body_is_redacted() -> None:
    # A secret-shaped value sneaking into a screener message must be masked.
    text = render_telegram(_report(message="failed: token=SUPERSECRETVALUE123"))
    assert "SUPERSECRETVALUE123" not in text
    assert "***REDACTED***" in text
