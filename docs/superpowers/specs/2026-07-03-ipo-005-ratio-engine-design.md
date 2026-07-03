# IPO-005 ratio-engine design

The authoritative implementation decision is documented in
[`docs/architecture/ipo-005-ratio-engine.md`](../../architecture/ipo-005-ratio-engine.md).

IPO-005 extends immutable manual evidence with sourced PBT, finance cost, total
assets, current liabilities, and post-issue shares. A pure Decimal engine calculates
sixteen general-company ratios on demand and returns typed diagnostic receipts. It
does not persist ratios or feed IPO-001 scoring automatically.

The selected design rejects two tempting shortcuts: EBITDA is not used as an EBIT
proxy, and the historical share count is not reused for post-issue market value.
Legacy IPO-004 revisions remain readable and degrade through explicit missing-input
statuses rather than zero-filled data.
