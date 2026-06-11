"""JOB-002 daily scan schedule config loader.

Run the daily scan command against a YAML schedule with::

    python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml

This module is intentionally small, boring, and dependency-light. It only knows
how to turn a YAML file into a list of :class:`DailyScanEntry` records. It does
*not* import the screener registry, universe loader, data loader, or database.

Why keep it registry-free:
Validating only the *shape* of the config (required fields, types) here keeps the
loader trivially unit-testable without the scanner stack, and keeps a single
source of truth for "does this screener/universe exist?" That existence question
already has a clear answer at run time:

- an unknown ``screener_key`` is reported by ``run_daily_scan`` exactly like
  JOB-001's ``--screener`` typo path ("Unknown screener key."), and
- an unknown ``universe_key`` surfaces through ``backend.universe_loader``'s
  existing ``KeyError("Unknown universe key: ...")``.

Both keep the rest of the schedule running and still make the process exit 1, so
operators see one typo without losing the valid scans around it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class DailyScanConfigError(ValueError):
    """Raised when a daily scan config file is missing, unreadable, or malformed.

    A single, specific exception type lets the CLI catch config problems and turn
    them into a clear operator message plus a non-zero exit code, instead of a
    raw traceback. This mirrors the repo's other "fail clearly" boundaries such
    as ``ScreenerRegistryError`` and ``SettingsError``.
    """


@dataclass(frozen=True)
class DailyScanEntry:
    """One named scan batch from the schedule file.

    Field guide:
    - ``name``: required human label shown in operator output.
    - ``screener_key``: required registry key (validated at run time).
    - ``enabled``: defaults to ``True``; set ``false`` to keep an entry as a
      documented example without running it.
    - ``universe_key``: optional override. ``None`` means "use the universe the
      screener declares in its registry metadata."
    - ``params``: optional per-run overrides merged over the screener defaults.
    - ``description``: optional free-text note for operators.
    """

    name: str
    screener_key: str
    enabled: bool = True
    universe_key: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""


def load_daily_scan_config(path: str | Path) -> list[DailyScanEntry]:
    """Parse a YAML schedule file into a list of :class:`DailyScanEntry`.

    Returns *all* entries, including disabled ones, so the caller can log which
    scans it skipped. Raises :class:`DailyScanConfigError` for any shape problem:
    an unreadable file, invalid YAML, a missing ``daily_scans`` list, or an entry
    that is missing a required field or has a wrong-typed field.
    """
    config_path = Path(path)

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        # Keep the path (useful for operators) but not the raw OS error text.
        raise DailyScanConfigError(
            f"Could not read daily scan config file: {config_path}"
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DailyScanConfigError(
            f"Daily scan config is not valid YAML: {config_path}"
        ) from exc

    if not isinstance(data, Mapping):
        raise DailyScanConfigError(
            "Daily scan config must be a mapping with a top-level 'daily_scans' list."
        )

    raw_entries = data.get("daily_scans")
    # Strings/bytes are technically sequences; exclude them so a stray scalar is a
    # clear error instead of iterating one character at a time.
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        raise DailyScanConfigError(
            "'daily_scans' must be a list of scan entries."
        )

    return [_parse_entry(index, raw) for index, raw in enumerate(raw_entries)]


def _parse_entry(index: int, raw: Any) -> DailyScanEntry:
    """Validate and convert one raw mapping into a :class:`DailyScanEntry`.

    ``index`` is only used to point operators at the offending entry, e.g.
    ``daily_scans[2].screener_key is required``.
    """
    where = f"daily_scans[{index}]"
    if not isinstance(raw, Mapping):
        raise DailyScanConfigError(f"{where} must be a mapping of entry fields.")

    name = _require_non_empty_str(raw, "name", where)
    screener_key = _require_non_empty_str(raw, "screener_key", where)

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise DailyScanConfigError(f"{where}.enabled must be true or false.")

    universe_key = raw.get("universe_key")
    if universe_key is not None and not (
        isinstance(universe_key, str) and universe_key.strip()
    ):
        raise DailyScanConfigError(
            f"{where}.universe_key must be a non-empty string when set."
        )

    params = raw.get("params", {})
    if not isinstance(params, Mapping):
        raise DailyScanConfigError(f"{where}.params must be a mapping of parameters.")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise DailyScanConfigError(f"{where}.description must be a string.")

    return DailyScanEntry(
        name=name,
        screener_key=screener_key,
        enabled=enabled,
        universe_key=universe_key,
        params=dict(params),
        description=description,
    )


def _require_non_empty_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    """Return a required string field or raise a clear config error."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DailyScanConfigError(
            f"{where}.{key} is required and must be a non-empty string."
        )
    return value
