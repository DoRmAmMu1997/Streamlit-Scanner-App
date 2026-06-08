"""Tests for JOB-002's daily scan schedule config loader.

These are pure, offline unit tests for ``backend.jobs.daily_scan_config``. The
loader only validates the *shape* of a YAML schedule (required fields, types), so
none of these tests need the screener registry, a universe CSV, Dhan, or a
database. They write a tiny YAML file to ``tmp_path`` and assert on the parsed
:class:`DailyScanEntry` list (or the clear :class:`DailyScanConfigError`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.jobs.daily_scan_config import (
    DailyScanConfigError,
    DailyScanEntry,
    load_daily_scan_config,
)


def _write(tmp_path: Path, text: str) -> Path:
    """Write YAML text to a temp file and return its path."""
    path = tmp_path / "daily_scans.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_config_parses_enabled_and_disabled_entries(tmp_path):
    """A well-formed file returns every entry, including disabled ones."""
    path = _write(
        tmp_path,
        """
        daily_scans:
          - name: Bollinger daily
            screener_key: bollinger_band_reversal
            enabled: true
            description: Mean reversion gate.
          - name: Knoxville override
            screener_key: envelope_knoxville_buy
            enabled: true
            universe_key: hemant_super_45
            params:
              percent: 14.0
          - name: 67 Ka Funda (AI)
            screener_key: sixty_seven_ka_funda
            enabled: false
        """,
    )

    entries = load_daily_scan_config(path)

    assert [entry.screener_key for entry in entries] == [
        "bollinger_band_reversal",
        "envelope_knoxville_buy",
        "sixty_seven_ka_funda",
    ]
    assert [entry.enabled for entry in entries] == [True, True, False]

    override = entries[1]
    assert override.universe_key == "hemant_super_45"
    assert override.params == {"percent": 14.0}
    assert isinstance(override, DailyScanEntry)

    # The disabled AI entry is still returned so the runner can log it as skipped.
    assert entries[2].enabled is False


def test_optional_fields_default_when_omitted(tmp_path):
    """Only name + screener_key are required; the rest take sensible defaults."""
    path = _write(
        tmp_path,
        """
        daily_scans:
          - name: Minimal
            screener_key: heikin_ashi_supertrend
        """,
    )

    (entry,) = load_daily_scan_config(path)

    assert entry.enabled is True  # omitted enabled is treated as enabled
    assert entry.universe_key is None  # fall back to the screener's registry universe
    assert entry.params == {}
    assert entry.description == ""


def test_missing_screener_key_is_a_clear_error(tmp_path):
    """A required field that is missing fails clearly and points at the entry."""
    path = _write(
        tmp_path,
        """
        daily_scans:
          - name: No key here
            enabled: true
        """,
    )

    with pytest.raises(DailyScanConfigError, match=r"daily_scans\[0\]\.screener_key"):
        load_daily_scan_config(path)


def test_missing_name_is_a_clear_error(tmp_path):
    """``name`` is required too."""
    path = _write(
        tmp_path,
        """
        daily_scans:
          - screener_key: bollinger_band_reversal
        """,
    )

    with pytest.raises(DailyScanConfigError, match=r"daily_scans\[0\]\.name"):
        load_daily_scan_config(path)


def test_bad_yaml_is_a_clear_error(tmp_path):
    """Malformed YAML becomes a config error, not a raw YAML traceback."""
    path = _write(tmp_path, "daily_scans: [unclosed\n")

    with pytest.raises(DailyScanConfigError, match="not valid YAML"):
        load_daily_scan_config(path)


def test_daily_scans_must_be_a_list(tmp_path):
    """A scalar where a list is expected fails clearly."""
    path = _write(tmp_path, "daily_scans: nope\n")

    with pytest.raises(DailyScanConfigError, match="must be a list"):
        load_daily_scan_config(path)


def test_params_must_be_a_mapping(tmp_path):
    """``params`` must be a mapping, not a list."""
    path = _write(
        tmp_path,
        """
        daily_scans:
          - name: Bad params
            screener_key: bollinger_band_reversal
            params:
              - 1
              - 2
        """,
    )

    with pytest.raises(DailyScanConfigError, match=r"params must be a mapping"):
        load_daily_scan_config(path)


def test_enabled_must_be_boolean(tmp_path):
    """A non-boolean ``enabled`` is rejected (YAML bool words still work)."""
    path = _write(
        tmp_path,
        """
        daily_scans:
          - name: Bad flag
            screener_key: bollinger_band_reversal
            enabled: maybe
        """,
    )

    with pytest.raises(DailyScanConfigError, match=r"enabled must be true or false"):
        load_daily_scan_config(path)


def test_missing_file_is_a_clear_error(tmp_path):
    """Pointing --config at a nonexistent path fails clearly, not with OSError."""
    missing = tmp_path / "does_not_exist.yaml"

    with pytest.raises(DailyScanConfigError, match="Could not read"):
        load_daily_scan_config(missing)


def test_example_config_file_is_valid_and_ships_ai_disabled():
    """The committed example must parse and keep AI-heavy screeners opt-in."""
    repo_root = Path(__file__).resolve().parents[1]
    entries = load_daily_scan_config(repo_root / "config" / "daily_scans.example.yaml")

    by_key = {entry.screener_key: entry for entry in entries}
    # AI-heavy screeners must ship disabled in the example.
    assert by_key["sixty_seven_ka_funda"].enabled is False
    assert by_key["technical_analysis"].enabled is False
    # At least one deterministic screener is enabled so the example is runnable.
    assert any(
        entry.enabled and entry.screener_key not in {"sixty_seven_ka_funda", "technical_analysis"}
        for entry in entries
    )
