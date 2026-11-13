"""Daily prices and technical indicators for A-share listings, from public CN endpoints.

Yahoo is the framework's default price vendor, but it throttles anonymous
clients and treats Chinese funds as second-class. Three keyless public
endpoints cover exactly the symbols the ETF analyst already relies on —
Shanghai (``.SS``) and Shenzhen (``.SZ``) listings — and they fail
independently, so one blocked edge does not cost the analyst its prices:

    push2his.eastmoney.com        Eastmoney daily kline, forward-adjusted
    web.ifzq.gtimg.cn             Tencent daily kline, forward-adjusted
    quotes.sina.cn                Sina daily kline

The vendor is deliberately narrow: any other symbol raises
``NoMarketDataError`` so a configured ``cn_market,yfinance`` chain falls
through to Yahoo, and the agent still sees one unambiguous "no data" signal
when no source can serve it.

Volume is reported in shares (as Yahoo does), so the analyst and the
indicators reason about one unit: Eastmoney and Tencent quote lots (手) and are
multiplied by 100; Sina already quotes shares.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.indicators import (
    INDICATOR_DESCRIPTIONS,
    format_indicator_window,
    indicator_values,
)
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.eastmoney import _get as _eastmoney_get

_TIMEOUT = 10.0

# A frame whose newest bar is older than this many calendar days is treated as
# stale, the same guard the Yahoo vendor applies; long holiday weekends fit.
MAX_STALE_DAYS = 10

# Enough history for the 200-day averages the analyst asks for.
INDICATOR_HISTORY_DAYS = 400

_SHARES_PER_LOT = 100

# The analyst asks for several indicators over the same history, and every one
# would otherwise re-read the same kline. A short-lived in-process cache keeps
# one analysis to a handful of requests.
_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()


def _secid(symbol: str) -> tuple[str, str, str]:
    """``(canonical, eastmoney secid, tencent/sina symbol)`` for an A-share listing."""
    canonical = normalize_symbol(symbol)
    code, _, market = canonical.partition(".")
    if market not in ("SS", "SZ") or not (len(code) == 6 and code.isdigit()):
        raise NoMarketDataError(
            symbol,
            canonical,
            "not a Shanghai/Shenzhen listing; the CN price vendor covers A-shares only",
        )
    market_id = "1" if market == "SS" else "0"
    prefix = "sh" if market == "SS" else "sz"
    return canonical, f"{market_id}.{code}", f"{prefix}{code}"


def _price_text(value: float) -> str:
    """A price with at least two decimals, trailing zeros trimmed (Yahoo's style)."""
    text = f"{value:.6f}".rstrip("0")
    whole, _, fraction = text.partition(".")
    if len(fraction) < 2:
        fraction = fraction.ljust(2, "0")
    return f"{whole}.{fraction}"


def _load_json(body: str, source: str):
    """A vendor JSON body, or a typed transport error when it is unparseable."""
    try:
        return json.loads(body)
    except ValueError as exc:
        raise VendorUnavailableError(f"{source} returned unparseable JSON") from exc


def _to_frame(rows) -> pd.DataFrame:
    """Normalize row tuples ``(date, open, close, high, low, volume)`` to a frame."""
    records = []
    for row in rows:
        if isinstance(row, dict):
            parts = [row.get("day"), row.get("open"), row.get("close"), row.get("high"),
                     row.get("low"), row.get("volume")]
        else:
            parts = list(row)
        if len(parts) < 6:
            continue
        try:
            records.append({
                "Date": pd.Timestamp(parts[0]),
                "Open": float(parts[1]),
                "Close": float(parts[2]),
                "High": float(parts[3]),
                "Low": float(parts[4]),
                "Volume": float(parts[5]),
            })
        except (TypeError, ValueError):
            continue
    return pd.DataFrame(records)


def _bar_count(start: datetime) -> int:
    """How many trailing daily bars to ask a latest-N endpoint for.

    The latest-N endpoints count back from *today*, not from the run's end date,
    so a historical run must still ask for everything between its start and now.
    """
    today = datetime.strptime(get_current_date(), "%Y-%m-%d")
    days = max((today - start).days + 15, 40)
    return min(days, 2500)


def _eastmoney_klines(canonical: str, secid: str, start: datetime, end: datetime) -> pd.DataFrame:
    body = _eastmoney_get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": secid,
            "fields1": "f1,f2,f3",
            # f51 date, f52 open, f53 close, f54 high, f55 low, f56 volume (lots), f57 amount
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101",
            "fqt": "1",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        },
        referer=f"https://quote.eastmoney.com/{canonical.split('.')[0]}.html",
    )
    payload = _load_json(body, "Eastmoney")
    rows = ((payload.get("data") or {}).get("klines")) or []
    frame = _to_frame(row.split(",")[:6] for row in rows)
    if not frame.empty:
        frame["Volume"] *= _SHARES_PER_LOT
    return frame


