# IPO-007 - Read-only IPO screener dashboard

## Decision

The dashboard follows the validation-page split exactly: a Streamlit-free
builder (`backend/ipo/dashboard.py`) assembles everything from repository
reads, and a thin page (`ui/ipo_page.py`) renders whatever the builder
returned. No network call and no scoring happen during render; the compute
pass is the IPO-008 job or the page's explicit re-score action.

`build_dashboard_snapshot` denormalizes each issue's stored state — latest
evaluation (with its contribution receipt), manual-profile presence, cached
document counts, pending proposal count — into frozen `IpoDashboardRow`
values. Pure selectors implement the seven spec sections: Available filings,
Open IPOs, Upcoming IPOs (RHP stage), DRHP watchlist, Recommended, Not
Recommended, and the Missing data queue (no verified profile, no downloaded
prospectus, a factor the verdict flagged missing, or a proposal awaiting
review). `top_positive_and_risk_reasons` ranks stored contributions against
`PDF_WEIGHTS` (>=75% of weight is a headline strength, <=35% a headline
risk); missing factors are excluded because "could not check" and "checked
and weak" are deliberately different messages.

## Page behavior

- Every authenticated user sees the "IPO screener" view (same tier as the
  validation page); rows carry company, issue status, score, recommendation,
  confidence, top positives, top risks, missing data, source documents, and
  last-updated, plus proposal/document progress.
- The four stored `recommendation_type` strings map onto the sprint's
  friendly labels purely in the UI ("Recommended - high conviction",
  "Recommended - selective / listing-gain oriented", "Not Recommended",
  "Not Recommended - insufficient verified data"); a test pins the map's
  completeness against the DB vocabulary.
- A binary verdict filter (All / Recommended / Not Recommended) narrows
  every section; unscored issues appear only under All.
- Per-issue score-breakdown expanders show the full receipt: every reason
  string (with its provenance suffix), triggered hard flags, missing data,
  and source documents.
- The **Re-score all issues** button renders only with `MANAGE_IPO_DATA`
  (hiding is UX; the app dispatch capability check is the boundary). It runs
  the same `rescore_issue` service the job uses — repository work only —
  counts outcomes without letting one failure abort the rest, records an
  audit event, and invalidates the five-minute snapshot cache.

## Testing

`tests/test_ipo_dashboard_builder.py` pins section membership, the
missing-data queue rules, strength/risk selection, and snapshot
denormalization over monkeypatched repositories.
`tests/test_app_ipo_page.py` smoke-tests the renderer against a fake ``st``
with every repository seam stubbed (proving render purity), plus the label
map, filter semantics, spec column contract, and the re-score audit/cache
path. `tests/test_app_orchestration.py` pins the navigation entry, the
re-export identity, and the keyword-only capability boundary.
