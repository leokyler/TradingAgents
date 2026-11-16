"""The ETF price window: price, return, drawdown and turnover, computed once.

The ETF analyst used to receive only fund-level blocks (profile, holdings,
basket valuation), so a run whose profile was withheld had no price, volume or
size figures at all and the report said so for most of its length. Prices live
in the market-data path, not the fund-data path, so this block is fetched
separately and never withheld together with the profile: it is the same
``cn_market`` series the price tools serve, cut at the analysis date.

Every number a reader would otherwise have to compute is computed here
deterministically — latest close, 5/20/60-session returns, the 60-session high
and low, the drawdown from that high, and the 20-session average turnover. The
model reports the figures; it does not derive them, so a weak model cannot
invent a different return than the data supports.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from tradingagents.dataflows import prices, units
from tradingagents.dataflows.dates import parse_day
from tradingagents.dataflows.router import get_cn_market_stock

logger = logging.getLogger(__name__)

# The analyst is asked about the recent trend, so the window is a quarter of
# trading days. The calendar lookback carries a buffer for weekends and holidays.
WINDOW_SESSIONS = 60
_CALENDAR_LOOKBACK_DAYS = 180

# The turnover estimate uses close x volume when the vendor's CSV carries no
# amount column; it is labelled as an estimate rather than passed off as the
# exchange's own turnover figure.
_AMOUNT_NOTE = "estimated as close x volume"


def _price_frame(ticker: str, start_date: str, end_date: str) -> tuple[pd.DataFrame | None, str]:
    """The daily bars and the source that served them, or ``(None, reason)``.

    The block reads ``cn_market`` directly, deliberately: the requirement fixes
    that source, and the vendor already falls back Eastmoney -> Tencent -> Sina.
    This is the documented exception to "the configured chain is exact"; the
    ETF analyst only runs for A-share funds, and no other market reaches it.
    """
    try:
        text = get_cn_market_stock(ticker, start_date, end_date)
        frame = prices.frame_from_text(text)
        if frame is not None:
            missing = {"Date", "Open", "High", "Low", "Close", "Volume"} - set(frame.columns)
            if not missing:
                return frame, "cn_market"
            reason = f"cn_market text lacks {', '.join(sorted(missing))}"
        else:
            reason = "cn_market returned no usable bars"
    except Exception as exc:  # noqa: BLE001 - a price block must not end the run
        logger.warning("ETF Analyst: cn_market price window failed for %s: %s", ticker, exc)
        reason = f"cn_market failed ({type(exc).__name__})"
    return None, reason


def _cut(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Rows on or before the analysis date, oldest first, one row per day."""
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    cutoff = pd.Timestamp(trade_date)
    frame = frame[frame["Date"] <= cutoff]
    return frame.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)


def _pct_change(closes: pd.Series, sessions: int) -> float | None:
    """Percent change over ``sessions`` rows, or None when history is shorter."""
    if len(closes) <= sessions:
        return None
    base = float(closes.iloc[-1 - sessions])
    if not base:
        return None
    return (float(closes.iloc[-1]) / base - 1.0) * 100.0


def _turnover(frame: pd.DataFrame) -> tuple[float | None, bool]:
    """Mean daily traded value over the window, and whether it is an estimate."""
    if "Amount" in frame.columns:
        amount = pd.to_numeric(frame["Amount"], errors="coerce")
        return (float(amount.mean()) if amount.notna().any() else None), False
    if "Close" not in frame.columns or "Volume" not in frame.columns:
        return None, True
    value = pd.to_numeric(frame["Close"], errors="coerce") * pd.to_numeric(
        frame["Volume"], errors="coerce"
    )
    return (float(value.mean()) if value.notna().any() else None), True


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def build_price_window(ticker: str, trade_date: str) -> str:
    """The deterministic price block, or a placeholder naming what failed.

    The block starts with its source and the session it actually reaches, so a
    reader can tell a live window from a stale one. It never contains a figure
    the run could not have seen: the frame is cut at ``trade_date``.
    """
    as_of = parse_day(trade_date)
    if as_of is None:
        return (
            f"<price window unavailable for {ticker!r}: trade_date {trade_date!r} "
            "is not YYYY-MM-DD; do not estimate prices.>"
        )

    start_date = (as_of - timedelta(days=_CALENDAR_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    frame, source = _price_frame(ticker, start_date, trade_date)
    if frame is None:
        return (
            f"<price window unavailable for {ticker} between {start_date} and "
            f"{trade_date}: {source}. This is not an absence of price data, only "
            "a failed read; do not estimate or fabricate prices, volume or returns.>"
        )

    frame = _cut(frame, trade_date)
    if frame.empty:
        return (
            f"<price window unavailable for {ticker}: no daily bar on or before "
            f"{trade_date}; do not estimate prices.>"
        )

    latest = frame.iloc[-1]
    last_date = latest["Date"]
    closes = pd.to_numeric(frame["Close"], errors="coerce")
    window = frame.tail(WINDOW_SESSIONS)

    high = float(pd.to_numeric(window["High"], errors="coerce").max())
    low = float(pd.to_numeric(window["Low"], errors="coerce").min())
    last_close = float(closes.iloc[-1])
    drawdown = (last_close / high - 1.0) * 100.0 if high else None
    average_turnover, estimated = _turnover(frame.tail(20))

    lines = [
        f"# Price and liquidity window for {ticker}",
        f"# Source: {source}, through the latest session {last_date:%Y-%m-%d}.",
        "",
        f"- **Latest close**: {last_close:.3f} CNY ({last_date:%Y-%m-%d})",
        f"- **Change over 5 sessions**: {_pct(_pct_change(closes, 5))}",
        f"- **Change over 20 sessions**: {_pct(_pct_change(closes, 20))}",
        f"- **Change over 60 sessions**: {_pct(_pct_change(closes, WINDOW_SESSIONS))}",
        f"- **60-session high**: {high:.3f} CNY",
        f"- **60-session low**: {low:.3f} CNY",
        f"- **Drawdown from the 60-session high**: {_pct(drawdown)}",
        f"- **Average daily turnover over the last 20 sessions**: {units.money(average_turnover)}"
        + (f" ({_AMOUNT_NOTE})" if estimated else ""),
        f"- **Sessions used**: {len(window)} from {window['Date'].iloc[0]:%Y-%m-%d} "
        f"to {last_date:%Y-%m-%d} (forward-adjusted where available)",
        "",
        "Notes:",
        "- This block is independent of the fund profile: it is served by the "
        "price vendor and stays available even when the profile is withheld.",
        "- Returns and drawdowns are computed from the closes above; they already "
        "include the fund's own distributions where the vendor adjusts them.",
    ]
    return "\n".join(lines)
