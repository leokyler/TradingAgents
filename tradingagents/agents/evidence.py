"""Evidence gate: a run without price data is REVIEW, not a tradeable rating.

The pipeline always reaches the Portfolio Manager, and its prompt asks for a
committed rating even when the reports are thin. When every price vendor fails,
the analysts still write prose around the data layer's ``NO_DATA_AVAILABLE``
sentinel, the debate still argues, and the manager still returns Buy/Hold/Sell —
an absence of evidence presented as a decision.

The gate is deliberately narrow: it asks the data layer one question after the
graph finishes — can this instrument be priced on this date? — and marks the run
``REVIEW`` when the answer is no. Technical evidence exists whenever prices
exist, so a run with prices but no news or fundamentals is still a call (a
technical-only run is a supported mode). Without prices, every rating is
invention, whatever the reports say.

Two consequences are deliberate, not oversights:

* The probe is a *second* read of the same series, so it can disagree with what
  the run itself saw minutes earlier. The gate takes the later answer: a run
  whose prices are unreachable now is the one a reader would re-run anyway.
* The router reports an exhausted chain as a ``DATA_UNAVAILABLE`` *sentinel*
  rather than an exception, so "every vendor is throttled right now" counts as
  no data here. That is the conservative reading, and the one that keeps a
  transient outage from reaching the memory log as a Buy.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from tradingagents.agents.rating import RATING_REVIEW

logger = logging.getLogger(__name__)

# A window long enough for the indicators the market analyst uses to have a
# basis, short enough that a vendor call stays cheap.
PRICE_LOOKBACK_DAYS = 30

# The decision's own opening rating line — the one ``rating.extract_rating``
# reads. Rewritten to REVIEW when the gate fires, so the text below the header
# does not still announce the rating the run is no longer allowed to publish.
_OPENING_RATING_RE = re.compile(
    r"(?im)^(?P<lead>[ \t]*(?:[-*+]\s+)?[\s*_#]*(?:final|our|overall|my|current)?[\s*_#]*"
    r"rating[\s*_#]*[:：][\s*]*)"
    r"(?P<value>[A-Za-z]+)"
)

GATE_NOTE = (
    "\n\n**Data coverage**: no usable price data could be read for this instrument "
    "on {trade_date}, so this run is marked REVIEW rather than a tradeable call. "
    "The analysts' text above is not grounded in a price series; re-run once the "
    "market data vendors are reachable."
)


def price_data_available(ticker: str, trade_date: str) -> bool | None:
    """Whether the configured price vendors can serve this instrument on this date.

    Returns ``None`` when the probe itself could not run: an exception out of the
    router is a failure of the probe, not its verdict on the instrument. The
    sentinels ``NO_DATA_AVAILABLE`` / ``DATA_UNAVAILABLE`` are the verdicts the
    gate acts on.

    Imported lazily so ``agents`` does not import the data layer at module load.
    """
    from tradingagents.dataflows.prices import unavailable
    from tradingagents.dataflows.router import route_to_vendor

    try:
        start = (datetime.strptime(str(trade_date), "%Y-%m-%d")
                 - timedelta(days=PRICE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        start = str(trade_date)
    try:
        text = route_to_vendor("get_stock_data", ticker, start, str(trade_date))
    except Exception as exc:  # noqa: BLE001 - a failed probe is not a verdict
        logger.warning("Evidence gate: price probe failed for %s on %s: %s",
                       ticker, trade_date, exc)
        return None
    return not unavailable(text)


def _rewrite_opening_rating(decision: str, rating: str) -> str:
    """The decision with its opening rating line set to ``rating`` (if it has one)."""
    return _OPENING_RATING_RE.sub(lambda m: f"{m.group('lead')}{rating}", decision, count=1)


def gate_signal(state: dict, signal: str, config: dict) -> tuple[str, str]:
    """``(signal, note)``: the rating to publish, and the note explaining a downgrade.

    ``note`` is empty when the rating stands, so the caller can tell whether the
    decision needs rewriting.
    """
    if signal == RATING_REVIEW:
        return signal, ""
    if not config.get("evidence_gate", True):
        return signal, ""
    ticker = str(state.get("company_of_interest") or "")
    trade_date = str(state.get("trade_date") or "")
    if not ticker or not trade_date:
        return signal, ""
    # Only an explicit "no data" verdict gates; ``None`` (probe failure) leaves
    # the rating alone so a transient blip does not cost the whole run.
    if price_data_available(ticker, trade_date) is not False:
        return signal, ""
    logger.warning("Evidence gate: no price data for %s on %s; publishing REVIEW", ticker, trade_date)
    return RATING_REVIEW, GATE_NOTE.format(trade_date=trade_date)


def apply_gate_note(decision: str, note: str) -> str:
    """The decision text a gated run publishes: opening rating set to REVIEW, note appended."""
    return _rewrite_opening_rating(decision, RATING_REVIEW) + note
