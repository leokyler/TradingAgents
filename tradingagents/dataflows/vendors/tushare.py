"""Keyed A-share data from Tushare Pro: daily bars, indicators and ETF fund facts.

Tushare is the keyed counterpart to the public CN endpoints in ``cn_market``: a
token (``TUSHARE_TOKEN``) and a points tier buy a stable, exchange-sourced feed
instead of scraping pages whose edge throttles an IP after a burst. It is the
vendor to prefer for the fund-level facts an ETF decision needs — published NAV,
shares outstanding, fund size and the premium/discount they imply — and it
serves stocks just as well.

Endpoints used (all through the one JSON API at ``api.tushare.pro``):
    fund_daily   daily OHLCV for an exchange-traded fund, 10+ years
    daily        daily OHLCV for a stock
    fund_nav     published unit/accumulated NAV by nav_date
    fund_share   shares outstanding by trade date (``fd_share``, in 万份)
    fund_basic   fund name and type

Point-in-time discipline holds: every series is filtered to rows dated on or
before the run, and fund NAV/share rows also by their announcement date, so a
historical run reads what had been published by then rather than today's
snapshot. Prices are the vendor's unadjusted daily bars (fund dividends
are rare and the analysts compare across a few months), and volume is normalized
to shares to match the other price vendors.

The vendor is narrow on purpose: any non-Shanghai/Shenzhen symbol raises
``NoMarketDataError`` so a ``tushare,eastmoney`` chain falls through to the next
vendor, and a missing token raises ``VendorNotConfiguredError`` so a chain that
lists it degrades to the keyless sources instead of failing the run.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import requests

from tradingagents.dataflows import prices
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.etf import is_a_share_etf
from tradingagents.dataflows.indicators import (
    HISTORY_DAYS,
    format_indicator_window,
    indicator_values,
    require_supported,
)
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import transport

_API_URL = "https://api.tushare.pro"
_TOKEN_ENV = "TUSHARE_TOKEN"
_TIMEOUT = 15.0

# A frame whose newest bar is older than this many calendar days is treated as
# stale, matching the other price vendors; long holiday weekends fit.
MAX_STALE_DAYS = 10

_SHARES_PER_LOT = 100

def _http() -> requests.Session:
    """The API session: direct, not through a proxy."""
    return transport.session()


def _token() -> str:
    token = os.getenv(_TOKEN_ENV, "").strip()
    if not token:
        raise VendorNotConfiguredError(
            f"{_TOKEN_ENV} environment variable is not set."
        )
    return token


def _post(payload: dict) -> dict:
    """One API call; transport and API-level failures become typed vendor errors."""
    try:
        response = _http().post(_API_URL, json=payload, timeout=_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VendorUnavailableError(f"Tushare request failed: {exc}") from exc
    code = body.get("code")
    if code != 0:
        raise VendorUnavailableError(f"Tushare API error {code}: {body.get('msg')}")
    return body


def _api(api_name: str, **params) -> pd.DataFrame:
    """Call an endpoint and return its rows as a DataFrame."""
    body = _post({"api_name": api_name, "token": _token(), "params": params, "fields": ""})
    data = body.get("data") or {}
    return pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])


def _ts_code(symbol: str) -> tuple[str, str]:
    """``(canonical, Tushare ts_code)`` for an A-share listing, or a typed refusal."""
    canonical = normalize_symbol(symbol)
    code, _, market = canonical.partition(".")
    if market not in ("SS", "SZ") or not (len(code) == 6 and code.isdigit()):
        raise NoMarketDataError(
            symbol,
            canonical,
            "not a Shanghai/Shenzhen listing; the Tushare vendor covers A-shares only",
        )
    return canonical, f"{code}.{'SH' if market == 'SS' else 'SZ'}"


def _end_date(end_date: str) -> datetime:
    return min(
        datetime.strptime(end_date, "%Y-%m-%d"),
        datetime.strptime(get_current_date(), "%Y-%m-%d"),
    )


def _price_frame(symbol: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, datetime]:
    """Daily bars for an A-share ETF or stock, sorted and cut at the run date."""
    canonical, ts_code = _ts_code(symbol)
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = _end_date(end_date)
    except ValueError as exc:
        raise ValueError(f"dates must be YYYY-MM-DD, got {start_date!r} and {end_date!r}") from exc
    if start > end:
        raise ValueError(f"start date {start_date} is after end date {end_date}")

    api_name = "fund_daily" if is_a_share_etf(canonical) else "daily"
    frame = _api(
        api_name,
        ts_code=ts_code,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if frame is None or frame.empty:
        raise NoMarketDataError(
            canonical, canonical, f"no daily bars between {start_date} and {end_date}"
        )
    renames = {"trade_date": "Date", "open": "Open", "high": "High", "low": "Low",
               "close": "Close", "vol": "Volume"}
    frame = frame.rename(columns={k: v for k, v in renames.items() if k in frame.columns})
    if "Date" not in frame.columns or "Close" not in frame.columns:
        raise VendorUnavailableError(f"Tushare {api_name} returned unexpected columns")

    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["Date"] = pd.to_datetime(frame["Date"], format="%Y%m%d", errors="coerce")
    frame = frame.dropna(subset=["Date", "Close"])
    frame = frame[(frame["Date"] >= start) & (frame["Date"] <= end)]
    frame = frame.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    if frame.empty:
        raise NoMarketDataError(
            canonical, canonical, f"no usable daily bars between {start_date} and {end_date}"
        )
    if "Volume" in frame.columns:
        frame["Volume"] = frame["Volume"].fillna(0) * _SHARES_PER_LOT
    gap = (end - frame["Date"].iloc[-1]).days
    if gap > MAX_STALE_DAYS:
        raise NoMarketDataError(
            canonical,
            canonical,
            f"latest row is {frame['Date'].iloc[-1]:%Y-%m-%d}, {gap} days before {end:%Y-%m-%d}",
        )
    return frame, end


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV as the CSV text the analyst reads, priced by Tushare."""
    canonical, _ = _ts_code(symbol)
    frame, _ = _price_frame(canonical, start_date, end_date)
    return prices.render_ohlcv(
        frame, canonical, start_date, end_date, "Tushare Pro daily bars (unadjusted)"
    )


