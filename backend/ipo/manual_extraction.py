"""Strict, framework-free contracts for IPO-004 manual prospectus entry.

Beginner note:
The Streamlit form is not trusted to validate financial data correctly by
itself. Browser widgets can be bypassed and future callers may not use a browser
at all, so every rule lives in these frozen domain objects. The objects retain
the value printed in the prospectus and its unit; convenience methods perform
exact ``Decimal`` conversion when a scoring or analysis caller needs canonical
rupees or shares.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import cast

from backend.ipo.models import IpoValidationError

_FOUR_PLACES = Decimal("0.0001")
_MAX_OBJECTS_LENGTH = 20_000


def _decimal(value: object, field_name: str, *, non_negative: bool = False) -> Decimal:
    """Return one finite four-place decimal and optionally reject negatives.

    Beginner note:
    Converting through ``str`` avoids importing binary floating-point rounding
    noise. Four decimal places cover per-share figures and percentages while
    remaining compatible with exact PostgreSQL ``NUMERIC`` columns.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IpoValidationError(f"{field_name} must be numeric.") from exc
    if not parsed.is_finite():
        raise IpoValidationError(f"{field_name} must be finite.")
    if non_negative and parsed < 0:
        raise IpoValidationError(f"{field_name} must be non-negative.")
    return parsed.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _page(value: object, field_name: str) -> int:
    """Return a positive source page without silently truncating decimals."""
    if isinstance(value, bool):
        raise IpoValidationError(f"{field_name} must be positive.")
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise IpoValidationError(f"{field_name} must be positive.") from exc
    if not numeric.is_finite() or numeric != parsed or parsed <= 0:
        raise IpoValidationError(f"{field_name} must be positive.")
    return parsed


def _parse_enum(value: object, enum_type: type[enum.StrEnum], field_name: str) -> enum.StrEnum:
    """Accept a typed enum or its stored string value and fail with a clear error."""
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise IpoValidationError(f"{field_name} must be one of: {allowed}.") from exc


class IpoAmountUnit(enum.StrEnum):
    """Reported monetary scales accepted from Indian offer documents."""

    INR = "inr"
    THOUSAND_INR = "thousand_inr"
    LAKH_INR = "lakh_inr"
    MILLION_INR = "million_inr"
    CRORE_INR = "crore_inr"

    def to_inr(self, value: Decimal) -> Decimal:
        """Convert a reported amount into exact rupees for downstream callers."""
        multiplier = {
            self.INR: Decimal("1"),
            self.THOUSAND_INR: Decimal("1000"),
            self.LAKH_INR: Decimal("100000"),
            self.MILLION_INR: Decimal("1000000"),
            self.CRORE_INR: Decimal("10000000"),
        }[self]
        return value * multiplier


class IpoShareUnit(enum.StrEnum):
    """Reported scales accepted for an issuer's equity-share count."""

    SHARES = "shares"
    THOUSAND_SHARES = "thousand_shares"
    LAKH_SHARES = "lakh_shares"
    MILLION_SHARES = "million_shares"
    CRORE_SHARES = "crore_shares"

    def to_shares(self, value: Decimal) -> Decimal:
        """Convert a reported count into individual shares without rounding."""
        multiplier = {
            self.SHARES: Decimal("1"),
            self.THOUSAND_SHARES: Decimal("1000"),
            self.LAKH_SHARES: Decimal("100000"),
            self.MILLION_SHARES: Decimal("1000000"),
            self.CRORE_SHARES: Decimal("10000000"),
        }[self]
        return value * multiplier


class IpoPeerMetric(enum.StrEnum):
    """Supported typed columns from a prospectus peer-comparison table."""

    EPS = "eps"
    PE = "pe"
    NAV_BOOK_VALUE = "nav_book_value"
    RONW = "ronw"
    EV_EBITDA = "ev_ebitda"
    PRICE_SALES = "price_sales"


def _company_key(value: str) -> str:
    """Create a stable peer key while retaining the entered display name.

    Beginner note:
    Two rows such as ``Example Ltd`` and ``example-limited`` describe the same
    peer. Normalizing Unicode, punctuation, whitespace, and common company
    suffixes lets the domain reject that accidental duplicate before storage.
    """
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    words = normalized.split()
    while words and words[-1] in {"ltd", "limited", "pvt", "private"}:
        words.pop()
    return " ".join(words)


