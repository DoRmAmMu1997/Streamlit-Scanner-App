"""Outbound notifications for the daily scan summary (ALERT-001).

After the headless daily scan completes, ``notify_daily_scan`` sends an opt-in
summary (scan status, symbols scanned, shortlisted count, top-10 ranked results,
failed-symbol summary, and an app link) to Telegram and/or email. Channels are
opt-in (each fires only when its credentials are configured) and best-effort (a
send failure is logged, never raised), so notifications cannot affect the job.

Public surface:
- `notify_daily_scan(summary)` — the entry point the daily job calls.
- `NotificationResult` / `ChannelResult` — per-call/per-channel outcomes.
- `NotificationSettings` / `load_notification_settings` — opt-in config.
- `DailyScanReport` / `build_daily_scan_report` — the structured summary.
"""

from backend.notifications.config import (
    NotificationSettings,
    load_notification_settings,
)
from backend.notifications.report import DailyScanReport, build_daily_scan_report
from backend.notifications.service import (
    ChannelResult,
    NotificationResult,
    notify_daily_scan,
)

__all__ = [
    "ChannelResult",
    "DailyScanReport",
    "NotificationResult",
    "NotificationSettings",
    "build_daily_scan_report",
    "load_notification_settings",
    "notify_daily_scan",
]
