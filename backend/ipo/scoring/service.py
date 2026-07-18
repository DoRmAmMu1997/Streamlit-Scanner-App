"""IPO-006 scoring service: load evidence, derive factors, persist verdicts.

This is the one place that assembles the full evidence bundle for an issue
(latest manual profile, on-demand ratios, newest subscription snapshot, and
enrichment signals), runs the pure factor/flag/score/verdict pipeline, and
persists the immutable evaluation pair. Both the ``run_ipo_screener`` job and
the dashboard's re-score button call :func:`rescore_issue`, so a manual click
and a scheduled run can never disagree about how scoring works.

Beginner note — how idempotency works here:
Before persisting, the service computes a SHA-256 *inputs fingerprint* over
exactly the evidence and rule versions it consumed. If the newest stored
evaluation was produced by the same model version from the same fingerprint,
re-scoring would write a byte-identical row, so the service reports
``skipped_unchanged`` instead. Re-running the screener is therefore free
until some real input (a new revision, price band, subscription snapshot, or
enrichment observation) actually changes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Final, Literal

from backend.ipo.models import (
    IpoEnrichmentBatchUsability,
    IpoEnrichmentSignalType,
    IpoEvaluationRecord,
    IpoStatus,
)
from backend.ipo.repository import (
    SessionFactory,
    evaluate_issue,
    get_latest_evaluation,
    load_ipo_factor_inputs_snapshot,
)
from backend.ipo.scoring.caution_flags import (
    CAUTION_FLAGS_VERSION,
    NEAR_CLOSE_WINDOW_DAYS,
    evaluate_caution_flags,
)
from backend.ipo.scoring.factor_derivation import (
    FACTOR_MODEL_VERSION,
    GMP_SIGNAL_MAX_AGE_DAYS,
    IpoFactorInputs,
    derive_score_input,
)
from backend.observability import EVENT_IPO_ISSUE_SCORED, log_event
from backend.storage import session_scope

logger = logging.getLogger(__name__)

SCREENER_MODEL_VERSION: Final = "ipo-006-v2"


@dataclass(frozen=True)
class IpoRescoreOutcome:
    """What one re-score attempt did for one issue.

    ``insufficient_inputs`` writes nothing: an issue without a verified manual
    profile belongs in the dashboard's missing-data queue, not in evaluation
    history with a fabricated all-missing score.
    """

    issue_id: int
    company_name: str
    status: Literal["evaluated", "skipped_unchanged", "insufficient_inputs"]
    evaluation: IpoEvaluationRecord | None = None
    missing: tuple[str, ...] = ()


def compute_inputs_fingerprint(inputs: IpoFactorInputs) -> str:
    """Hash exactly the evidence and rule versions scoring will consume.

    Beginner note:
        Two time-derived facts are hashed instead of the clock itself: the set
        of GMP observations still inside the staleness window, and whether the
        issue is inside its near-close demand window. Hashing ``as_of``
        directly would change the fingerprint every run and defeat
        idempotency; hashing the derived facts re-scores exactly when the
        passage of time would actually change a factor or flag.
    """
    issue = inputs.issue
    profile = inputs.profile
    subscription = inputs.subscription
    cutoff = inputs.as_of - dt.timedelta(days=GMP_SIGNAL_MAX_AGE_DAYS)
    enrichment_facts = [
        {
            "semantic_hash": signal.semantic_hash,
            "signal_type": signal.signal_type.value,
            "parsed_value": (
                str(signal.parsed_value)
                if signal.parsed_value is not None
                else None
            ),
            "batch_usability": signal.batch_usability.value,
            "authority": signal.authority.value,
            "corroborated": signal.corroborated,
            "authority_policy_version": signal.authority_policy_version,
            "source_policy": signal.source_policy,
            "payload": [dict(entry) for entry in signal.payload],
            "inside_freshness_window": (
                (signal.last_seen_at or signal.captured_at) >= cutoff
                if signal.signal_type is IpoEnrichmentSignalType.GMP
                else None
            ),
        }
        for signal in inputs.enrichment
        if signal.batch_usability
        is not IpoEnrichmentBatchUsability.NOT_EVALUABLE
    ]
    enrichment_facts.sort(
        key=lambda fact: json.dumps(fact, sort_keys=True, separators=(",", ":"))
    )
    ratio_facts = (
        {
            "formula_version": inputs.ratios.formula_version,
            "source_sha256": inputs.ratios.source_content_sha256,
            "ratios": {
                name.value: {
                    "status": receipt.status.value,
                    "value": (
                        str(receipt.value)
                        if receipt.value is not None
                        else None
                    ),
                    "explanation": receipt.explanation,
                }
                for name, receipt in sorted(
                    inputs.ratios.ratios.items(),
                    key=lambda item: item[0].value,
                )
            },
        }
        if inputs.ratios is not None
        else None
    )
    near_close = (
        issue.status in (IpoStatus.OPEN, IpoStatus.CLOSED)
        and issue.close_date is not None
        and inputs.as_of.date()
        >= issue.close_date - dt.timedelta(days=NEAR_CLOSE_WINDOW_DAYS)
    )
    payload = {
        "screener_model_version": SCREENER_MODEL_VERSION,
        "factor_model_version": FACTOR_MODEL_VERSION,
        "caution_flags_version": CAUTION_FLAGS_VERSION,
        "issue": {
            "company_name": issue.company_name,
            "issue_type": issue.issue_type.value,
            "status": issue.status.value,
            "open_date": issue.open_date.isoformat() if issue.open_date else None,
            "close_date": issue.close_date.isoformat() if issue.close_date else None,
            "price_band_low": (
                str(issue.price_band_low)
                if issue.price_band_low is not None
                else None
            ),
            "price_band_high": (
                str(issue.price_band_high)
                if issue.price_band_high is not None
                else None
            ),
        },
        "extraction": (
            {
                "sha256": profile.source_content_sha256,
                "source_document_url": profile.source_document_url,
                "units": {
                    "financial": profile.financial_amount_unit.value,
                    "issue": profile.issue_amount_unit.value,
                    "shares": profile.equity_share_unit.value,
                },
                "canonical_values": {
                    key: str(value)
                    for key, value in sorted(profile.canonical_values.items())
                },
                "periods": [
                    {
                        key: value.isoformat()
                        if isinstance(value, dt.date)
                        else str(value)
                        for key, value in sorted(period.items())
                    }
                    for period in profile.period_values_inr()
                ],
                "objects_of_issue": profile.objects_of_issue,
                "objects_of_issue_page": profile.objects_of_issue_page,
                "peers": [
                    {
                        "company_key": peer.company_key,
                        "source_page": peer.source_page,
                        "metrics": {
                            str(getattr(metric, "value", metric)): str(value)
                            for metric, value in sorted(
                                peer.metrics.items(),
                                key=lambda item: str(
                                    getattr(item[0], "value", item[0])
                                ),
                            )
                        },
                    }
                    for peer in sorted(
                        profile.peers, key=lambda peer: peer.company_key
                    )
                ],
            }
            if profile is not None
            else None
        ),
        "ratios": ratio_facts,
        "subscription": (
            {
                "captured_at": subscription.captured_at.isoformat(),
                "qib": (
                    str(subscription.qib_multiple)
                    if subscription.qib_multiple is not None
                    else None
                ),
                "nii": (
                    str(subscription.nii_multiple)
                    if subscription.nii_multiple is not None
                    else None
                ),
                "retail": (
                    str(subscription.retail_multiple)
                    if subscription.retail_multiple is not None
                    else None
                ),
                "total": (
                    str(subscription.total_multiple)
                    if subscription.total_multiple is not None
                    else None
                ),
                "source_url": subscription.source_url,
                "source_confidence": subscription.source_confidence.value,
            }
            if subscription is not None
            else None
        ),
        "enrichment": enrichment_facts,
        "near_close": near_close,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def rescore_issue(
    issue_id: int,
    *,
    as_of: dt.datetime | None = None,
    session_factory: SessionFactory = session_scope,
) -> IpoRescoreOutcome:
    """Re-derive, re-flag, and (when inputs changed) re-score one issue.

    Args:
        issue_id: The issue to evaluate; a missing id raises
            ``IpoNotFoundError`` because the caller named a specific issue.
        as_of: Injected clock for the staleness/near-close rules; defaults to
            the current UTC instant.
        session_factory: Injectable transaction scope.

    Returns:
        An outcome whose status says whether a new evaluation was persisted,
        an identical one already existed, or the evidence was insufficient.

    Beginner note:
        No network happens here — every input is a repository read, so the
        dashboard's re-score button can safely call this inside a page action.
    """
    when = as_of if as_of is not None else dt.datetime.now(dt.UTC)
    inputs = load_ipo_factor_inputs_snapshot(
        issue_id,
        as_of=when,
        session_factory=session_factory,
    )
    issue = inputs.issue
    if inputs.profile is None:
        return IpoRescoreOutcome(
            issue_id=issue_id,
            company_name=issue.company_name,
            status="insufficient_inputs",
            missing=("manual_extraction",),
        )

    fingerprint = compute_inputs_fingerprint(inputs)

    latest = get_latest_evaluation(issue_id, session_factory=session_factory)
    if (
        latest is not None
        and latest.model_version == SCREENER_MODEL_VERSION
        and latest.inputs_fingerprint == fingerprint
    ):
        return IpoRescoreOutcome(
            issue_id=issue_id,
            company_name=issue.company_name,
            status="skipped_unchanged",
            evaluation=latest,
        )

    score_input = derive_score_input(inputs)
    caution_flags = evaluate_caution_flags(inputs)
    evaluation = evaluate_issue(
        issue_id,
        score_input,
        caution_flags=caution_flags,
        inputs_fingerprint=fingerprint,
        model_version=SCREENER_MODEL_VERSION,
        session_factory=session_factory,
    )
    log_event(
        logger,
        EVENT_IPO_ISSUE_SCORED,
        issue_id=issue_id,
        score=str(evaluation.result.score),
        recommendation=evaluation.result.recommendation.value,
        recommendation_type=evaluation.result.recommendation_type,
        triggered_flags=len(
            [flag for flag in evaluation.result.caution_flags if flag.status.value == "triggered"]
        ),
    )
    return IpoRescoreOutcome(
        issue_id=issue_id,
        company_name=issue.company_name,
        status="evaluated",
        evaluation=evaluation,
    )