@dataclass(frozen=True)
class IpoManualPeriodData:
    """Sourced income-statement values for one annual fiscal period.

    Beginner note:
    IPO-004 revisions contain revenue, EBITDA, and PAT only. IPO-005 adds PBT
    and finance cost so EBIT and interest coverage can be calculated without a
    web scrape or an accounting proxy. The new fields are nullable here solely
    so historical IPO-004 revisions remain readable after the migration.
    """

    period_end: dt.date
    revenue: Decimal
    revenue_page: int
    ebitda: Decimal
    ebitda_page: int
    pat: Decimal
    pat_page: int
    profit_before_tax: Decimal | None = None
    profit_before_tax_page: int | None = None
    finance_cost: Decimal | None = None
    finance_cost_page: int | None = None

    def __post_init__(self) -> None:
        """Normalize values and require a positive page for every supplied value.

        Beginner note:
            ``__post_init__`` runs immediately after the frozen dataclass is built.
            That makes a period valid-or-rejected at construction time, so no later
            service can accidentally persist a value without the page that proves it.
        """
        if not isinstance(self.period_end, dt.date):
            raise IpoValidationError("period_end must be a date.")
        object.__setattr__(self, "revenue", _decimal(self.revenue, "revenue", non_negative=True))
        object.__setattr__(self, "ebitda", _decimal(self.ebitda, "ebitda"))
        object.__setattr__(self, "pat", _decimal(self.pat, "pat"))
        for name in ("revenue_page", "ebitda_page", "pat_page"):
            object.__setattr__(self, name, _page(getattr(self, name), name))

        # A legacy period has none of the four IPO-005 values. A new period has
        # all four. Rejecting a partial group prevents a number from becoming
        # detached from the prospectus page that proves where it came from.
        ratio_fields = (
            self.profit_before_tax,
            self.profit_before_tax_page,
            self.finance_cost,
            self.finance_cost_page,
        )
        if all(value is None for value in ratio_fields):
            return
        if any(value is None for value in ratio_fields):
            raise IpoValidationError(
                "profit_before_tax and finance_cost require values and source pages together."
            )
        object.__setattr__(
            self,
            "profit_before_tax",
            _decimal(self.profit_before_tax, "profit_before_tax"),
        )
        object.__setattr__(
            self,
            "finance_cost",
            _decimal(self.finance_cost, "finance_cost", non_negative=True),
        )
        object.__setattr__(
            self,
            "profit_before_tax_page",
            _page(self.profit_before_tax_page, "profit_before_tax_page"),
        )
        object.__setattr__(
            self,
            "finance_cost_page",
            _page(self.finance_cost_page, "finance_cost_page"),
        )


@dataclass(frozen=True)
class IpoPeerValuationData:
    """One peer company with a flexible but allowlisted valuation metric map."""

    company_name: str
    source_page: int
    metrics: Mapping[IpoPeerMetric | str, Decimal]
    company_key: str = ""

    def __post_init__(self) -> None:
        """Normalize peer identity, page, metric names, and decimal values."""
        company_name = str(self.company_name).strip()
        if not company_name or len(company_name) > 255:
            raise IpoValidationError("peer company_name must contain 1 to 255 characters.")
        company_key = _company_key(company_name)
        if not company_key:
            raise IpoValidationError("peer company_name must identify a company.")
        if not isinstance(self.metrics, Mapping) or not self.metrics:
            raise IpoValidationError("A peer requires at least one supported metric.")

        normalized: dict[IpoPeerMetric, Decimal] = {}
        for raw_metric, raw_value in self.metrics.items():
            metric = cast(
                IpoPeerMetric,
                _parse_enum(raw_metric, IpoPeerMetric, "peer metric"),
            )
            if metric in normalized:
                raise IpoValidationError(f"Duplicate peer metric: {metric.value}.")
            normalized[metric] = _decimal(raw_value, f"peer {metric.value}")

        object.__setattr__(self, "company_name", company_name)
        object.__setattr__(self, "company_key", company_key)
        object.__setattr__(self, "source_page", _page(self.source_page, "source_page"))
        object.__setattr__(self, "metrics", MappingProxyType(normalized))


