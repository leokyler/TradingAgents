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
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from tradingagents.agents.rating import RATING_REVIEW

logger = logging.getLogger(__name__)

# A window long enough for the indicators the market analyst uses to have a
# basis, short enough that a vendor call stays cheap.
PRICE_LOOKBACK_DAYS = 30

# The two sentinels the router returns when no configured vendor could serve a
# call; either one means there is no price series to analyze.
PRICE_UNAVAILABLE_PREFIXES = ("NO_DATA_AVAILABLE", "DATA_UNAVAILABLE")

GATE_NOTE = (
    "\n\n**Data coverage**: no usable price data could be read for this instrument "
    "on {trade_date}, so this run is marked REVIEW rather than a tradeable call. "
    "The analysts' text above is not grounded in a price series; re-run once the "
    "market data vendors are reachable."
)


def price_data_available(ticker: str, trade_date: str) -> bool | None:
    """Whether the configured price vendors can serve this instrument on this date.

    Returns ``None`` when the probe itself could not run (a vendor raised): an
    exception is a probe failure, not the data layer's verdict on the instrument,
    and downgrading a run over it would turn a blip into a lost analysis. The
    explicit ``NO_DATA_AVAILABLE`` / ``DATA_UNAVAILABLE`` sentinels are the
    verdicts the gate acts on.

    Imported lazily so ``agents`` does not import the data layer at module load.
    """
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
    return not str(text).startswith(PRICE_UNAVAILABLE_PREFIXES)


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
