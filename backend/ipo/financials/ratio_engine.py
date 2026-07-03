"""Compute deterministic general-company IPO ratios from manual evidence.

Beginner note:
This module performs accounting arithmetic only. It never fetches Screener.in,
opens a database session, or assigns an investment score. Keeping those concerns
outside the engine makes every output replayable from one immutable extraction
revision and one snapshotted upper price-band value.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from backend.ipo.manual_extraction import IpoManualExtractionRecord
from backend.ipo.models import IpoValidationError

FORMULA_VERSION: Final = "ipo-ratio-v1"
_FOUR_PLACES: Final = Decimal("0.0001")
_PERCENT: Final = Decimal("100")
_RECONCILIATION_ABSOLUTE_TOLERANCE: Final = Decimal("0.01")
_RECONCILIATION_RELATIVE_TOLERANCE: Final = Decimal("0.01")


class IpoRatioName(StrEnum):
    """Stable identifiers for the sixteen IPO-005 general-company ratios.

    Beginner note:
    Callers compare enum members instead of display labels, so wording in a UI
    can change without breaking saved tests, integrations, or missing-data logic.
    """

    REVENUE_CAGR = "revenue_cagr"
    PAT_CAGR = "pat_cagr"
    EBITDA_MARGIN = "ebitda_margin"
    PAT_MARGIN = "pat_margin"
    ROE = "roe"
    ROCE = "roce"
    DEBT_TO_EQUITY = "debt_to_equity"
    NET_DEBT_TO_EBITDA = "net_debt_to_ebitda"
    INTEREST_COVERAGE = "interest_coverage"
    CFO_TO_PAT = "cfo_to_pat"
    EPS = "eps"
    BOOK_VALUE_PER_SHARE = "book_value_per_share"
    PRICE_TO_EARNINGS = "price_to_earnings"
    PRICE_TO_BOOK = "price_to_book"
    EV_TO_EBITDA = "ev_to_ebitda"
    EV_TO_SALES = "ev_to_sales"


class IpoRatioStatus(StrEnum):
    """Explain why a ratio has a number or deliberately has no number.

    Beginner note:
    ``None`` alone is ambiguous. These states distinguish absent source evidence,
    division by zero, an economically misleading result, and a ratio that simply
    does not apply, which keeps graceful degradation auditable.
    """

    COMPUTED = "computed"
    MISSING_INPUTS = "missing_inputs"
    UNDEFINED = "undefined"
    NOT_MEANINGFUL = "not_meaningful"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class IpoRatioReceipt:
    """One ratio value together with enough context to explain its absence.

    Beginner note:
    The formula and missing-field names are static metadata, not submitted text.
    A UI can therefore explain a blank value without logging sensitive narratives
    or trying to reverse-engineer which branch the calculation took.
    """

    name: IpoRatioName
    value: Decimal | None
    status: IpoRatioStatus
    formula: str
    missing_inputs: tuple[str, ...] = ()
    explanation: str = ""


@dataclass(frozen=True)
class IpoPerShareReconciliation:
    """Compare one computed per-share value with the prospectus-reported value.

    Beginner note:
    A prospectus may round EPS or NAV differently. The engine preserves both the
    reported evidence and its own calculation rather than silently choosing one.
    """

    computed: Decimal | None
    reported: Decimal
    difference: Decimal | None
    materially_different: bool | None


@dataclass(frozen=True)
class IpoRatioAnalysis:
    """Immutable ratio snapshot tied to source and issue-price provenance.

    Beginner note:
    The document hash identifies the exact manual source and ``issue_updated_at``
    identifies the mutable price-band snapshot. Together they explain precisely
    which evidence produced an on-demand result even though ratios are not stored.
    """

    formula_version: str
    extraction_id: int
    issue_id: int
    source_content_sha256: str
    price_band_high: Decimal | None
    issue_updated_at: dt.datetime
    ratios: Mapping[IpoRatioName, IpoRatioReceipt]
    eps_reconciliation: IpoPerShareReconciliation
    book_value_reconciliation: IpoPerShareReconciliation


def _rounded(value: Decimal) -> Decimal:
    """Round a public ratio half-up while preserving exact internal arithmetic.

    Beginner note:
    A legal ``Numeric(24,4)`` numerator divided by a tiny legal denominator can
    have more than Decimal's default 28 significant digits. Raising precision
    locally lets that bounded result be quantized without changing process-wide
    arithmetic settings or falling back to an imprecise float.
    """
    with localcontext() as context:
        context.prec = 60
        return value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _computed(name: IpoRatioName, value: Decimal, formula: str) -> IpoRatioReceipt:
    """Build one successful ratio receipt with the shared rounding policy."""
    return IpoRatioReceipt(
        name=name,
        value=_rounded(value),
        status=IpoRatioStatus.COMPUTED,
        formula=formula,
    )


def _unavailable(
    name: IpoRatioName,
    status: IpoRatioStatus,
    formula: str,
    explanation: str,
    *missing_inputs: str,
) -> IpoRatioReceipt:
    """Build a deliberate no-value receipt instead of raising or inventing data."""
    return IpoRatioReceipt(
        name=name,
        value=None,
        status=status,
        formula=formula,
        missing_inputs=tuple(missing_inputs),
        explanation=explanation,
    )


def _ratio(
    name: IpoRatioName,
    numerator: Decimal,
    denominator: Decimal,
    formula: str,
    *,
    percentage: bool = False,
    require_positive_denominator: bool = False,
) -> IpoRatioReceipt:
    """Divide safely and distinguish zero from economically misleading negatives."""
    if denominator == 0:
        return _unavailable(
            name,
            IpoRatioStatus.UNDEFINED,
            formula,
            "The denominator is zero, so the ratio is mathematically undefined.",
        )
    if require_positive_denominator and denominator < 0:
        return _unavailable(
            name,
            IpoRatioStatus.NOT_MEANINGFUL,
            formula,
            "A negative denominator would produce a numeric but misleading ratio.",
        )
    value = numerator / denominator
    return _computed(name, value * _PERCENT if percentage else value, formula)


def _cagr(
    name: IpoRatioName,
    first: Decimal,
    last: Decimal,
    formula: str,
) -> IpoRatioReceipt:
    """Calculate a two-interval CAGR only when both endpoints are positive."""
    if first == 0:
        return _unavailable(
            name,
            IpoRatioStatus.UNDEFINED,
            formula,
            "CAGR cannot divide by a zero starting value.",
        )
    if first < 0 or last <= 0:
        return _unavailable(
            name,
            IpoRatioStatus.NOT_MEANINGFUL,
            formula,
            "CAGR is not economically meaningful across a non-positive endpoint.",
        )
    # Exactly three annual periods create two compounding intervals. Decimal.sqrt
    # avoids silently converting audited monetary values to binary floating point.
    with localcontext() as context:
        context.prec = 40
        growth = (last / first).sqrt() - Decimal(1)
    return _computed(name, growth * _PERCENT, formula)


def _reconciliation(computed: Decimal | None, reported: Decimal) -> IpoPerShareReconciliation:
    """Compare computed and reported values using the approved materiality rule."""
    if computed is None:
        return IpoPerShareReconciliation(None, reported, None, None)
    rounded_computed = _rounded(computed)
    difference = _rounded(rounded_computed - reported)
    tolerance = max(
        _RECONCILIATION_ABSOLUTE_TOLERANCE,
        abs(reported) * _RECONCILIATION_RELATIVE_TOLERANCE,
    )
    return IpoPerShareReconciliation(
        computed=rounded_computed,
        reported=reported,
        difference=difference,
        materially_different=abs(difference) > tolerance,
    )


def calculate_ipo_ratios(
    profile: IpoManualExtractionRecord,
    *,
    price_band_high: Decimal | None,
    issue_updated_at: dt.datetime,
) -> IpoRatioAnalysis:
    """Calculate all sixteen general-company ratios from one immutable profile.

    Args:
        profile: Latest detached manual-extraction revision.
        price_band_high: Snapshotted upper issue price in INR per share, or ``None``.
        issue_updated_at: Timestamp identifying the mutable issue snapshot used.

    Returns:
        A frozen analysis with one receipt for every ratio, including unavailable
        values and their exact reason.

    Beginner note:
        Missing legacy fields suppress only the affected ratios. This is graceful
        degradation: useful historical ratios remain visible, but the engine never
        fills absent evidence with zero or a guessed accounting proxy.
    """
    periods = tuple(sorted(profile.periods, key=lambda period: period.period_end))
    if len(periods) != 3:
        raise IpoValidationError("IPO ratios require exactly three fiscal periods.")
    if price_band_high is not None and (
        not isinstance(price_band_high, Decimal)
        or not price_band_high.is_finite()
        or price_band_high < 0
    ):
        raise IpoValidationError(
            "price_band_high must be a finite non-negative Decimal when provided."
        )
    if (
        not isinstance(issue_updated_at, dt.datetime)
        or issue_updated_at.tzinfo is None
        or issue_updated_at.utcoffset() is None
    ):
        raise IpoValidationError("issue_updated_at must be timezone-aware.")
    issue_updated_at = issue_updated_at.astimezone(dt.UTC)

    first = periods[0]
    latest = periods[-1]
    unit = profile.financial_amount_unit
    revenue_first = unit.to_inr(first.revenue)
    revenue = unit.to_inr(latest.revenue)
    ebitda = unit.to_inr(latest.ebitda)
    pat_first = unit.to_inr(first.pat)
    pat = unit.to_inr(latest.pat)
    values = profile.canonical_values
    net_worth = values["net_worth_inr"]
    debt = values["total_debt_inr"]
    cash = values["cash_inr"]
    cfo = values["cash_flow_from_operations_inr"]
    shares = values["equity_shares"]

    ratios: dict[IpoRatioName, IpoRatioReceipt] = {}
    period_years = [period.period_end.year for period in periods]
    if period_years == list(range(period_years[0], period_years[0] + 3)):
        ratios[IpoRatioName.REVENUE_CAGR] = _cagr(
            IpoRatioName.REVENUE_CAGR,
            revenue_first,
            revenue,
            "((FY3 revenue / FY1 revenue) ^ (1 / 2) - 1) * 100",
        )
        ratios[IpoRatioName.PAT_CAGR] = _cagr(
            IpoRatioName.PAT_CAGR,
            pat_first,
            pat,
            "((FY3 PAT / FY1 PAT) ^ (1 / 2) - 1) * 100",
        )
    else:
        # IPO-004 accepted any three distinct dates. A legacy revision may
        # therefore contain gaps, for which a hard-coded two-interval CAGR would
        # be precise-looking but false. Other latest-period ratios remain usable.
        for name, formula in (
            (
                IpoRatioName.REVENUE_CAGR,
                "((FY3 revenue / FY1 revenue) ^ (1 / 2) - 1) * 100",
            ),
            (
                IpoRatioName.PAT_CAGR,
                "((FY3 PAT / FY1 PAT) ^ (1 / 2) - 1) * 100",
            ),
        ):
            ratios[name] = _unavailable(
                name,
                IpoRatioStatus.NOT_MEANINGFUL,
                formula,
                "CAGR requires three consecutive annual fiscal years.",
            )
    ratios[IpoRatioName.EBITDA_MARGIN] = _ratio(
        IpoRatioName.EBITDA_MARGIN,
        ebitda,
        revenue,
        "FY3 EBITDA / FY3 revenue * 100",
        percentage=True,
    )
    ratios[IpoRatioName.PAT_MARGIN] = _ratio(
        IpoRatioName.PAT_MARGIN,
        pat,
        revenue,
        "FY3 PAT / FY3 revenue * 100",
        percentage=True,
    )
    ratios[IpoRatioName.ROE] = _ratio(
        IpoRatioName.ROE,
        pat,
        net_worth,
        "FY3 PAT / closing net worth * 100",
        percentage=True,
        require_positive_denominator=True,
    )
    ratios[IpoRatioName.DEBT_TO_EQUITY] = _ratio(
        IpoRatioName.DEBT_TO_EQUITY,
        debt,
        net_worth,
        "total debt / closing net worth",
        require_positive_denominator=True,
    )
    ratios[IpoRatioName.NET_DEBT_TO_EBITDA] = _ratio(
        IpoRatioName.NET_DEBT_TO_EBITDA,
        debt - cash,
        ebitda,
        "(total debt - cash) / FY3 EBITDA",
        require_positive_denominator=True,
    )
    ratios[IpoRatioName.CFO_TO_PAT] = _ratio(
        IpoRatioName.CFO_TO_PAT,
        cfo,
        pat,
        "cash flow from operations / FY3 PAT",
    )
    ratios[IpoRatioName.EPS] = _ratio(
        IpoRatioName.EPS,
        pat,
        shares,
        "FY3 PAT / sourced equity shares",
        require_positive_denominator=True,
    )
    ratios[IpoRatioName.BOOK_VALUE_PER_SHARE] = _ratio(
        IpoRatioName.BOOK_VALUE_PER_SHARE,
        net_worth,
        shares,
        "closing net worth / sourced equity shares",
        require_positive_denominator=True,
    )
    # Keep full-precision intermediates separate from the rounded public receipts.
    # Feeding a four-place EPS back into P/E would make downstream answers depend
    # on display precision instead of the immutable source values.
    eps_unrounded = pat / shares if shares > 0 else None
    book_value_unrounded = net_worth / shares if shares > 0 else None

    # IPO-005 facts may be absent only on legacy IPO-004 revisions. Build their
    # dependent receipts explicitly so callers can distinguish old evidence from
    # an invalid denominator in otherwise complete evidence.
    pbt = latest.profit_before_tax
    finance_cost = latest.finance_cost
    if pbt is None or finance_cost is None:
        interest_missing = tuple(
            name
            for name, value in (
                ("profit_before_tax", pbt),
                ("finance_cost", finance_cost),
            )
            if value is None
        )
        ratios[IpoRatioName.ROCE] = _unavailable(
            IpoRatioName.ROCE,
            IpoRatioStatus.MISSING_INPUTS,
            "(FY3 PBT + FY3 finance cost) / (total assets - current liabilities) * 100",
            "The legacy extraction does not contain all IPO-005 ROCE inputs.",
            *interest_missing,
            *(
                name
                for name, value in (
                    ("total_assets", profile.total_assets),
                    ("current_liabilities", profile.current_liabilities),
                )
                if value is None
            ),
        )
        ratios[IpoRatioName.INTEREST_COVERAGE] = _unavailable(
            IpoRatioName.INTEREST_COVERAGE,
            IpoRatioStatus.MISSING_INPUTS,
            "(FY3 PBT + FY3 finance cost) / FY3 finance cost",
            "The legacy extraction does not contain all IPO-005 coverage inputs.",
            *interest_missing,
        )
        ebit: Decimal | None = None
    else:
        pbt_inr = unit.to_inr(pbt)
        finance_cost_inr = unit.to_inr(finance_cost)
        ebit = pbt_inr + finance_cost_inr
        if finance_cost_inr == 0:
            ratios[IpoRatioName.INTEREST_COVERAGE] = _unavailable(
                IpoRatioName.INTEREST_COVERAGE,
                IpoRatioStatus.NOT_APPLICABLE,
                "(FY3 PBT + FY3 finance cost) / FY3 finance cost",
                "No finance cost was reported, so coverage is not applicable rather than infinite.",
            )
        else:
            ratios[IpoRatioName.INTEREST_COVERAGE] = _computed(
                IpoRatioName.INTEREST_COVERAGE,
                ebit / finance_cost_inr,
                "(FY3 PBT + FY3 finance cost) / FY3 finance cost",
            )

        if profile.total_assets is None or profile.current_liabilities is None:
            missing = tuple(
                name
                for name, value in (
                    ("total_assets", profile.total_assets),
                    ("current_liabilities", profile.current_liabilities),
                )
                if value is None
            )
            ratios[IpoRatioName.ROCE] = _unavailable(
                IpoRatioName.ROCE,
                IpoRatioStatus.MISSING_INPUTS,
                "(FY3 PBT + FY3 finance cost) / (total assets - current liabilities) * 100",
                "The legacy extraction does not contain all IPO-005 ROCE inputs.",
                *missing,
            )
        else:
            capital_employed = unit.to_inr(profile.total_assets) - unit.to_inr(
                profile.current_liabilities
            )
            ratios[IpoRatioName.ROCE] = _ratio(
                IpoRatioName.ROCE,
                ebit,
                capital_employed,
                "(FY3 PBT + FY3 finance cost) / (total assets - current liabilities) * 100",
                percentage=True,
                require_positive_denominator=True,
            )

    if price_band_high is None:
        for name, denominator_name in (
            (IpoRatioName.PRICE_TO_EARNINGS, "price_band_high"),
            (IpoRatioName.PRICE_TO_BOOK, "price_band_high"),
            (IpoRatioName.EV_TO_EBITDA, "price_band_high"),
            (IpoRatioName.EV_TO_SALES, "price_band_high"),
        ):
            ratios[name] = _unavailable(
                name,
                IpoRatioStatus.MISSING_INPUTS,
                "upper price-band valuation",
                "The issue does not have an upper price band.",
                denominator_name,
            )
    else:
        if eps_unrounded is None or eps_unrounded <= 0:
            ratios[IpoRatioName.PRICE_TO_EARNINGS] = _unavailable(
                IpoRatioName.PRICE_TO_EARNINGS,
                IpoRatioStatus.NOT_MEANINGFUL,
                "upper price band / computed EPS",
                "P/E is not meaningful when computed EPS is non-positive.",
            )
        else:
            ratios[IpoRatioName.PRICE_TO_EARNINGS] = _computed(
                IpoRatioName.PRICE_TO_EARNINGS,
                price_band_high / eps_unrounded,
                "upper price band / computed EPS",
            )
        if book_value_unrounded is None or book_value_unrounded <= 0:
            ratios[IpoRatioName.PRICE_TO_BOOK] = _unavailable(
                IpoRatioName.PRICE_TO_BOOK,
                IpoRatioStatus.NOT_MEANINGFUL,
                "upper price band / computed book value per share",
                "P/B is not meaningful when computed book value is non-positive.",
            )
        else:
            ratios[IpoRatioName.PRICE_TO_BOOK] = _computed(
                IpoRatioName.PRICE_TO_BOOK,
                price_band_high / book_value_unrounded,
                "upper price band / computed book value per share",
            )

        if profile.post_issue_equity_shares is None:
            for name in (IpoRatioName.EV_TO_EBITDA, IpoRatioName.EV_TO_SALES):
                ratios[name] = _unavailable(
                    name,
                    IpoRatioStatus.MISSING_INPUTS,
                    "enterprise value multiple",
                    "The legacy extraction does not contain post-issue shares.",
                    "post_issue_equity_shares",
                )
        else:
            post_issue_shares = profile.equity_share_unit.to_shares(
                profile.post_issue_equity_shares
            )
            enterprise_value = price_band_high * post_issue_shares + debt - cash
            ratios[IpoRatioName.EV_TO_EBITDA] = _ratio(
                IpoRatioName.EV_TO_EBITDA,
                enterprise_value,
                ebitda,
                "(upper price band * post-issue shares + debt - cash) / FY3 EBITDA",
                require_positive_denominator=True,
            )
            ratios[IpoRatioName.EV_TO_SALES] = _ratio(
                IpoRatioName.EV_TO_SALES,
                enterprise_value,
                revenue,
                "(upper price band * post-issue shares + debt - cash) / FY3 revenue",
                require_positive_denominator=True,
            )

    return IpoRatioAnalysis(
        formula_version=FORMULA_VERSION,
        extraction_id=profile.id,
        issue_id=profile.issue_id,
        source_content_sha256=profile.source_content_sha256,
        price_band_high=price_band_high,
        issue_updated_at=issue_updated_at,
        ratios=MappingProxyType(ratios),
        eps_reconciliation=_reconciliation(eps_unrounded, profile.eps),
        book_value_reconciliation=_reconciliation(
            book_value_unrounded, profile.nav_book_value
        ),
    )