@dataclass(frozen=True)
class IpoManualExtractionData:
    """One complete immutable manual extraction submitted from a cached PDF.

    Beginner note:
    The payload intentionally has no ``entered_by`` or timestamp field. Those
    values come from the authenticated server-side caller and clock, preventing
    a browser from claiming that another administrator entered the evidence.
    """

    # Which cached document these numbers were read from. The repository re-checks it
    # against the parent issue before insert. Actor + timestamp are deliberately
    # absent (see the class docstring) so the browser can never supply them.
    source_document_id: int
    # The reported scales; every monetary/share value below is expressed in these and
    # converted to canonical units later, in the detached record.
    financial_amount_unit: IpoAmountUnit
    issue_amount_unit: IpoAmountUnit
    equity_share_unit: IpoShareUnit
    # Exactly three annual income-statement rows (revenue/EBITDA/PAT), each sourced.
    periods: tuple[IpoManualPeriodData, ...]
    # Balance-sheet and cash-flow singletons -- once-per-IPO facts -- each paired with
    # the prospectus page it was read from.
    net_worth: Decimal
    net_worth_page: int
    total_debt: Decimal
    total_debt_page: int
    cash: Decimal
    cash_page: int
    cash_flow_from_operations: Decimal
    cash_flow_from_operations_page: int
    equity_shares: Decimal
    equity_shares_page: int
    eps: Decimal
    eps_page: int
    nav_book_value: Decimal
    nav_book_value_page: int
    # Free-text "objects of the issue" (what the raised money will fund) plus its page.
    objects_of_issue: str
    objects_of_issue_page: int
    # Issue structure: fresh capital raised by the company vs offer-for-sale by
    # existing shareholders.
    fresh_issue_amount: Decimal
    fresh_issue_amount_page: int
    ofs_amount: Decimal
    ofs_amount_page: int
    # Promoter ownership before and after the issue (percentages), each with its page.
    promoter_holding_pre_issue: Decimal
    promoter_holding_pre_issue_page: int
    promoter_holding_post_issue: Decimal
    promoter_holding_post_issue_page: int
    # IPO-005 adds the raw balance-sheet/share facts needed for ROCE and
    # enterprise-value ratios. These belong to the immutable revision rather
    # than to a ratio table because they are sourced evidence, not calculations.
    total_assets: Decimal
    total_assets_page: int
    current_liabilities: Decimal
    current_liabilities_page: int
    post_issue_equity_shares: Decimal
    post_issue_equity_shares_page: int
    # One or more prospectus peer companies with allowlisted valuation ratios.
    peers: tuple[IpoPeerValuationData, ...]

    def __post_init__(self) -> None:
        """Enforce completeness, chronology, numeric ranges, and provenance.

        Beginner note:
        A frozen dataclass cannot use normal assignment in ``__post_init__`` (the
        instance is read-only), so validated values are written back through
        ``object.__setattr__``. That is why every normalization below uses it.
        """
        # ``bool`` is a subclass of ``int`` in Python, so ``True`` would otherwise pass
        # the ``<= 0`` test and act like the id ``1``. Reject it explicitly.
        if isinstance(self.source_document_id, bool) or self.source_document_id <= 0:
            raise IpoValidationError("source_document_id must be positive.")
        financial_unit = _parse_enum(
            self.financial_amount_unit, IpoAmountUnit, "financial_amount_unit"
        )
        issue_unit = _parse_enum(self.issue_amount_unit, IpoAmountUnit, "issue_amount_unit")
        share_unit = _parse_enum(self.equity_share_unit, IpoShareUnit, "equity_share_unit")
        object.__setattr__(self, "financial_amount_unit", financial_unit)
        object.__setattr__(self, "issue_amount_unit", issue_unit)
        object.__setattr__(self, "equity_share_unit", share_unit)

        periods = tuple(self.periods)
        if len(periods) != 3:
            raise IpoValidationError("Manual extraction requires exactly three fiscal periods.")
        if not all(isinstance(period, IpoManualPeriodData) for period in periods):
            raise IpoValidationError("periods must contain IpoManualPeriodData values.")
        # Sort by date so the stored order is always oldest-to-newest regardless of the
        # order the form happened to submit, then confirm the three dates are distinct.
        periods = tuple(sorted(periods, key=lambda period: period.period_end))
        if len({period.period_end for period in periods}) != 3:
            raise IpoValidationError("Fiscal periods require distinct period_end dates.")
        years = [period.period_end.year for period in periods]
        if years != list(range(years[0], years[0] + 3)):
            raise IpoValidationError("Fiscal periods must cover three consecutive annual years.")
        if any(
            period.profit_before_tax is None or period.finance_cost is None
            for period in periods
        ):
            raise IpoValidationError(
                "Every fiscal period requires profit_before_tax and finance_cost with source pages."
            )
        object.__setattr__(self, "periods", periods)

        # Negative profit, EBITDA, cash flow, net worth, EPS, or NAV can be
        # truthful evidence. Revenue, debt, cash, issue size, and share count
        # cannot be negative under this contract.
        numeric_rules = {
            "net_worth": False,
            "total_debt": True,
            "cash": True,
            "cash_flow_from_operations": False,
            "equity_shares": True,
            "eps": False,
            "nav_book_value": False,
            "fresh_issue_amount": True,
            "ofs_amount": True,
            "promoter_holding_pre_issue": True,
            "promoter_holding_post_issue": True,
            "total_assets": True,
            "current_liabilities": True,
            "post_issue_equity_shares": True,
        }
        for name, non_negative in numeric_rules.items():
            if getattr(self, name) is None:
                raise IpoValidationError(f"{name} is required.")
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), name, non_negative=non_negative),
            )
        if self.equity_shares <= 0:
            raise IpoValidationError("equity_shares must be positive.")
        if self.post_issue_equity_shares <= 0:
            raise IpoValidationError("post_issue_equity_shares must be positive.")
        for name in ("promoter_holding_pre_issue", "promoter_holding_post_issue"):
            if getattr(self, name) > 100:
                raise IpoValidationError(f"{name} must be from 0 to 100.")

        page_names = (
            "net_worth_page",
            "total_debt_page",
            "cash_page",
            "cash_flow_from_operations_page",
            "equity_shares_page",
            "eps_page",
            "nav_book_value_page",
            "objects_of_issue_page",
            "fresh_issue_amount_page",
            "ofs_amount_page",
            "promoter_holding_pre_issue_page",
            "promoter_holding_post_issue_page",
            "total_assets_page",
            "current_liabilities_page",
            "post_issue_equity_shares_page",
        )
        for name in page_names:
            object.__setattr__(self, name, _page(getattr(self, name), name))

        objects = str(self.objects_of_issue).strip()
        if not objects:
            raise IpoValidationError("objects_of_issue is required.")
        if len(objects) > _MAX_OBJECTS_LENGTH:
            raise IpoValidationError(
                f"objects_of_issue must not exceed {_MAX_OBJECTS_LENGTH} characters."
            )
        object.__setattr__(self, "objects_of_issue", objects)

        peers = tuple(self.peers)
        if not peers:
            raise IpoValidationError("Manual extraction requires at least one peer.")
        if not all(isinstance(peer, IpoPeerValuationData) for peer in peers):
            raise IpoValidationError("peers must contain IpoPeerValuationData values.")
        if len({peer.company_key for peer in peers}) != len(peers):
            raise IpoValidationError("Normalized peer companies must be unique.")
        object.__setattr__(self, "peers", peers)


