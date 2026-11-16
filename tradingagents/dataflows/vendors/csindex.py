"""Second, keyless source for an A-share ETF's basket valuation.

Eastmoney's fundf10 holdings page is the primary source for the disclosed
basket, but it is one host and one page: a timeout there used to cost the ETF
analyst its whole valuation block. CSI's own index-valuation series covers the
same fund at the index level, is public, and is available through akshare
(``stock_zh_index_value_csindex``). This vendor resolves the fund's benchmark
from a second keyless source (THS fund basics), finds that benchmark in CSI's
index catalogue, and serves its PE and dividend yield as of the analysis date.

It is a fallback, not a replacement: CSI publishes index PE and dividend yield,
not PB or the constituent-weighted aggregate Eastmoney computes. The block says
so, and never fabricates the missing multiples.
"""

from __future__ import annotations

import functools
import io
import logging

import pandas as pd
import requests

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.dates import parse_day
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.etf import etf_code
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import transport

logger = logging.getLogger(__name__)

_CN_PIP_HINT = 'pip install "tradingagents[cn]"'

# CSI publishes the valuation series as one .xls per index. akshare reads it
# without a request timeout, which can stall the fallback for a minute; the
# fork fetches the same public file with a bounded request instead.
_CSI_INDICATOR_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/"
    "autofile/indicator/{code}indicator.xls"
)
_CSI_TIMEOUT = 12.0
_CSI_COLUMNS = [
    "日期", "指数代码", "指数中文全称", "指数中文简称", "指数英文全称",
    "指数英文简称", "市盈率1", "市盈率2", "股息率1", "股息率2",
]


