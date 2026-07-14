# IPO-006 - Factor derivation, hard caution flags, and the extended verdict

## Decision

IPO-006 builds the "middle layer" the IPO-001 design deferred: a pure,
deterministic mapping from typed evidence (IPO-005 ratio receipts, the
human-verified manual extraction, the newest official subscription snapshot,
and optional IPO-009 web signals) into the seven 0-100 `FactorAssessment`
values the scorecard consumes, plus a fixed catalog of seven hard caution
flags that can force `Not Recommended` regardless of the numeric score.

The scorecard and verdict modules moved into a package —
`backend/ipo/scoring/score_model.py` and `scoring/recommendation.py`
(history-preserving renames of `scorecard.py`/`verdict.py`) — joined by
`scoring/factor_derivation.py`, `scoring/caution_flags.py`, and the
orchestration-facing `scoring/service.py` (documented in IPO-008). The
`backend.ipo` facade keeps every public name importable exactly as before.

## The None-versus-zero rule

A factor score of `None` means the evidence needed to judge is absent (no
profile, ratio receipt `missing_inputs`, no subscription row, no usable GMP
signal) and feeds the fail-closed verdict path. A score of `0` means the
evidence exists and is bad (negative CAGR, undersubscribed book, grey-market
discount). Ratio statuses map per ratio: `computed` is banded, `undefined`
is usually known-weak zero (a loss-base CAGR carries the engine's own
explanation into the reason string), and everything else leaves the
sub-input unavailable.

## v1 band tables (FACTOR_MODEL_VERSION = "ipo-006-factors-v1")

Bands are half-open (`lower <= x < upper`) `Decimal` module constants; a
factor is the half-up-rounded mean of its available sub-scores. Core
sub-inputs must all be available or the factor is `None`; optional
sub-inputs join the mean only when computed.

| Factor (weight) | Core sub-inputs | Optional | Bands (value -> sub-score) |
|---|---|---|---|
| Financial growth (20) | revenue CAGR, PAT CAGR | - | >=25% 100, 15-25 75, 8-15 50, 0-8 25, <0 0 |
| Return ratios (15) | ROE | ROCE | >=20% 100, 15-20 75, 10-15 50, 5-10 25, <5 0 |
| Valuation (15) | P/E vs peer P/E median | EV/EBITDA vs peer median | premium <0.8x 100, 0.8-1.0 80, 1.0-1.2 60, 1.2-1.5 35, >=1.5 10 |
| Business quality (25) | EBITDA margin, PAT margin, CFO/PAT | interest coverage | per-metric tables in `factor_derivation.py` |
| Promoter quality (10) | post-issue holding %, OFS share of issue | - | holding >=60 100 ... <30 20; OFS 0 100 ... pure OFS 0 |
| QIB subscription (10) | latest QIB multiple | - | >=50x 100, 20-50 85, 10-20 70, 3-10 55, 1-3 35, <1 0 |
| GMP sentiment (5) | median parsed GMP % (last 5 days, clean signals only) | - | >=40 100, 20-40 75, 10-20 60, 0-10 40, <0 0 |

Every factor's reason string names each sub-score, the band it hit, and its
provenance (ratio-engine formula version, extraction id, source SHA-256
prefix). Missing factors carry an explanatory reason too, so the dashboard's
missing-data queue needs no reconstruction. Any threshold change must bump
`FACTOR_MODEL_VERSION` so stored evaluations stay attributable.

## Hard caution flags (CAUTION_FLAGS_VERSION = "ipo-006-flags-v1")

`evaluate_caution_flags` returns all seven flags in fixed catalog order, each
`triggered`, `not_triggered`, or `not_evaluable` (required evidence absent —
reported honestly, never guessed):

1. `entirely_ofs_weak_growth` — zero fresh issue and revenue CAGR <8% or undefined.
2. `very_expensive_valuation` — P/E premium >1.5x the positive peer median.
3. `weak_qib_demand_near_close` — inside the close-date-minus-1-day window while
   open/closed: QIB book <1x, or no snapshot at all.
4. `negative_operating_cash_flow_despite_profits` — CFO <0 while latest PAT >0.
5. `high_debt_without_debt_reduction_use` — D/E >1.5 or net debt/EBITDA >3 and the
   objects of issue contain no repayment/deleveraging language.
6. `litigation_or_auditor_red_flag` — non-quarantined IPO-009 litigation signals
   with recorded keyword matches (keywords only; snippet text never reaches here).
7. `loss_making_no_credible_path` — latest year is a loss that is not narrowing.

## Verdict precedence and the fourth type

`build_recommendation(score_result, *, caution_flags=None)` decides in order:
(1) any missing critical factor -> `Not Recommended` with the new
`recommendation_type` **"Insufficient verified data"** (migration
`20260713ipo006` widened the CHECK; existing history rows stay valid);
(2) any triggered flag -> `Not Recommended` / `Skip`, with each flag's
evidence prepended to the reasons; (3) otherwise the unchanged >=80 / >=65
score bands. The recommendation stays strictly binary; the four types are
sub-labels, and the dashboard maps them to friendlier wording purely in the
UI. The full flag report is persisted in `caution_flags_json` and serialized
by `IpoRecommendationResult.to_dict()`.

## Testing

`tests/test_ipo_factor_derivation.py` pins every band boundary and the
None-versus-zero table; `tests/test_ipo_caution_flags.py` pins each flag's
three outcomes; `tests/test_ipo_verdict.py` pins precedence (a triggered flag
overrides a 95-point score; missing-critical outranks flags).
