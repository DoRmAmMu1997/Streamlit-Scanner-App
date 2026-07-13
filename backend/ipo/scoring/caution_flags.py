"""Evaluate the seven IPO-006 hard caution flags against typed evidence.

A hard caution flag is a deterministic red-line check: when one triggers, the
recommendation policy forces ``Not Recommended`` no matter how high the
numeric score is. This module only *evaluates* flags; enforcement lives in
:mod:`backend.ipo.scoring.recommendation`.

Beginner note:
Every flag reports one of three outcomes. ``triggered`` and ``not_triggered``
both mean the rule ran against real evidence; ``not_evaluable`` means the
required evidence was absent, and the report says so instead of letting the
gap pass as a clean check. No rule in this file ever guesses a missing value.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, cast

from backend.ipo.financials.ratio_engine import (
    IpoRatioAnalysis,
    IpoRatioName,
    IpoRatioReceipt,
    IpoRatioStatus,
)
from backend.ipo.manual_extraction import IpoPeerMetric
from backend.ipo.models import (
    IpoCautionFlag,
    IpoCautionFlagReport,
    IpoCautionFlagStatus,
    IpoEnrichmentSignalType,
    IpoStatus,
)
from backend.ipo.scoring.factor_derivation import IpoFactorInputs, _peer_median

CAUTION_FLAGS_VERSION: Final = "ipo-006-flags-v1"

FLAG_ENTIRELY_OFS_WEAK_GROWTH: Final = "entirely_ofs_weak_growth"
FLAG_VERY_EXPENSIVE_VALUATION: Final = "very_expensive_valuation"
FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE: Final = "weak_qib_demand_near_close"
FLAG_NEGATIVE_CFO_DESPITE_PROFITS: Final = "negative_operating_cash_flow_despite_profits"
FLAG_HIGH_DEBT_NO_REDUCTION_USE: Final = "high_debt_without_debt_reduction_use"
FLAG_LITIGATION_RED_FLAG: Final = "litigation_or_auditor_red_flag"
FLAG_LOSS_MAKING_NO_PATH: Final = "loss_making_no_credible_path"

# The report always lists all seven flags in exactly this order so persisted
# receipts stay byte-comparable across runs.
CAUTION_FLAG_ORDER: Final = (
    FLAG_ENTIRELY_OFS_WEAK_GROWTH,
    FLAG_VERY_EXPENSIVE_VALUATION,
    FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
    FLAG_NEGATIVE_CFO_DESPITE_PROFITS,
    FLAG_HIGH_DEBT_NO_REDUCTION_USE,
    FLAG_LITIGATION_RED_FLAG,
    FLAG_LOSS_MAKING_NO_PATH,
)

# Rule thresholds. Any change requires a CAUTION_FLAGS_VERSION bump so stored
# verdicts remain attributable to the exact rules that produced them.
WEAK_GROWTH_CAGR_PERCENT: Final = Decimal("8")
VERY_EXPENSIVE_PREMIUM: Final = Decimal("1.5")
HIGH_DEBT_TO_EQUITY: Final = Decimal("1.5")
HIGH_NET_DEBT_TO_EBITDA: Final = Decimal("3")
QIB_WEAK_MULTIPLE: Final = Decimal("1")
NEAR_CLOSE_WINDOW_DAYS: Final = 1

# Case-folded fragments that count as a debt-reduction use of proceeds.
# "repay" also matches "repayment" and "prepay(ment)"; the check is
# deliberately generous because the safe failure direction is NOT triggering.
DEBT_REDUCTION_KEYWORDS: Final = ("repay", "debt reduction", "reduction of debt", "deleverag")

_TWO_PLACES = Decimal("0.01")


def _fmt(value: Decimal) -> str:
    """Render one decimal at two places so evidence strings stay deterministic."""
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _receipt(
    ratios: IpoRatioAnalysis | None, name: IpoRatioName
) -> IpoRatioReceipt | None:
    """Fetch one ratio receipt, treating an absent snapshot as absent evidence."""
    if ratios is None:
        return None
    return ratios.ratios.get(name)


def _flag(name: str, status: IpoCautionFlagStatus, evidence: str) -> IpoCautionFlag:
    """Build one immutable flag outcome for the fixed-order report."""
    return IpoCautionFlag(name=name, status=status, evidence=evidence)


def _entirely_ofs_weak_growth(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger when a pure offer-for-sale rides on a weak revenue story."""
    profile = inputs.profile
    if profile is None:
        return _flag(
            FLAG_ENTIRELY_OFS_WEAK_GROWTH,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No verified manual extraction on file.",
        )
    canonical = profile.canonical_values
    fresh = canonical["fresh_issue_amount_inr"]
    ofs = canonical["ofs_amount_inr"]
    if fresh > 0 or ofs == 0:
        return _flag(
            FLAG_ENTIRELY_OFS_WEAK_GROWTH,
            IpoCautionFlagStatus.NOT_TRIGGERED,
            f"Fresh issue INR {_fmt(fresh)} is part of the offer.",
        )

    receipt = _receipt(inputs.ratios, IpoRatioName.REVENUE_CAGR)
    if receipt is None or receipt.status is IpoRatioStatus.MISSING_INPUTS:
        return _flag(
            FLAG_ENTIRELY_OFS_WEAK_GROWTH,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "Entirely offer-for-sale, but revenue CAGR is unavailable.",
        )
    if receipt.status is IpoRatioStatus.UNDEFINED:
        return _flag(
            FLAG_ENTIRELY_OFS_WEAK_GROWTH,
            IpoCautionFlagStatus.TRIGGERED,
            f"Entirely offer-for-sale and revenue CAGR is undefined: {receipt.explanation}",
        )
    if receipt.value is not None and receipt.value < WEAK_GROWTH_CAGR_PERCENT:
        return _flag(
            FLAG_ENTIRELY_OFS_WEAK_GROWTH,
            IpoCautionFlagStatus.TRIGGERED,
            (
                f"Entirely offer-for-sale with revenue CAGR {_fmt(receipt.value)}% "
                f"below {WEAK_GROWTH_CAGR_PERCENT}%."
            ),
        )
    grown = _fmt(receipt.value) if receipt.value is not None else "n/a"
    return _flag(
        FLAG_ENTIRELY_OFS_WEAK_GROWTH,
        IpoCautionFlagStatus.NOT_TRIGGERED,
        f"Entirely offer-for-sale but revenue CAGR {grown}% clears the weak-growth bar.",
    )


