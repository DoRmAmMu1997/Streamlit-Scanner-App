"""Render a ``DailyScanReport`` into plain-text Telegram and email messages.

Pure functions, ASCII output. The body is composed only from non-secret summary
fields, and each returned string is still passed through ``redact_text`` as a
defense-in-depth net so a secret can never reach a notification (ALERT-001
acceptance: "secrets are not leaked in the message").
"""

from __future__ import annotations

from backend.notifications.report import DailyScanReport, RankedRow
from backend.security import redact_text


def _score(value: float | None) -> str:
    """Format a score to 2dp, or ``n/a`` when unscored (RANK-* not yet applied)."""
    return f"{value:.2f}" if value is not None else "n/a"


def _ranked_lines(rows: tuple[RankedRow, ...]) -> list[str]:
    if not rows:
        return ["  (no shortlisted results)"]
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        rating = f" {row.rating}" if row.rating else ""
        screener = f" [{row.screener_key}]" if row.screener_key else ""
        lines.append(
            f"  {index}. {row.symbol}{rating} - score {_score(row.final_score)}{screener}"
        )
    return lines


def _body_lines(report: DailyScanReport) -> list[str]:
    header = "Daily scan complete" if report.ok else "Daily scan FAILED"
    scanned = (
        "n/a"
        if report.total_symbols_scanned is None
        else str(report.total_symbols_scanned)
    )
    lines = [
        header,
        "",
        f"Symbols scanned: {scanned}",
        f"Shortlisted: {report.total_shortlisted}",
        f"Screeners: {len(report.screeners)} ({report.failed_count} failed)",
        "",
        "Per screener:",
    ]
    for line in report.screeners:
        universe = f"/{line.universe_key}" if line.universe_key else ""
        detail = f" - {line.message}" if line.message else ""
        lines.append(
            f"  - {line.screener_key}{universe}: {line.status}, "
            f"{line.shortlisted} shortlisted{detail}"
        )
    lines += ["", "Top results:", *_ranked_lines(report.top_results)]
    if report.app_url:
        lines += ["", f"Open the app: {report.app_url}"]
    return lines


def render_telegram(report: DailyScanReport) -> str:
    """Return the Telegram message text (secret-safe)."""
    return redact_text("\n".join(_body_lines(report)))


def render_email(report: DailyScanReport) -> tuple[str, str]:
    """Return the ``(subject, body)`` for the email message (both secret-safe)."""
    status = "OK" if report.ok else "FAILED"
    subject = f"[Scanner] Daily scan {status} - {report.total_shortlisted} shortlisted"
    body = "\n".join(_body_lines(report))
    return redact_text(subject), redact_text(body)