@dataclass(frozen=True)
class IpoManualExtractionRecord:
    """Detached immutable revision returned after its database session closes.

    Beginner note:
    The record retains reported values because they are the audit evidence. The
    properties below expose canonical units without overwriting that evidence,
    giving a future factor-derivation service an exact and convenient input.
    """

    # Database identity and the frozen source snapshot (the FK may later become NULL,
    # but the copied URL/hashes keep the evidence self-describing).
    id: int
    issue_id: int
    source_document_id: int | None
    source_document_url: str
    source_record_hash: str | None
    source_content_sha256: str
    # From here down this mirrors IpoManualExtractionData's reported values (kept in
    # their original units as audit evidence) and adds the server-supplied actor and
    # timestamp at the end. Canonical-unit access is via the properties below.
    financial_amount_unit: IpoAmountUnit
    issue_amount_unit: IpoAmountUnit
    equity_share_unit: IpoShareUnit
    periods: tuple[IpoManualPeriodData, ...]
    net_worth: Decimal
    net_worth_page: int
    total_debt: Decimal
    total_debt_page: int
    cash: Decimal
    cash_page: int
    cash_flow_from_operations: Decimal
    cash_flow_from_operations_page: int
    equity_shares: Decimal
    equity_shares_page: int
    eps: Decimal
    eps_page: int
    nav_book_value: Decimal
    nav_book_value_page: int
    objects_of_issue: str
    objects_of_issue_page: int
    fresh_issue_amount: Decimal
    fresh_issue_amount_page: int
    ofs_amount: Decimal
    ofs_amount_page: int
    promoter_holding_pre_issue: Decimal
    promoter_holding_pre_issue_page: int
    promoter_holding_post_issue: Decimal
    promoter_holding_post_issue_page: int
    peers: tuple[IpoPeerValuationData, ...]
    entered_by_email: str
    submitted_at: dt.datetime
    # IPO-005 facts are optional only for detached legacy IPO-004 revisions.
    # Every new submission requires the complete six-value/page group.
    total_assets: Decimal | None = None
    total_assets_page: int | None = None
    current_liabilities: Decimal | None = None
    current_liabilities_page: int | None = None
    post_issue_equity_shares: Decimal | None = None
    post_issue_equity_shares_page: int | None = None

    @property
    def net_worth_inr(self) -> Decimal:
        """Return reported net worth converted into individual rupees."""
        return self.financial_amount_unit.to_inr(self.net_worth)

    @property
    def equity_shares_canonical(self) -> Decimal:
        """Return the reported share count converted into individual shares."""
        return self.equity_share_unit.to_shares(self.equity_shares)

    @property
    def canonical_values(self) -> Mapping[str, Decimal]:
        """Expose every singleton numeric fact in canonical downstream units.

        Monetary statement values use ``financial_amount_unit``; fresh/OFS use
        their separately reported issue unit. Per-share and percentage values
        are already canonical and therefore pass through unchanged.

        Beginner note:
            The record keeps prospectus-scale values for auditability, but formulas
            need one common scale. This property returns a read-only view in INR and
            individual shares without overwriting the original reported evidence.
        """
        values = {
                "net_worth_inr": self.financial_amount_unit.to_inr(self.net_worth),
                "total_debt_inr": self.financial_amount_unit.to_inr(self.total_debt),
                "cash_inr": self.financial_amount_unit.to_inr(self.cash),
                "cash_flow_from_operations_inr": self.financial_amount_unit.to_inr(
                    self.cash_flow_from_operations
                ),
                "equity_shares": self.equity_share_unit.to_shares(self.equity_shares),
                "eps_inr_per_share": self.eps,
                "nav_book_value_inr_per_share": self.nav_book_value,
                "fresh_issue_amount_inr": self.issue_amount_unit.to_inr(
                    self.fresh_issue_amount
                ),
                "ofs_amount_inr": self.issue_amount_unit.to_inr(self.ofs_amount),
                "promoter_holding_pre_issue_pct": self.promoter_holding_pre_issue,
                "promoter_holding_post_issue_pct": self.promoter_holding_post_issue,
        }
        # Legacy IPO-004 revisions do not have these values. Omitting keys is
        # safer than exposing a fabricated zero that would look like real evidence.
        if (
            self.total_assets is not None
            and self.current_liabilities is not None
            and self.post_issue_equity_shares is not None
        ):
            values.update(
                {
                    "total_assets_inr": self.financial_amount_unit.to_inr(
                        self.total_assets
                    ),
                    "current_liabilities_inr": self.financial_amount_unit.to_inr(
                        self.current_liabilities
                    ),
                    "post_issue_equity_shares": self.equity_share_unit.to_shares(
                        self.post_issue_equity_shares
                    ),
                }
            )
        return MappingProxyType(values)

    def period_values_inr(self) -> tuple[dict[str, Decimal | dt.date], ...]:
        """Return every available annual fact converted to individual INR.

        Beginner note:
        Legacy IPO-004 periods omit PBT and finance cost rather than returning
        zero. That distinction lets downstream callers tell "not entered yet"
        from a genuine zero finance cost without inspecting database columns.
        """
        rows: list[dict[str, Decimal | dt.date]] = []
        for period in self.periods:
            row: dict[str, Decimal | dt.date] = {
                "period_end": period.period_end,
                "revenue_inr": self.financial_amount_unit.to_inr(period.revenue),
                "ebitda_inr": self.financial_amount_unit.to_inr(period.ebitda),
                "pat_inr": self.financial_amount_unit.to_inr(period.pat),
            }
            if period.profit_before_tax is not None and period.finance_cost is not None:
                row.update(
                    {
                        "profit_before_tax_inr": self.financial_amount_unit.to_inr(
                            period.profit_before_tax
                        ),
                        "finance_cost_inr": self.financial_amount_unit.to_inr(
                            period.finance_cost
                        ),
                    }
                )
            rows.append(row)
        return tuple(rows)
