"""Test the deterministic IPO-005 ratio engine and its diagnostic receipts.

Beginner note:
These tests build detached domain records instead of inserting database rows. That
keeps the accounting formulas independently testable: if a formula fails here, the
problem cannot be hidden by SQLAlchemy, Streamlit, or a migration.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from backend.ipo.financials.ratio_engine import (
    IpoRatioName,
    IpoRatioStatus,
    calculate_ipo_ratios,
)
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionRecord,
    IpoManualPeriodData,
    IpoShareUnit,
)
from backend.ipo.models import IpoValidationError


def _period(
    year: int,
    *,
    revenue: str,
    ebitda: str,
    pat: str,
    profit_before_tax: str,
    finance_cost: str,
) -> IpoManualPeriodData:
    """Build one fully sourced annual period in the IPO-005 shape.

    Beginner note:
        Every formula test needs the same value-and-page pairing. Keeping that
        complete shape in one helper makes a missing citation an obvious fixture
        failure instead of unrelated noise in an accounting assertion.
    """
    return IpoManualPeriodData(
        period_end=dt.date(year, 3, 31),
        revenue=Decimal(revenue),
        revenue_page=100,
        ebitda=Decimal(ebitda),
        ebitda_page=101,
        pat=Decimal(pat),
        pat_page=102,
        profit_before_tax=Decimal(profit_before_tax),
        profit_before_tax_page=103,
        finance_cost=Decimal(finance_cost),
        finance_cost_page=104,
    )


def _profile() -> IpoManualExtractionRecord:
    """Return a profitable three-year profile with hand-checkable ratios.

    Beginner note:
    Crore and lakh units deliberately exercise canonical conversion. Ratios must
    use individual INR and shares internally even though the source document used
    compact Indian reporting units.
    """
    return IpoManualExtractionRecord(
        id=7,
        issue_id=3,
        source_document_id=11,
        source_document_url="https://www.sebi.gov.in/filing.html",
        source_record_hash="a" * 64,
        source_content_sha256="b" * 64,
        financial_amount_unit=IpoAmountUnit.CRORE_INR,
        issue_amount_unit=IpoAmountUnit.CRORE_INR,
        equity_share_unit=IpoShareUnit.LAKH_SHARES,
        periods=(
            _period(
                2023,
                revenue="100",
                ebitda="20",
                pat="10",
                profit_before_tax="14",
                finance_cost="2",
            ),
            _period(
                2024,
                revenue="110",
                ebitda="24",
                pat="11",
                profit_before_tax="15",
                finance_cost="2",
            ),
            _period(
                2025,
                revenue="121",
                ebitda="30.25",
                pat="12.1",
                profit_before_tax="16",
                finance_cost="2.15",
            ),
        ),
        net_worth=Decimal("60.5"),
        net_worth_page=120,
        total_debt=Decimal("12.1"),
        total_debt_page=121,
        cash=Decimal("6.05"),
        cash_page=122,
        cash_flow_from_operations=Decimal("18.15"),
        cash_flow_from_operations_page=123,
        equity_shares=Decimal("50"),
        equity_shares_page=124,
        eps=Decimal("24.20"),
        eps_page=125,
        nav_book_value=Decimal("121.00"),
        nav_book_value_page=126,
        objects_of_issue="Fund expansion without changing historical ratios.",
        objects_of_issue_page=127,
        fresh_issue_amount=Decimal("20"),
        fresh_issue_amount_page=128,
        ofs_amount=Decimal("5"),
        ofs_amount_page=129,
        promoter_holding_pre_issue=Decimal("70"),
        promoter_holding_pre_issue_page=130,
        promoter_holding_post_issue=Decimal("60"),
        promoter_holding_post_issue_page=131,
        peers=(),
        entered_by_email="admin@example.com",
        submitted_at=dt.datetime(2026, 7, 3, tzinfo=dt.UTC),
        total_assets=Decimal("100"),
        total_assets_page=132,
        current_liabilities=Decimal("20"),
        current_liabilities_page=133,
        post_issue_equity_shares=Decimal("60"),
        post_issue_equity_shares_page=134,
    )


def _replace_latest_period(
    profile: IpoManualExtractionRecord, **changes: object
) -> IpoManualExtractionRecord:
    """Return a profile whose newest period contains selected test values.

    Beginner note:
        Ratios use the latest year for margins and returns. Replacing only that
        immutable child lets an edge-case test change one economic condition while
        preserving every other source fact from the baseline profile.
    """
    periods = (*profile.periods[:-1], replace(profile.periods[-1], **changes))
    return replace(profile, periods=periods)


def test_profitable_profile_computes_all_sixteen_ratios_exactly() -> None:
    """A complete profitable profile should return sixteen computed receipts.

    Beginner note:
        This hand-checkable golden scenario pins the whole formula catalog, public
        rounding, formula version, and source hash in one place. A missing receipt is
        therefore caught even when every remaining individual formula is correct.
    """
    analysis = calculate_ipo_ratios(
        _profile(),
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.formula_version == "ipo-ratio-v1"
    assert analysis.extraction_id == 7
    assert analysis.source_content_sha256 == "b" * 64
    assert set(analysis.ratios) == set(IpoRatioName)
    assert all(receipt.status is IpoRatioStatus.COMPUTED for receipt in analysis.ratios.values())
    assert analysis.ratios[IpoRatioName.REVENUE_CAGR].value == Decimal("10.0000")
    assert analysis.ratios[IpoRatioName.PAT_CAGR].value == Decimal("10.0000")
    assert analysis.ratios[IpoRatioName.EBITDA_MARGIN].value == Decimal("25.0000")
    assert analysis.ratios[IpoRatioName.PAT_MARGIN].value == Decimal("10.0000")
    assert analysis.ratios[IpoRatioName.ROE].value == Decimal("20.0000")
    assert analysis.ratios[IpoRatioName.ROCE].value == Decimal("22.6875")
    assert analysis.ratios[IpoRatioName.DEBT_TO_EQUITY].value == Decimal("0.2000")
    assert analysis.ratios[IpoRatioName.NET_DEBT_TO_EBITDA].value == Decimal("0.2000")
    assert analysis.ratios[IpoRatioName.INTEREST_COVERAGE].value == Decimal("8.4419")
    assert analysis.ratios[IpoRatioName.CFO_TO_PAT].value == Decimal("1.5000")
    assert analysis.ratios[IpoRatioName.EPS].value == Decimal("24.2000")
    assert analysis.ratios[IpoRatioName.BOOK_VALUE_PER_SHARE].value == Decimal("121.0000")
    assert analysis.ratios[IpoRatioName.PRICE_TO_EARNINGS].value == Decimal("10.0000")
    assert analysis.ratios[IpoRatioName.PRICE_TO_BOOK].value == Decimal("2.0000")
    assert analysis.ratios[IpoRatioName.EV_TO_EBITDA].value == Decimal("5.0000")
    assert analysis.ratios[IpoRatioName.EV_TO_SALES].value == Decimal("1.2500")
    assert analysis.eps_reconciliation.materially_different is False
    assert analysis.book_value_reconciliation.materially_different is False


def test_loss_makes_growth_and_pe_unavailable_but_keeps_signed_ratios() -> None:
    """A loss should suppress misleading metrics without hiding useful negatives.

    Beginner note:
        Negative margins and returns describe the loss and remain useful. PAT CAGR
        and P/E rely on a positive earnings base, so the correct result for those is
        an explanation—not a fabricated positive multiple or a raised exception.
    """
    profile = _replace_latest_period(
        _profile(),
        pat=Decimal("-12.1"),
        profit_before_tax=Decimal("-14.3"),
        finance_cost=Decimal("2.2"),
    )
    profile = replace(
        profile,
        periods=(replace(profile.periods[0], pat=Decimal("-10")), *profile.periods[1:]),
        cash_flow_from_operations=Decimal("6.05"),
        eps=Decimal("-24.2"),
    )

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.PAT_CAGR].status is IpoRatioStatus.NOT_MEANINGFUL
    assert analysis.ratios[IpoRatioName.PAT_MARGIN].value == Decimal("-10.0000")
    assert analysis.ratios[IpoRatioName.ROE].value == Decimal("-20.0000")
    assert analysis.ratios[IpoRatioName.ROCE].value == Decimal("-15.1250")
    assert analysis.ratios[IpoRatioName.CFO_TO_PAT].value == Decimal("-0.5000")
    assert analysis.ratios[IpoRatioName.EPS].value == Decimal("-24.2000")
    assert analysis.ratios[IpoRatioName.PRICE_TO_EARNINGS].status is IpoRatioStatus.NOT_MEANINGFUL


def test_zero_debt_and_finance_cost_are_not_reported_as_infinite_coverage() -> None:
    """Debt-free evidence should produce zero leverage and non-applicable coverage.

    Beginner note:
        Zero debt is a real computed leverage value. Zero finance cost instead makes
        interest coverage inapplicable; displaying infinity would imply a measured
        denominator that the prospectus did not actually report.
    """
    profile = replace(_profile(), total_debt=Decimal("0"))
    profile = _replace_latest_period(profile, finance_cost=Decimal("0"))

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.DEBT_TO_EQUITY].value == Decimal("0.0000")
    assert analysis.ratios[IpoRatioName.NET_DEBT_TO_EBITDA].value == Decimal("-0.2000")
    assert analysis.ratios[IpoRatioName.INTEREST_COVERAGE].status is IpoRatioStatus.NOT_APPLICABLE
    assert analysis.ratios[IpoRatioName.INTEREST_COVERAGE].value is None


def test_high_debt_profile_preserves_large_leverage_ratios() -> None:
    """High leverage is valid evidence and should not be clipped to a score range.

    Beginner note:
        Ratios are evidence, not 0-100 factor scores. This protects the boundary
        between IPO-005 arithmetic and a future normalization policy that may decide
        how a large leverage value affects an investment score.
    """
    profile = replace(_profile(), total_debt=Decimal("302.5"))

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.DEBT_TO_EQUITY].value == Decimal("5.0000")
    assert analysis.ratios[IpoRatioName.NET_DEBT_TO_EBITDA].value == Decimal("9.8000")


def test_legacy_revision_marks_only_new_input_dependencies_missing() -> None:
    """An IPO-004 revision should retain computable ratios after the IPO-005 migration.

    Beginner note:
        Old revisions genuinely lack the new fields. Graceful degradation means only
        ROCE, coverage, and enterprise-value multiples lose values; unrelated ratios
        must remain available from evidence that was already complete.
    """
    profile = _profile()
    legacy_periods = tuple(
        replace(
            period,
            profit_before_tax=None,
            profit_before_tax_page=None,
            finance_cost=None,
            finance_cost_page=None,
        )
        for period in profile.periods
    )
    profile = replace(
        profile,
        periods=legacy_periods,
        total_assets=None,
        total_assets_page=None,
        current_liabilities=None,
        current_liabilities_page=None,
        post_issue_equity_shares=None,
        post_issue_equity_shares_page=None,
    )

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.REVENUE_CAGR].status is IpoRatioStatus.COMPUTED
    assert analysis.ratios[IpoRatioName.ROCE].status is IpoRatioStatus.MISSING_INPUTS
    assert set(analysis.ratios[IpoRatioName.ROCE].missing_inputs) == {
        "profit_before_tax",
        "finance_cost",
        "total_assets",
        "current_liabilities",
    }
    assert analysis.ratios[IpoRatioName.INTEREST_COVERAGE].status is IpoRatioStatus.MISSING_INPUTS
    assert analysis.ratios[IpoRatioName.EV_TO_EBITDA].status is IpoRatioStatus.MISSING_INPUTS
    assert analysis.ratios[IpoRatioName.EV_TO_SALES].status is IpoRatioStatus.MISSING_INPUTS


def test_missing_price_band_suppresses_only_price_dependent_ratios() -> None:
    """Missing issue pricing should not erase operating and balance-sheet ratios.

    Beginner note:
        Price is mutable issue metadata, while margins and returns come from the
        immutable extraction. Keeping those dependency groups separate prevents one
        absent price field from making the entire analysis appear empty.
    """
    analysis = calculate_ipo_ratios(
        _profile(),
        price_band_high=None,
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.ROCE].status is IpoRatioStatus.COMPUTED
    for name in (
        IpoRatioName.PRICE_TO_EARNINGS,
        IpoRatioName.PRICE_TO_BOOK,
        IpoRatioName.EV_TO_EBITDA,
        IpoRatioName.EV_TO_SALES,
    ):
        assert analysis.ratios[name].status is IpoRatioStatus.MISSING_INPUTS
        assert analysis.ratios[name].missing_inputs == ("price_band_high",)


def test_zero_and_negative_denominators_receive_distinct_statuses() -> None:
    """Zero is undefined, while negative equity/capital is economically misleading.

    Beginner note:
        Both cases intentionally return no number, but for different reasons. The
        typed status lets a reader distinguish impossible division from arithmetic
        that is possible yet unsuitable for peer comparison.
    """
    profile = _replace_latest_period(_profile(), revenue=Decimal("0"), ebitda=Decimal("0"))
    profile = replace(
        profile,
        net_worth=Decimal("-1"),
        total_assets=Decimal("10"),
        current_liabilities=Decimal("11"),
    )

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.EBITDA_MARGIN].status is IpoRatioStatus.UNDEFINED
    assert analysis.ratios[IpoRatioName.PAT_MARGIN].status is IpoRatioStatus.UNDEFINED
    assert analysis.ratios[IpoRatioName.ROE].status is IpoRatioStatus.NOT_MEANINGFUL
    assert analysis.ratios[IpoRatioName.DEBT_TO_EQUITY].status is IpoRatioStatus.NOT_MEANINGFUL
    assert analysis.ratios[IpoRatioName.ROCE].status is IpoRatioStatus.NOT_MEANINGFUL
    assert analysis.ratios[IpoRatioName.NET_DEBT_TO_EBITDA].status is IpoRatioStatus.UNDEFINED
    assert analysis.ratios[IpoRatioName.EV_TO_EBITDA].status is IpoRatioStatus.UNDEFINED
    assert analysis.ratios[IpoRatioName.EV_TO_SALES].status is IpoRatioStatus.UNDEFINED


def test_reconciliation_uses_one_percent_or_one_paisa_whichever_is_larger() -> None:
    """Tiny prospectus rounding differences should not be mislabeled as material.

    Beginner note:
        Prospectuses often round per-share figures. Testing values on both sides of
        the threshold proves the engine tolerates ordinary presentation rounding but
        still flags a difference large enough to deserve manual investigation.
    """
    within_tolerance = replace(
        _profile(),
        eps=Decimal("24.44"),
        nav_book_value=Decimal("122.21"),
    )
    outside_tolerance = replace(
        _profile(),
        eps=Decimal("24.45"),
        nav_book_value=Decimal("122.23"),
    )

    within = calculate_ipo_ratios(
        within_tolerance,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )
    outside = calculate_ipo_ratios(
        outside_tolerance,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert within.eps_reconciliation.materially_different is False
    assert within.book_value_reconciliation.materially_different is False
    assert outside.eps_reconciliation.materially_different is True
    assert outside.book_value_reconciliation.materially_different is True


def test_legacy_nonconsecutive_years_do_not_claim_a_two_interval_cagr() -> None:
    """Old profiles with fiscal gaps should return an explicit no-value receipt.

    Beginner note:
        Three rows do not automatically mean two annual intervals. This protects
        legacy IPO-004 evidence from a precise-looking CAGR whose time assumption is
        false, while leaving latest-year ratios untouched.
    """
    profile = _profile()
    profile = replace(
        profile,
        periods=(
            replace(profile.periods[0], period_end=dt.date(2021, 3, 31)),
            replace(profile.periods[1], period_end=dt.date(2023, 3, 31)),
            profile.periods[2],
        ),
    )

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.REVENUE_CAGR].status is IpoRatioStatus.NOT_MEANINGFUL
    assert analysis.ratios[IpoRatioName.PAT_CAGR].status is IpoRatioStatus.NOT_MEANINGFUL
    assert "consecutive" in analysis.ratios[IpoRatioName.REVENUE_CAGR].explanation


def test_dependent_valuations_use_unrounded_per_share_intermediates() -> None:
    """Public four-place rounding must not feed back into later calculations.

    Beginner note:
        EPS is displayed at four places, but P/E must divide by the exact underlying
        EPS. Otherwise a display choice becomes hidden accounting input and can
        compound rounding error in every dependent multiple.
    """
    profile = _profile()
    profile = replace(
        profile,
        financial_amount_unit=IpoAmountUnit.INR,
        equity_share_unit=IpoShareUnit.SHARES,
        equity_shares=Decimal("3"),
        eps=Decimal("0.3333"),
        periods=(
            *profile.periods[:-1],
            replace(profile.periods[-1], pat=Decimal("1")),
        ),
    )

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("1"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.EPS].value == Decimal("0.3333")
    assert analysis.ratios[IpoRatioName.PRICE_TO_EARNINGS].value == Decimal("3.0000")


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("-0.01")])
def test_public_engine_rejects_nonfinite_or_negative_issue_prices(price: Decimal) -> None:
    """A direct caller cannot bypass issue-domain price validation.

    Beginner note:
        The repository normally supplies a validated price, but the pure function is
        public too. Repeating this small guard keeps ``NaN`` or a negative price from
        contaminating every valuation receipt when the engine is called directly.
    """
    with pytest.raises(IpoValidationError, match="price_band_high"):
        calculate_ipo_ratios(
            _profile(),
            price_band_high=price,
            issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
        )


def test_public_engine_requires_timezone_aware_issue_provenance() -> None:
    """A ratio snapshot timestamp must identify one unambiguous UTC instant.

    Beginner note:
        A naive datetime has no timezone and cannot prove which issue-price snapshot
        was used across machines. Requiring an offset makes the receipt replayable and
        converts it to UTC before returning it.
    """
    with pytest.raises(IpoValidationError, match="timezone-aware"):
        calculate_ipo_ratios(
            _profile(),
            price_band_high=Decimal("242"),
            issue_updated_at=dt.datetime(2026, 7, 2),
        )


def test_database_valid_extreme_values_still_round_without_decimal_overflow() -> None:
    """Large Numeric(24,4) inputs must produce a finite receipt instead of crashing.

    Beginner note:
        A very large legal numerator divided by the smallest legal denominator can
        exceed Decimal's default significant-digit context. The engine raises its
        local precision so valid database evidence remains calculable and exact.
    """
    profile = _replace_latest_period(
        _profile(), pat=Decimal("99999999999999999999.9999")
    )
    profile = replace(profile, net_worth=Decimal("0.0001"))

    analysis = calculate_ipo_ratios(
        profile,
        price_band_high=Decimal("242"),
        issue_updated_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )

    assert analysis.ratios[IpoRatioName.ROE].status is IpoRatioStatus.COMPUTED
    assert analysis.ratios[IpoRatioName.ROE].value == Decimal(
        "99999999999999999999999900.0000"
    )
