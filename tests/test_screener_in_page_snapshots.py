"""Whole-payload snapshot tests for the screener.in page parser (TEST-005).

``tests/test_screener_in_client.py`` unit-tests each sub-parser with inline
HTML. What those tests cannot catch is a *silent partial regression*: the
parser is deliberately soft-failing (a missing or renamed section degrades to
an empty value instead of raising), so a refactor that accidentally breaks one
extraction branch — say a selector typo that turns ``sector`` into ``""`` —
still passes every targeted assert and simply returns less data forever.

These snapshots lock the ENTIRE ``_parse_company_page`` payload over two
checked-in fixture pages:

- ``full_page.html`` — a representative page exercising every offline branch
  (top-ratios card, all six financial tables, static peers, pros/cons,
  announcements, concalls, raw-text notes).
- ``degraded_page.html`` — everything missing except ``<h1>``; pins the
  soft-fail contract (empty tables, ``None`` ratios) so "missing section"
  can neither crash nor fabricate values.

Fixtures are synthetic but shape-faithful (IDs and span markup mirror the live
site, same as the inline samples in ``test_screener_in_client.py``). They are
parsed with ``session=None``, so no HTTP happens; the HTMX peer/median-PE
branches stay covered by the existing client tests.

Regenerate after an intentional parser change with::

    UPDATE_GOLDEN=1 python -m pytest tests/test_screener_in_page_snapshots.py

then review the JSON diff before committing (same workflow as the screener
goldens in ``tests/test_screener_golden_outputs.py``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from backend.fundamentals.screener_in_client import _parse_company_page

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "screener_in"
GOLDEN_DIR = Path(__file__).parent / "golden" / "screener_in"

_CASES = (
    ("full_page", "SNAP"),
    ("degraded_page", "BARE"),
)


def _parse_fixture(name: str, symbol: str) -> dict:
    html = (FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8")
    payload = _parse_company_page(
        html,
        symbol=symbol,
        source_url=f"https://www.screener.in/company/{symbol}/consolidated/",
        session=None,
    )
    # ``fetched_at`` is wall-clock and cannot live in a snapshot. Validate its
    # shape, then drop it so the rest of the payload compares exactly.
    fetched_at = payload.pop("fetched_at")
    datetime.fromisoformat(fetched_at)
    return payload


@pytest.mark.parametrize(("name", "symbol"), _CASES, ids=lambda value: str(value))
def test_parse_company_page_matches_snapshot(name: str, symbol: str):
    """The full parsed payload should fail tests when any branch drifts."""
    payload = _parse_fixture(name, symbol)

    golden_path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        with golden_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")
        pytest.skip(f"Rewrote snapshot for {name}; rerun without UPDATE_GOLDEN to verify.")

    if not golden_path.exists():
        pytest.fail(f"Snapshot is missing: {golden_path}")
    with golden_path.open(encoding="utf-8") as file:
        assert payload == json.load(file)
