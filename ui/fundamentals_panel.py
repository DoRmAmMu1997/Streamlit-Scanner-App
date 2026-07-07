"""Check Fundamentals panel — eligibility, agent caching, rendering (REF-002).

Extracted verbatim from app.py, continuing REF-001's direction. The
fundamental-analysis agent runs for ANY shortlisted symbol; eligibility only
selects criteria (9) vs universal (7) mode. Two helpers below build that
eligibility set, and a third lazily instantiates the Claude Agent SDK agent.
None of the agent code runs unless the user actually clicks the
"Check Fundamentals" button.

Beginner note: like every ui/ module, this file reads Streamlit through its
own module global, so tests monkeypatch ``ui.fundamentals_panel.st`` — the
module that actually renders. app.py re-exports these helpers so existing
callers and tests keep the ``app.<name>`` access path (REF-001 convention).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

import pandas as pd
import streamlit as st

from backend.config import get_agent_fast_mode, get_fundamentals_model
from backend.fundamentals.fundamental_agent import (
    AgentVerdict,
    FundamentalAgent,
    FundamentalsUsageLimitError,
)
from backend.universe_loader import load_universe
from ui.common import _redact_secrets

logger = logging.getLogger(__name__)


_FUNDAMENTALS_UNIVERSES: tuple[str, ...] = ("hemant_super_45", "nifty_100")


@st.cache_data(ttl=600)
def _eligible_symbols_set(universe_keys: tuple[str, ...]) -> frozenset[str]:
    """Return the uppercase symbol set across the given universe keys.

    Cached for 10 minutes because universe CSVs are refreshed at most once
    per CLI prefetch run — re-reading on every Streamlit rerun is wasteful.
    """
    symbols: set[str] = set()
    for key in universe_keys:
        try:
            df = load_universe(key)
        except Exception:
            # A missing universe CSV must not break the rest of the UI.
            logger.warning("Could not load universe %s for fundamentals eligibility", key)
            continue
        if "symbol" not in df.columns:
            continue
        for symbol in df["symbol"].astype(str):
            cleaned = symbol.strip().upper()
            if cleaned:
                symbols.add(cleaned)
    return frozenset(symbols)


def _is_eligible_for_fundamentals(symbol: str | None) -> bool:
    """True when `symbol` belongs to Hemant Super 45 OR Nifty 100."""
    if not symbol:
        return False
    return str(symbol).strip().upper() in _eligible_symbols_set(_FUNDAMENTALS_UNIVERSES)


@st.cache_resource(show_spinner=False)
def _get_fundamental_agent(model: str, fast_mode: bool) -> FundamentalAgent:
    """Memoize one agent per (model, fast_mode) across reruns.

    The Claude Agent SDK authenticates via your Claude subscription, so there
    is no API key argument. `cache_resource` keys on the arguments, so switching
    the model OR toggling fast mode rebuilds the agent (and its on-disk cache
    handle) automatically.
    """
    return FundamentalAgent(model=model, fast_mode=fast_mode)


def _render_fundamentals_panel(symbol: str | None) -> None:
    """Render the per-stock Check Fundamentals section under the chart.

    The button is now visible for ANY selected symbol — eligibility just
    determines how many criteria the agent applies:
      - Hemant Super 45 ∪ Nifty 100 symbols → criteria mode (all NINE criteria
        + observations + outlook + rating).
      - Anything else → universal mode (the SEVEN universal criteria, skipping
        Business Age and Market Leader, + observations + outlook + rating).

    Stays hidden only when no symbol is selected.
    """
    if not symbol:
        return

    # Mode is symbol-deterministic: HS45/N100 → criteria (9), everything else
    # → universal (7). The button label and behavior adapt accordingly.
    mode: Literal["criteria", "universal"] = (
        "criteria" if _is_eligible_for_fundamentals(symbol) else "universal"
    )

    st.divider()
    st.subheader("Fundamentals")
    if mode == "criteria":
        st.caption(
            "AI agent applies all nine user-defined criteria, adds its own "
            "expert observations, and produces a holistic 0–10 rating."
        )
    else:
        st.caption(
            f"**Universal mode** — `{symbol}` is outside Hemant Super 45 / "
            "Nifty 100, so the two context-heavy criteria (Business Age, Market "
            "Leader) are skipped. The agent still applies the other seven "
            "criteria plus a holistic rating, observations, and forward outlook."
        )

    model = get_fundamentals_model()

    # Session-state cache key is now mode-qualified so a criteria-mode and a
    # universal-mode verdict for the same symbol cannot collide.
    session_key = f"fundamentals_verdict::{symbol}::{model}::{mode}"
    cached_verdict_dict = st.session_state.get(session_key)

    button_col, rerun_col, _spacer = st.columns([2, 1, 2])
    primary_label = (
        f"View cached verdict: {symbol}"
        if cached_verdict_dict is not None
        else f"Check Fundamentals: {symbol}"
    )
    run_now = button_col.button(
        primary_label,
        type="primary",
        key=f"check_fund_btn::{symbol}::{model}::{mode}",
        disabled=cached_verdict_dict is not None,
    )
    rerun_now = False
    if cached_verdict_dict is not None:
        rerun_now = rerun_col.button(
            "Re-run analysis",
            key=f"rerun_fund_btn::{symbol}::{model}::{mode}",
            help="Bypass the cache and re-fetch screener.in + re-query the LLM.",
        )
    if cached_verdict_dict is not None and not rerun_now:
        # UI-002: make the staleness provenance explicit up front — the verdict
        # below is served from this browser session, and its "Data fetched"
        # caption (bottom of the block) is the age that matters.
        st.caption(
            "Showing a verdict cached in this session — see \"Data fetched\" below "
            "for its age; \"Re-run analysis\" refreshes screener.in data and the model's view."
        )

    if run_now or rerun_now:
        try:
            agent = _get_fundamental_agent(model, get_agent_fast_mode())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not build FundamentalAgent")
            st.error(f"Could not build FundamentalAgent: {_redact_secrets(str(exc))}")
            return

        with st.spinner(f"Senior analyst evaluating **{symbol}** — this can take 20–60s..."):
            try:
                verdict = agent.check(symbol, force_refresh=rerun_now, mode=mode)
            except FundamentalsUsageLimitError as exc:
                # Expected operational state (plan limit hit) — show a gentle
                # notice, not a red error, and keep cached verdicts usable.
                logger.warning("Fundamentals usage limit reached for %s: %s", symbol, exc)
                st.warning(f"⏳ {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Fundamental agent failed for %s", symbol)
                st.error(f"Fundamental check failed: {_redact_secrets(str(exc))}")
                return
        # Persist verdict as plain dict so it survives reruns even after
        # the Pydantic class changes shape.
        st.session_state[session_key] = verdict.model_dump(mode="json")
        cached_verdict_dict = st.session_state[session_key]

    if cached_verdict_dict is None:
        return

    try:
        verdict = AgentVerdict.model_validate(cached_verdict_dict)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cached verdict for %s is invalid; clearing", symbol, exc_info=True)
        st.session_state.pop(session_key, None)
        st.error(f"Cached verdict could not be parsed: {exc}")
        return

    _render_verdict_block(verdict)


def _render_verdict_block(verdict: AgentVerdict) -> None:
    """Render the rating metric + criteria table + observations + summary.

    Both modes now run a criteria checklist, so the rating, the
    "criteria passed X/Y" metric, and the breakdown table appear for every
    stock. The only difference is the count: 9 criteria in 'criteria' mode
    (curated universe) vs 7 in 'universal' mode (Business Age and Market
    Leader skipped).
    """
    # Headline numbers: rating, criteria-passed (X / Y), and the model.
    metric_cols = st.columns([1, 1, 2])
    metric_cols[0].metric(
        "Fundamental rating",
        f"{verdict.rating}/10",
        help="Holistic expert judgment — NOT a count of passed criteria.",
    )
    metric_cols[1].metric(
        "Criteria passed",
        f"{verdict.passed_criteria_count} / {verdict.total_criteria}",
    )
    metric_cols[2].metric(
        "Model",
        verdict.model_used.split("/")[-1] if "/" in verdict.model_used else verdict.model_used,
    )

    # Criteria breakdown table (shown whenever the agent returned rows).
    breakdown_rows = [
        {
            "Criterion": criterion.name,
            "Pass": "✅" if criterion.passed else "❌",
            "Measured": criterion.measured_value,
            "Threshold": criterion.threshold,
            "Reasoning": criterion.reasoning,
        }
        for criterion in verdict.criteria_breakdown
    ]
    if breakdown_rows:
        st.markdown("**Criteria breakdown**")
        st.dataframe(
            pd.DataFrame(breakdown_rows),
            width="stretch",
            hide_index=True,
        )

    # Additional agent-chosen observations, grouped by sentiment
    if verdict.additional_observations:
        st.markdown("**Additional observations (beyond the criteria)**")
        sentiment_order = {"positive": 0, "negative": 1, "neutral": 2}
        sentiment_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
        sorted_observations = sorted(
            verdict.additional_observations,
            key=lambda obs: sentiment_order.get(obs.sentiment, 3),
        )
        for observation in sorted_observations:
            icon = sentiment_icon.get(observation.sentiment, "•")
            st.markdown(
                f"- {icon} **{observation.topic}** — {observation.finding}  \n"
                f"  _Evidence:_ {observation.evidence}"
            )

    # Forward outlook (analyst view). Distinct from the criterion-(e) pass/fail —
    # this is the agent's free-form view on the company's next 1–4 quarters,
    # broken into three labelled subsections by source: announcements first,
    # concall second, overall integrated summary third. Subsections that came
    # back empty (e.g. no concall transcript was read) are hidden so the UI
    # never shows an empty bullet.
    outlook = getattr(verdict, "forward_outlook", None)
    if outlook is not None and any(
        section.strip()
        for section in (
            outlook.announcements_conclusion,
            outlook.concall_conclusion,
            outlook.overall_summary,
        )
    ):
        st.markdown("**Forward outlook (analyst view)**")
        if outlook.announcements_conclusion.strip():
            st.markdown(
                f"- **Conclusion from Announcements:** {outlook.announcements_conclusion}"
            )
        if outlook.concall_conclusion.strip():
            st.markdown(
                f"- **Conclusion from the latest Concall:** {outlook.concall_conclusion}"
            )
        if outlook.overall_summary.strip():
            st.markdown(
                f"- **Overall summary:** {outlook.overall_summary}"
            )

    # Summary callout
    st.markdown("**Summary**")
    st.info(verdict.summary_comments)
    st.caption(
        f"Data fetched: {_format_data_freshness(verdict.data_freshness)} · "
        f"Model: `{verdict.model_used}`"
    )


def _format_data_freshness(value: str) -> str:
    """Humanize the verdict's ISO ``data_freshness`` for the caption (UI-002).

    Beginner note: the raw value is a machine timestamp like
    ``2026-07-06T08:15:23.123456+00:00`` — accurate but unreadable at a glance,
    which defeats the caption's job of answering "how stale is this verdict?".
    Zone-aware values are normalized to UTC so the label is honest; a naive
    value is shown without the UTC suffix rather than mislabeled; anything
    unparseable renders verbatim (backticked, like the old caption) so a
    surprising upstream format degrades the caption, not the verdict block.
    """
    text = str(value or "").strip()
    if not text:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return f"`{text}`"
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
    return parsed.strftime("%d %b %Y, %H:%M")