def _very_expensive_valuation(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger when the issue's P/E premium exceeds 1.5x the peer median."""
    receipt = _receipt(inputs.ratios, IpoRatioName.PRICE_TO_EARNINGS)
    if (
        receipt is None
        or receipt.status is not IpoRatioStatus.COMPUTED
        or receipt.value is None
    ):
        return _flag(
            FLAG_VERY_EXPENSIVE_VALUATION,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No computed P/E for this issue (price band or earnings evidence missing).",
        )
    median = _peer_median(inputs.profile, IpoPeerMetric.PE)
    if median is None:
        return _flag(
            FLAG_VERY_EXPENSIVE_VALUATION,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No positive peer P/E metrics on file to compare against.",
        )
    premium = receipt.value / median
    if premium > VERY_EXPENSIVE_PREMIUM:
        return _flag(
            FLAG_VERY_EXPENSIVE_VALUATION,
            IpoCautionFlagStatus.TRIGGERED,
            (
                f"P/E {_fmt(receipt.value)} is {_fmt(premium)}x the peer median "
                f"{_fmt(median)} (limit {VERY_EXPENSIVE_PREMIUM}x)."
            ),
        )
    return _flag(
        FLAG_VERY_EXPENSIVE_VALUATION,
        IpoCautionFlagStatus.NOT_TRIGGERED,
        (
            f"P/E {_fmt(receipt.value)} is {_fmt(premium)}x the peer median "
            f"{_fmt(median)}, within the {VERY_EXPENSIVE_PREMIUM}x limit."
        ),
    )


def _weak_qib_demand_near_close(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger on weak or absent QIB demand once the book is about to close.

    Beginner note:
        Demand data cannot meaningfully exist before the issue window, so the
        rule only judges from one day before the close date onward and only
        while the issue is open or closed. Inside that window an *absent*
        snapshot is itself the warning the spec asks for.
    """
    issue = inputs.issue
    if issue.status not in (IpoStatus.OPEN, IpoStatus.CLOSED) or issue.close_date is None:
        return _flag(
            FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "Issue is not in its subscription window yet.",
        )
    window_start = issue.close_date - dt.timedelta(days=NEAR_CLOSE_WINDOW_DAYS)
    if inputs.as_of.date() < window_start:
        return _flag(
            FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            f"Book closes {issue.close_date.isoformat()}; too early to judge demand.",
        )
    subscription = inputs.subscription
    if subscription is None or subscription.qib_multiple is None:
        return _flag(
            FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
            IpoCautionFlagStatus.TRIGGERED,
            "No QIB demand snapshot available this close to the book closing.",
        )
    if subscription.qib_multiple < QIB_WEAK_MULTIPLE:
        return _flag(
            FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
            IpoCautionFlagStatus.TRIGGERED,
            (
                f"QIB book only {_fmt(subscription.qib_multiple)}x subscribed "
                "near the close."
            ),
        )
    return _flag(
        FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
        IpoCautionFlagStatus.NOT_TRIGGERED,
        f"QIB book {_fmt(subscription.qib_multiple)}x subscribed near the close.",
    )


def _negative_cfo_despite_profits(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger when reported profit is not backed by operating cash flow."""
    profile = inputs.profile
    if profile is None:
        return _flag(
            FLAG_NEGATIVE_CFO_DESPITE_PROFITS,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No verified manual extraction on file.",
        )
    canonical = profile.canonical_values
    cfo = canonical["cash_flow_from_operations_inr"]
    latest = profile.period_values_inr()[-1]
    # period_values_inr mixes dates and Decimals in one row dict; the pat_inr
    # key is always a Decimal, so narrow the union for the comparison below.
    latest_pat = cast(Decimal, latest["pat_inr"])
    if cfo < 0 and latest_pat > 0:
        return _flag(
            FLAG_NEGATIVE_CFO_DESPITE_PROFITS,
            IpoCautionFlagStatus.TRIGGERED,
            (
                f"Operating cash flow INR {_fmt(cfo)} is negative while the "
                f"latest PAT INR {_fmt(latest_pat)} is positive."
            ),
        )
    return _flag(
        FLAG_NEGATIVE_CFO_DESPITE_PROFITS,
        IpoCautionFlagStatus.NOT_TRIGGERED,
        (
            f"Operating cash flow INR {_fmt(cfo)} against latest PAT INR "
            f"{_fmt(latest_pat)} shows no profit/cash divergence."
        ),
    )


def _high_debt_without_reduction_use(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger on high leverage when the objects of issue skip debt repayment."""
    debt_receipts = [
        receipt
        for receipt in (
            _receipt(inputs.ratios, IpoRatioName.DEBT_TO_EQUITY),
            _receipt(inputs.ratios, IpoRatioName.NET_DEBT_TO_EBITDA),
        )
        if receipt is not None
        and receipt.status is IpoRatioStatus.COMPUTED
        and receipt.value is not None
    ]
    if not debt_receipts or inputs.profile is None:
        return _flag(
            FLAG_HIGH_DEBT_NO_REDUCTION_USE,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No computed leverage ratios or verified objects of issue on file.",
        )
    thresholds = {
        IpoRatioName.DEBT_TO_EQUITY: HIGH_DEBT_TO_EQUITY,
        IpoRatioName.NET_DEBT_TO_EBITDA: HIGH_NET_DEBT_TO_EBITDA,
    }
    breaches = [
        receipt
        for receipt in debt_receipts
        if receipt.value is not None and receipt.value > thresholds[receipt.name]
    ]
    if not breaches:
        summary = ", ".join(
            f"{receipt.name.value} {_fmt(receipt.value)}"
            for receipt in debt_receipts
            if receipt.value is not None
        )
        return _flag(
            FLAG_HIGH_DEBT_NO_REDUCTION_USE,
            IpoCautionFlagStatus.NOT_TRIGGERED,
            f"Leverage within limits ({summary}).",
        )
    objects_text = inputs.profile.objects_of_issue.casefold()
    if any(keyword in objects_text for keyword in DEBT_REDUCTION_KEYWORDS):
        return _flag(
            FLAG_HIGH_DEBT_NO_REDUCTION_USE,
            IpoCautionFlagStatus.NOT_TRIGGERED,
            "Leverage is high but the objects of issue name debt repayment.",
        )
    summary = ", ".join(
        f"{receipt.name.value} {_fmt(receipt.value)}"
        for receipt in breaches
        if receipt.value is not None
    )
    return _flag(
        FLAG_HIGH_DEBT_NO_REDUCTION_USE,
        IpoCautionFlagStatus.TRIGGERED,
        f"High leverage ({summary}) with no debt-reduction use of proceeds.",
    )


def _litigation_red_flag(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger on keyword-matched litigation signals from clean web evidence.

    Beginner note:
        Only the collector's recorded keyword matches are read here — never
        snippet text — and quarantined signals are ignored entirely. A row that
        tripped the prompt-injection scanner can therefore never argue its way
        into a verdict, in either direction.
    """
    litigation_signals = [
        signal
        for signal in inputs.enrichment
        if signal.signal_type is IpoEnrichmentSignalType.LITIGATION_RED_FLAG
    ]
    if not litigation_signals:
        return _flag(
            FLAG_LITIGATION_RED_FLAG,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No litigation web signals collected (enrichment absent).",
        )
    matched: list[str] = []
    for signal in litigation_signals:
        if signal.quarantined:
            continue
        for entry in signal.payload:
            for keyword in entry.get("matched_keywords", ()):
                if keyword not in matched:
                    matched.append(str(keyword))
    if matched:
        return _flag(
            FLAG_LITIGATION_RED_FLAG,
            IpoCautionFlagStatus.TRIGGERED,
            (
                "Litigation-related web signals matched keywords: "
                + ", ".join(sorted(matched))
                + " (low-confidence web source)."
            ),
        )
    return _flag(
        FLAG_LITIGATION_RED_FLAG,
        IpoCautionFlagStatus.NOT_TRIGGERED,
        "Litigation web signals collected; no red-flag keywords matched.",
    )


def _loss_making_no_path(inputs: IpoFactorInputs) -> IpoCautionFlag:
    """Trigger when the latest year is a loss and the loss is not narrowing."""
    profile = inputs.profile
    if profile is None:
        return _flag(
            FLAG_LOSS_MAKING_NO_PATH,
            IpoCautionFlagStatus.NOT_EVALUABLE,
            "No verified manual extraction on file.",
        )
    periods = profile.period_values_inr()
    # Same union-narrowing note as the cash-flow flag: pat_inr is a Decimal.
    latest_pat = cast(Decimal, periods[-1]["pat_inr"])
    previous_pat = cast(Decimal, periods[-2]["pat_inr"])
    if latest_pat >= 0:
        return _flag(
            FLAG_LOSS_MAKING_NO_PATH,
            IpoCautionFlagStatus.NOT_TRIGGERED,
            f"Latest PAT INR {_fmt(latest_pat)} is not a loss.",
        )
    if latest_pat <= previous_pat:
        return _flag(
            FLAG_LOSS_MAKING_NO_PATH,
            IpoCautionFlagStatus.TRIGGERED,
            (
                f"Latest PAT INR {_fmt(latest_pat)} is a loss that is not "
                f"narrowing (previous year INR {_fmt(previous_pat)})."
            ),
        )
    return _flag(
        FLAG_LOSS_MAKING_NO_PATH,
        IpoCautionFlagStatus.NOT_TRIGGERED,
        (
            f"Latest PAT INR {_fmt(latest_pat)} is a loss but narrowing from "
            f"INR {_fmt(previous_pat)}."
        ),
    )


def evaluate_caution_flags(inputs: IpoFactorInputs) -> IpoCautionFlagReport:
    """Evaluate all seven hard caution flags in their fixed catalog order.

    Args:
        inputs: The same frozen evidence bundle factor derivation consumes, so
            the flags and the factors always judge one consistent snapshot.

    Returns:
        A complete report — every flag present with a status and evidence line
        — stamped with :data:`CAUTION_FLAGS_VERSION`.
    """
    return IpoCautionFlagReport(
        version=CAUTION_FLAGS_VERSION,
        flags=(
            _entirely_ofs_weak_growth(inputs),
            _very_expensive_valuation(inputs),
            _weak_qib_demand_near_close(inputs),
            _negative_cfo_despite_profits(inputs),
            _high_debt_without_reduction_use(inputs),
            _litigation_red_flag(inputs),
            _loss_making_no_path(inputs),
        ),
    )
