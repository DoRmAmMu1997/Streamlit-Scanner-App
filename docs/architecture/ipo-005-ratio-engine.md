# IPO-005 - Deterministic ratio engine

## Decision

IPO-005 derives sixteen general-company ratios from the newest immutable manual
extraction. It performs no scraping, PDF parsing, sector override, factor scoring,
or recommendation. The public result is a frozen receipt: every ratio has either
an exact `Decimal` value or an explicit reason why no honest value exists.

The engine lives in `backend/ipo/financials/ratio_engine.py`. The repository reads
the issue and newest extraction within one short transaction, detaches both rows,
and runs the pure calculation after the session closes. Ratios are not persisted;
the immutable source revision plus issue-price snapshot makes them replayable.

## Source additions

IPO-004 already stored revenue, EBITDA, PAT, net worth, debt, cash, CFO, a general
share count, reported EPS/NAV, and page provenance. IPO-005 adds the raw facts that
were missing for standard calculations:

- each annual period: profit before tax and finance cost, each with its source page;
- each revision: total assets, current liabilities, and post-issue shares, each with
  its source page.

PBT plus finance cost derives EBIT. Total assets minus current liabilities derives
capital employed. A distinct post-issue share count prevents historical EPS shares
from being silently reused for IPO market capitalization.

Migration `20260703ipo005` leaves the additions nullable so IPO-004 history remains
readable. All-null is the only legacy shape; grouped database checks reject partial
value/page groups. New typed submissions require every new fact. Downgrade refuses
while any IPO-005 value exists, but a database containing only legacy rows can
return losslessly to `20260701ipo004`.

## Formula contract

All inputs are converted to individual INR/shares, calculated with `Decimal`, and
rounded half-up to four places only at the public receipt boundary.

| Ratio | Formula |
|---|---|
| Revenue CAGR | `((FY3 revenue / FY1 revenue) ^ (1/2) - 1) * 100` |
| PAT CAGR | `((FY3 PAT / FY1 PAT) ^ (1/2) - 1) * 100` |
| EBITDA margin | `FY3 EBITDA / FY3 revenue * 100` |
| PAT margin | `FY3 PAT / FY3 revenue * 100` |
| ROE | `FY3 PAT / closing net worth * 100` |
| ROCE | `(FY3 PBT + FY3 finance cost) / (total assets - current liabilities) * 100` |
| Debt/equity | `total debt / closing net worth` |
| Net debt/EBITDA | `(total debt - cash) / FY3 EBITDA` |
| Interest coverage | `(FY3 PBT + FY3 finance cost) / FY3 finance cost` |
| CFO/PAT | `cash flow from operations / FY3 PAT` |
| EPS | `FY3 PAT / sourced equity shares` |
| Book value/share | `closing net worth / sourced equity shares` |
| P/E | `upper price band / computed EPS` |
| P/B | `upper price band / computed book value/share` |
| EV/EBITDA | `(upper price band * post-issue shares + debt - cash) / FY3 EBITDA` |
| EV/Sales | `(upper price band * post-issue shares + debt - cash) / FY3 revenue` |

Fresh-issue proceeds and intended debt repayment are not forecast into cash/debt.
Those plans are narrative evidence, not completed balance-sheet events.

## Missing and exceptional values

`computed` means a rounded value exists. `missing_inputs` identifies absent legacy
facts or a missing upper price band. `undefined` covers a zero denominator.
`not_meaningful` covers a mathematically possible but misleading presentation,
such as negative equity, non-positive EBITDA multiples, or P/E on a loss.
`not_applicable` currently describes zero finance cost: coverage is not reported as
infinity.

Negative PAT still produces signed PAT margin, ROE, ROCE, CFO/PAT, and EPS. PAT CAGR
and P/E are suppressed. Net cash may produce a negative net-debt/EBITDA ratio. One
unavailable receipt never suppresses unrelated ratios.

Computed EPS and book value are reconciled against the prospectus-reported values.
A difference is material only when it exceeds the greater of INR 0.01 or one percent
of the reported value. Reconciliation is diagnostic; it does not reject evidence or
change an investment verdict.

## Security and boundaries

The ratio package imports no network, Streamlit, SQLAlchemy, or AI client. Inputs are
bounded by the manual-extraction DTO and `Numeric(24,4)` storage. Static explanations
contain no submitted narrative, paths, URLs, or financial values, so callers can log
statuses without leaking source content. Existing admin authorization, authenticated
actor attribution, verified document hash, immutable revisions, and redacted audit
events remain the source-trust boundary.

Sector-specific definitions for banks/NBFCs, AMCs, insurers, and loss-making
technology companies are intentionally deferred. The v1 result must not be treated
as an appropriate sector model for those issuers.