def _tencent_klines(canonical: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    body = _plain_get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        {"param": f"{symbol},day,,,{_bar_count(start)},qfq"},
    )
    payload = _load_json(body, "Tencent")
    node = (payload.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    frame = _to_frame(row[:6] for row in rows)
    if not frame.empty:
        frame["Volume"] *= _SHARES_PER_LOT
    return frame


def _sina_klines(canonical: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    body = _plain_get(
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
        {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(_bar_count(start))},
    )
    rows = _load_json(body, "Sina")
    if not isinstance(rows, list):
        rows = []
    return _to_frame(rows)


_plain_session: requests.Session | None = None


def _plain_get(url: str, params: dict) -> str:
    """GET a mainland endpoint directly (proxies only get in the way of CN hosts)."""
    global _plain_session
    if _plain_session is None:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; TradingAgents)"})
        _plain_session = session
    try:
        response = _plain_session.get(url, params=params, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"CN quote endpoint request failed for {url}: {exc}") from exc


def _clip(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame if frame is not None else pd.DataFrame()
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame[(frame["Date"] >= start) & (frame["Date"] <= end)]
    return (
        frame.sort_values("Date")
        .drop_duplicates("Date", keep="last")
        .reset_index(drop=True)
    )


def _klines(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily bars between two dates, from the first source that can serve them."""
    canonical, secid, cn_symbol = _secid(symbol)
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"dates must be YYYY-MM-DD, got {start_date!r} and {end_date!r}") from exc
    if start > end:
        raise ValueError(f"start date {start_date} is after end date {end_date}")

    # A run can never see a bar after its own date.
    end = min(end, datetime.strptime(get_current_date(), "%Y-%m-%d"))
    key = (canonical, start_date, end_date)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1].copy()

    sources = (
        ("eastmoney", lambda: _eastmoney_klines(canonical, secid, start, end)),
        ("tencent", lambda: _tencent_klines(canonical, cn_symbol, start, end)),
        ("sina", lambda: _sina_klines(canonical, cn_symbol, start, end)),
    )
    first_no_data: NoMarketDataError | None = None
    last_unavailable: VendorUnavailableError | None = None
    for name, fetch in sources:
        try:
            frame = _clip(fetch(), start, end)
        except NoMarketDataError as exc:
            first_no_data = first_no_data or exc
            continue
        except VendorUnavailableError as exc:
            last_unavailable = exc
            continue
        if frame is None or frame.empty:
            continue
        newest = frame["Date"].iloc[-1]
        gap = (end - newest).days
        if gap > MAX_STALE_DAYS:
            first_no_data = first_no_data or NoMarketDataError(
                canonical, canonical,
                f"latest row is {newest:%Y-%m-%d}, {gap} days before {end:%Y-%m-%d} (stale frame)",
            )
            continue
        with _cache_lock:
            if len(_cache) > 32:
                _cache.clear()
            _cache[key] = (time.monotonic(), frame)
        return frame

    if last_unavailable is not None:
        raise last_unavailable
    if first_no_data is not None:
        raise first_no_data
    raise NoMarketDataError(canonical, canonical, f"no daily bars between {start_date} and {end_date}")


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV as the CSV text the analyst reads, priced from CN endpoints."""
    canonical, _, _ = _secid(symbol)
    frame = _klines(canonical, start_date, end_date)
    lines = [
        f"# Stock data for {canonical} from {start_date} to {end_date}",
        f"# Total records: {len(frame)}",
        "# Source: public China quote endpoints (forward-adjusted where available)",
        "",
        "Date,Open,High,Low,Close,Volume",
    ]
    for row in frame.itertuples(index=False):
        lines.append(
            f"{row.Date:%Y-%m-%d},{_price_text(row.Open)},{_price_text(row.High)},"
            f"{_price_text(row.Low)},{_price_text(row.Close)},{int(row.Volume)}"
        )
    return "\n".join(lines)


def get_indicators(symbol: str, indicator: str, as_of_date: str, look_back_days: int) -> str:
    """One indicator's dated window, computed from CN daily bars."""
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {list(INDICATOR_DESCRIPTIONS)}"
        )
    canonical, _, _ = _secid(symbol)
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d")
    start = (as_of - relativedelta(days=INDICATOR_HISTORY_DAYS)).strftime("%Y-%m-%d")
    frame = _klines(canonical, start, as_of_date)
    values = indicator_values(frame, indicator)
    return format_indicator_window(indicator, as_of_date, look_back_days, values)