def get_indicators(symbol: str, indicator: str, as_of_date: str, look_back_days: int) -> str:
    """One indicator's dated window, computed from Tushare bars."""
    require_supported(indicator)
    canonical, _ = _ts_code(symbol)
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d")
    start = as_of - pd.Timedelta(days=HISTORY_DAYS)
    frame, _ = _price_frame(canonical, start.strftime("%Y-%m-%d"), as_of_date)
    values = indicator_values(frame, indicator)
    return format_indicator_window(indicator, as_of_date, look_back_days, values)


def _latest(
    frame: pd.DataFrame, date_column: str, as_of: datetime, announced: str | None = None
) -> pd.Series | None:
    """The newest row dated on or before ``as_of`` — and, when the endpoint
    reports one, announced on or before it too.

    A value's ``nav_date``/``trade_date`` is the period it describes; ``ann_date``
    is when the vendor published it. Filtering on the period alone would let a
    historical run read a figure announced after the run date, which is exactly
    the leak the point-in-time rule exists to prevent.
    """
    if frame is None or frame.empty or date_column not in frame.columns:
        return None
    dates = pd.to_datetime(frame[date_column], format="%Y%m%d", errors="coerce")
    frame = frame.assign(_date=dates).dropna(subset=["_date"])
    frame = frame[frame["_date"] <= as_of]
    if announced and announced in frame.columns:
        published = pd.to_datetime(frame[announced], format="%Y%m%d", errors="coerce")
        frame = frame[published.isna() | (published <= as_of)]
    frame = frame.sort_values("_date")
    if frame.empty:
        return None
    return frame.iloc[-1]


def get_etf_profile(ticker: str, as_of_date: str | None = None) -> str:
    """Fund profile for an A-share ETF: price, size, shares, NAV and premium."""
    canonical, ts_code = _ts_code(ticker)
    if not is_a_share_etf(canonical):
        raise NoMarketDataError(
            ticker, canonical, "not an A-share exchange-traded fund; use a quote vendor for it"
        )
    as_of = _end_date(as_of_date or get_current_date())
    window_start = as_of - pd.Timedelta(days=30)

    bars, _ = _price_frame(canonical, window_start.strftime("%Y-%m-%d"), as_of.strftime("%Y-%m-%d"))
    latest = bars.iloc[-1]
    price = float(latest["Close"])
    prev = float(latest["pre_close"]) if "pre_close" in bars.columns else price

    nav_rows = _api(
        "fund_nav",
        ts_code=ts_code,
        start_date=(as_of - pd.Timedelta(days=365)).strftime("%Y%m%d"),
        end_date=as_of.strftime("%Y%m%d"),
    )
    nav_row = _latest(nav_rows, "nav_date", as_of, announced="ann_date")
    shares_row = _latest(
        _api(
            "fund_share",
            ts_code=ts_code,
            start_date=(as_of - pd.Timedelta(days=365)).strftime("%Y%m%d"),
            end_date=as_of.strftime("%Y%m%d"),
        ),
        "trade_date",
        as_of,
        announced="ann_date",
    )

    name = canonical
    try:
        basic = _api("fund_basic", ts_code=ts_code)
        if basic is not None and not basic.empty and "name" in basic.columns:
            name = str(basic.iloc[0]["name"])
    except (NoMarketDataError, VendorUnavailableError):
        pass

    lines = [f"# ETF profile for {canonical} ({name})", ""]
    lines.append(f"- **Price**: {price:.3f} CNY")
    if "pct_chg" in bars.columns:
        pct = float(bars.iloc[-1]["pct_chg"])
        lines.append(f"- **Daily change**: {pct:+.2f}%")
    lines.append(f"- **Previous close**: {prev:.3f} CNY")
    if "Volume" in bars.columns:
        lines.append(f"- **Session volume**: {float(latest['Volume']) / 1e6:.2f} million shares")
    if shares_row is not None and pd.notna(shares_row.get("fd_share")):
        shares = float(shares_row["fd_share"]) * 1e4
        lines.append(f"- **Shares outstanding**: {shares / 1e8:.2f} 亿份")
        lines.append(
            f"- **Fund size (shares x price)**: {shares * price / 1e8:.2f} 亿元"
        )
    if nav_row is not None and pd.notna(nav_row.get("unit_nav")):
        nav = float(nav_row["unit_nav"])
        nav_date = pd.to_datetime(nav_row["nav_date"], format="%Y%m%d").strftime("%Y-%m-%d")
        lines.append(f"- **Latest published NAV**: {nav:.4f} ({nav_date})")
        if nav:
            premium = (price - nav) / nav * 100
            state = "premium" if premium > 0 else "discount"
            lines.append(f"- **Premium/discount vs latest NAV**: {premium:+.2f}% ({state})")
    lines += [
        "",
        "Notes:",
        "- NAV is the latest *published* one on or before the run date; an intraday",
        "  run compares a live price with the previous close NAV, so the premium is",
        "  an estimate, not a real-time IOPV.",
        "- Fund size is shares outstanding x market price; net assets use NAV.",
    ]
    return "\n".join(lines)