def _import_akshare():
    """akshare, imported lazily so a plain install never needs it."""
    try:
        import akshare
    except ImportError as exc:
        raise VendorNotConfiguredError(
            "csindex vendor is not configured: the optional 'akshare' package is "
            f"not installed; install it with {_CN_PIP_HINT}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - installed but broken is an outage
        raise VendorUnavailableError(f"akshare failed to import: {exc}") from exc
    return akshare


@functools.lru_cache(maxsize=256)
def _benchmark_name(fund_code: str) -> str:
    """The fund's benchmark text, from THS's keyless fund basics."""
    akshare = _import_akshare()
    fetch = getattr(akshare, "fund_info_ths", None)
    if not callable(fetch):
        raise VendorUnavailableError("the installed akshare has no fund_info_ths")
    try:
        frame = fetch(symbol=fund_code)
    except Exception as exc:  # noqa: BLE001
        raise VendorUnavailableError(f"akshare fund_info_ths failed: {exc}") from exc
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    for row in rows:
        if str(row.get("字段") or "").strip() == "业绩比较基准":
            text = str(row.get("值") or "").strip()
            if text:
                return text
    raise NoMarketDataError(fund_code, fund_code,
                            "the fund's benchmark is not reported by fund_info_ths")


@functools.lru_cache(maxsize=1)
def _index_catalogue(akshare) -> dict[str, str]:
    """CSI index name -> code, from its public catalogue."""
    fetch = getattr(akshare, "index_csindex_all", None)
    if not callable(fetch):
        raise VendorUnavailableError("the installed akshare has no index_csindex_all")
    try:
        frame = fetch()
    except Exception as exc:  # noqa: BLE001
        raise VendorUnavailableError(f"akshare index_csindex_all failed: {exc}") from exc
    lookup: dict[str, str] = {}
    for row in frame.to_dict("records"):
        code = str(row.get("指数代码") or "").strip()
        for key in ("指数全称", "指数简称"):
            name = str(row.get(key) or "").strip()
            if code and name:
                lookup.setdefault(name, code)
    return lookup


def _fetch_csindex(index_code: str) -> pd.DataFrame:
    """The index's public valuation workbook, read with a bounded timeout."""
    url = _CSI_INDICATOR_URL.format(code=index_code)
    last: Exception | None = None
    for attempt in range(2):   # one retry, like the primary valuation endpoint
        try:
            response = transport.session().get(url, timeout=_CSI_TIMEOUT)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last = exc
            if attempt == 0:
                import time
                time.sleep(0.3)
    else:
        raise VendorUnavailableError(
            f"CSIndex valuation request failed after {_CSI_TIMEOUT:.0f}s "
            f"(retried once): {last}"
        ) from last
    try:
        frame = pd.read_excel(io.BytesIO(response.content))
    except Exception as exc:  # noqa: BLE001 - a moved/corrupt file is an outage
        raise VendorUnavailableError(f"CSIndex valuation file is unreadable: {exc}") from exc
    if frame.shape[1] < len(_CSI_COLUMNS):
        raise VendorUnavailableError("CSIndex valuation file has unexpected columns")
    frame.columns = _CSI_COLUMNS
    frame["日期"] = pd.to_datetime(frame["日期"].astype(str), format="%Y%m%d", errors="coerce")
    for column in ("市盈率1", "市盈率2", "股息率1", "股息率2"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["日期"])


def _index_code(fund_code: str, akshare) -> tuple[str, str]:
    """``(index_code, index_name)`` for the fund's benchmark."""
    benchmark = _benchmark_name(fund_code)
    # "中证人工智能主题指数收益率" / "中证人工智能主题指数" -> the catalogue name.
    for suffix in ("收益率", "全收益", "价格指数"):
        if benchmark.endswith(suffix):
            benchmark = benchmark[: -len(suffix)].strip()
    catalogue = _index_catalogue(akshare)
    code = catalogue.get(benchmark)
    if code is None:
        # The benchmark sometimes names the index with its exchange prefix.
        code = catalogue.get(benchmark.replace("中证", "", 1).strip())
    if code is None:
        raise VendorUnavailableError(
            f"could not resolve the benchmark {benchmark!r} to a CSI index code"
        )
    return code, benchmark


def get_etf_index_fundamentals(ticker: str, as_of_date: str | None = None,
                               limit: int = 10) -> str:
    """Index-level PE and dividend yield for an A-share ETF's benchmark.

    Dated point-in-time: only valuations whose date is on or before the run date
    are served. The block states that this is the index's own multiple, not the
    constituent-weighted aggregate the primary vendor computes, and that this
    source does not publish PB or ROE.
    """
    canonical = normalize_symbol(ticker)
    if etf_code(canonical) is None:
        raise NoMarketDataError(
            ticker, canonical,
            "not an A-share exchange-traded fund; the CSI valuation source covers "
            "Shanghai/Shenzhen listed funds only",
        )
    as_of = parse_day(as_of_date or get_current_date())
    if as_of is None:
        raise ValueError(f"as_of_date must be YYYY-MM-DD, got {as_of_date!r}")

    akshare = _import_akshare()
    code = str(etf_code(canonical))
    index_code, index_name = _index_code(code, akshare)
    frame = _fetch_csindex(index_code)
    if frame is None or frame.empty:
        raise NoMarketDataError(
            canonical, canonical,
            f"CSI returned no valuation rows for index {index_code}",
        )

    rows = []
    for row in frame.to_dict("records"):
        day = row.get("日期").date() if hasattr(row.get("日期"), "date") else parse_day(row.get("日期"))
        if day is not None and day <= as_of:
            rows.append((day, row))
    if not rows:
        raise NoMarketDataError(
            canonical, canonical,
            f"no CSI valuation on or before {as_of} for index {index_code} "
            f"(the source serves recent rows only)",
        )
    day, latest = max(rows, key=lambda item: item[0])

    def number(key):
        try:
            value = float(latest.get(key))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    pe_static, pe_ttm = number("市盈率1"), number("市盈率2")
    yield_static, yield_ttm = number("股息率1"), number("股息率2")

    lines = [
        f"# ETF index fundamentals for {canonical} "
        f"(CSI index valuation: {index_name} {index_code})",
        f"# Source: csindex via akshare; valuation as of {day:%Y-%m-%d}.",
        "",
        f"- **Index**: {index_name} ({index_code})",
        f"- **Valuation date**: {day:%Y-%m-%d}",
    ]
    if pe_static:
        lines.append(f"- **PE (static)**: {pe_static:.2f}")
    if pe_ttm:
        lines.append(f"- **PE (TTM)**: {pe_ttm:.2f}")
    if yield_static:
        lines.append(f"- **Dividend yield (static)**: {yield_static:.2f}%")
    if yield_ttm:
        lines.append(f"- **Dividend yield (TTM)**: {yield_ttm:.2f}%")
    lines += [
        "",
        "Notes:",
        "- This is the index's own multiple, not the capital-weighted aggregate "
        "over the fund's disclosed holdings that the primary vendor computes.",
        "- This source publishes PE and dividend yield only; it does not provide "
        "PB or ROE. Do not infer them.",
        "- The valuation is dated; a stale date means the source has not yet "
        "published a newer one, not that the index has no valuation.",
    ]
    return "\n".join(lines)
