"""The price text the vendors serve, and reading it back.

Every price vendor answers the price tools with the same CSV block, because the
analyst reads one format whichever vendor ran: a few ``#`` header lines, then
``Date,Open,High,Low,Close,Volume``. Two callers treat that text as data rather
than prose — the verification tool rebuilding a snapshot from a routed vendor,
and the evidence gate asking whether prices exist at all — so the format lives
here once instead of being re-derived at each site.

The ``NO_DATA_AVAILABLE`` / ``DATA_UNAVAILABLE`` sentinels are written by the
router (``router.no_data_available`` / ``router.vendor_unavailable``); the
predicate below is their single reader, kept in step with them.
"""

from __future__ import annotations

import io

import pandas as pd

# The prefixes the router puts in front of a failed call.
UNAVAILABLE_PREFIXES = ("NO_DATA_AVAILABLE", "DATA_UNAVAILABLE")

CSV_HEADER = "Date,Open,High,Low,Close,Volume"


def unavailable(text: object) -> bool:
    """Whether ``text`` is a router sentinel rather than data."""
    return str(text).startswith(UNAVAILABLE_PREFIXES)


def price_text(value: float) -> str:
    """A price with at least two decimals, trailing zeros trimmed (Yahoo's style)."""
    text = f"{value:.6f}".rstrip("0")
    whole, _, fraction = text.partition(".")
    if len(fraction) < 2:
        fraction = fraction.ljust(2, "0")
    return f"{whole}.{fraction}"


def render_ohlcv(
    frame: pd.DataFrame, symbol: str, start_date: str, end_date: str, source: str
) -> str:
    """The analyst-facing CSV for one vendor's daily bars."""
    lines = [
        f"# Stock data for {symbol} from {start_date} to {end_date}",
        f"# Total records: {len(frame)}",
        f"# Source: {source}",
        "",
        CSV_HEADER,
    ]
    for row in frame.itertuples(index=False):
        lines.append(
            f"{row.Date:%Y-%m-%d},{price_text(row.Open)},{price_text(row.High)},"
            f"{price_text(row.Low)},{price_text(row.Close)},{int(row.Volume)}"
        )
    return "\n".join(lines)


def frame_from_text(text: str) -> pd.DataFrame | None:
    """The OHLCV frame inside a price text, or ``None`` if it is not one.

    Returns ``None`` for a sentinel and for anything that does not parse into a
    frame with a ``Date`` column, so callers can fall back to their own sentinel
    path without raising.
    """
    if not text or unavailable(text):
        return None
    rows = [line for line in str(text).splitlines() if line.strip() and not line.startswith("#")]
    if not rows:
        return None
    try:
        frame = pd.read_csv(io.StringIO("\n".join(rows)))
    except Exception:  # noqa: BLE001 - not a price table; leave the caller's sentinel path
        return None
    if frame.empty or "Date" not in frame.columns:
        return None
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"]).sort_values("Date")
    return None if frame.empty else frame
