"""Fund-level data for A-share ETFs, from Eastmoney's public HTTP endpoints.

Yahoo has no fund data for Chinese ETFs (``funds_data`` answers "No Fund data
found"), so everything an ETF decision actually needs — shares outstanding, fund
size, latest NAV and premium/discount, disclosed holdings, and the valuation of
the basket — comes from Eastmoney here. No account or key is required, and the
three calls are the same ones its own fund pages make.

Request paths (all public, all plain HTTP):
    push2.eastmoney.com/api/qt/stock/get          real-time quote of the fund
    fund.eastmoney.com/pingzhongdata/<code>.js    name, NAV history, fee, scale
    fundf10.eastmoney.com/FundArchivesDatas.aspx  quarterly disclosed holdings

Two correctness rules, shared with the rest of the data layer:

* The holdings page is a *quarterly* disclosure, and the page always serves the
  newest one. Serving it for a run dated before its disclosure date would leak
  the future, so a historical run asks for the last quarter that had closed by
  then (``year``/``month`` parameters) and withholds the answer if that quarter
  is still newer than the run date.

* The quote, NAV and holdings-capped figures are present-day snapshots with no
  filing date, so a run dated in the past gets a withheld notice rather than a
  value it could not have seen (same rule as Yahoo's live profile).

A non-A-share symbol (a US ETF, a stock, an index) raises ``NoMarketDataError``
instead of returning nothing: the router turns that into one clear "no data"
signal and never silently prices the wrong instrument.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.etf import etf_code
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors import transport

logger = logging.getLogger(__name__)

_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_FUND_JS_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_HOLDINGS_REFERER = "https://fundf10.eastmoney.com/ccmx_{code}.html"

# One quote request carries every field the profile needs. fltt=2 makes prices
# decimal (otherwise they arrive as integers scaled by 10**f59).
_QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f84,f116,f168,f169,f170"

# A constituent quote only needs valuation and the industry: f116 market cap,
# f162 PE (TTM), f167 PB, f127 sub-industry name, f58 name.
_CONSTITUENT_FIELDS = "f57,f58,f116,f162,f167,f127"

# A quote or a fund page that has not answered in five seconds is not going to:
# the run has other work, and Eastmoney has other hosts.
_TIMEOUT = 5.0

# The quarterly disclosure lags the quarter end by a few weeks. Asking for the
# quarter that just ended usually returns the previous one, which the parser
# detects from the page's own 截止至 date.
_QUARTER_MONTHS = (3, 6, 9, 12)


def _get(url: str, *, params: dict | None = None, referer: str | None = None) -> str:
    """GET an Eastmoney endpoint through the shared transport.

    Spacing, the per-host cooldown and the browser fingerprint all live in
    ``vendors.transport`` because the CN price vendor hits the same edge.
    """
    return transport.get(url, params=params, referer=referer, timeout=_TIMEOUT, browser=True)


def eastmoney_secid(ticker: str) -> str | None:
    """Eastmoney's market-qualified id (``1.510300`` / ``0.159919``).

    Eastmoney prefixes Shanghai with ``1`` and Shenzhen with ``0``; the suffix
    of the Yahoo symbol is what decides, so no lookup is needed.
    """
    canonical = normalize_symbol(ticker)
    if canonical.endswith(".SS"):
        return f"1.{canonical[:-3]}"
    if canonical.endswith(".SZ"):
        return f"0.{canonical[:-3]}"
    return None


def stock_secid(code: str) -> str:
    """Eastmoney id for an A-share constituent, from its bare 6-digit code.

    A disclosed holding carries no exchange suffix, so the code's own prefix
    decides the market: Shanghai listings start with 5 (funds), 6 (main board)
    or 9 (B shares); Shenzhen listings start with 0, 1 or 3.
    """
    return f"1.{code}" if code[:1] in ("5", "6", "9") else f"0.{code}"


def _guard(ticker: str) -> str:
    """Resolve ``ticker`` to an A-share fund code or refuse it with a typed error."""
    canonical = normalize_symbol(ticker)
    code = etf_code(canonical)
    if code is None:
        raise NoMarketDataError(
            ticker, canonical,
            "not an A-share exchange-traded fund; the ETF data vendor covers "
            "Shanghai (.SS) and Shenzhen (.SZ) listed funds only",
        )
    return code


def _as_float(value) -> float | None:
    """A vendor numeric field as a float; ``"-"`` and blanks mean "not reported"."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None, unit: str = "", digits: int = 2) -> str:
    if value is None:
        return "n/a"
    text = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def _withhold_if_historical(canonical: str, as_of_date: str | None, what: str) -> str | None:
    """The notice a historical run gets instead of a present-day snapshot."""
    if not as_of_date or not is_historical(as_of_date):
        return None
    return (
        f"# {what} for {canonical}\n\n"
        f"{what} are withheld for this date: the vendor serves only the current "
        f"snapshot, with no filing date, so it cannot show what was public on "
        f"{as_of_date}. The price history for this run still comes from the "
        f"market data vendor."
    )


def _quote(secid: str, fields: str = _QUOTE_FIELDS) -> dict:
    body = _get(_QUOTE_URL, params={"secid": secid, "fltt": "2", "invt": "2",
                                    "fields": fields})
    data = transport.json_body(body, "Eastmoney").get("data") or {}
    if not data:
        raise NoMarketDataError(secid, secid, "no quote returned")
    return data


def _latest_nav(code: str) -> tuple[float, str] | None:
    """The most recent published unit NAV, from the fund's own data script.

    ``Data_netWorthTrend`` is a JSON array of ``{x: ms epoch, y: nav}``; the last
    element is the newest published NAV. ETFs publish NAV after the close, so on
    an intraday run this is the previous session's value — the premium it implies
    is still the right comparison (the price is live, the NAV is not).
    """
    try:
        script = _get(_FUND_JS_URL.format(code=code))
    except VendorUnavailableError:
        return None
    match = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", script, re.S)
    if not match:
        return None
    try:
        points = json.loads(match.group(1))
    except ValueError:
        return None
    if not points:
        return None
    last = points[-1]
    nav = _as_float(last.get("y"))
    stamp = _as_float(last.get("x"))
    if nav is None or stamp is None:
        return None
    nav_date = datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d")
    return nav, nav_date


def get_etf_profile(ticker: str, as_of_date: str | None = None) -> str:
    """A markdown profile of the fund: price, size, shares, NAV and premium."""
    canonical = normalize_symbol(ticker)
    code = _guard(ticker)
    withheld = _withhold_if_historical(canonical, as_of_date, "ETF profile")
    if withheld:
        return withheld

    secid = eastmoney_secid(canonical) or f"1.{code}"
    quote = _quote(secid)
    name = quote.get("f58") or code
    price = _as_float(quote.get("f43"))
    prev = _as_float(quote.get("f60"))
    change = _as_float(quote.get("f169"))
    pct = _as_float(quote.get("f170"))
    volume = _as_float(quote.get("f47"))          # lots (100 shares)
    amount = _as_float(quote.get("f48"))          # CNY
    shares = _as_float(quote.get("f84"))
    market_value = _as_float(quote.get("f116"))
    turnover = _as_float(quote.get("f168"))       # percent

    nav_pair = _latest_nav(code)
    nav, nav_date = nav_pair if nav_pair else (None, "")

    lines = [f"# ETF profile for {canonical} ({name})", ""]
    if price is not None:
        move = ""
        if change is not None and pct is not None:
            move = f" ({change:+.3f}, {pct:+.2f}%)"
        lines.append(f"- **Price**: {price:.3f} CNY{move}")
    if prev is not None:
        lines.append(f"- **Previous close**: {prev:.3f} CNY")
    if volume is not None and amount is not None:
        lines.append(f"- **Session volume**: {_fmt(volume / 1e4, ' 万手')} "
                     f"({_fmt(amount / 1e8, ' 亿元')} traded)")
    if shares is not None:
        lines.append(f"- **Shares outstanding**: {_fmt(shares / 1e8, ' 亿份')}")
    if market_value is not None:
        lines.append(f"- **Fund size (shares x price)**: {_fmt(market_value / 1e8, ' 亿元')}")
    if nav is not None:
        lines.append(f"- **Latest published NAV**: {nav:.4f} ({nav_date})")
        if price is not None and nav:
            premium = (price - nav) / nav * 100
            state = "premium" if premium > 0 else "discount"
            lines.append(f"- **Premium/discount vs latest NAV**: {premium:+.2f}% ({state})")
    if turnover is not None:
        lines.append(f"- **Turnover**: {turnover:.2f}%")

    lines += [
        "",
        "Notes:",
        "- The NAV above is the latest *published* one; intraday runs compare a "
        "live price with the previous close NAV, so the premium is an estimate, "
        "not a real-time IOPV.",
        "- Fund size is shares outstanding x market price; the fund's net assets "
        "use NAV instead and differ by the premium.",
    ]
    return "\n".join(lines)


def _holdings_rows(code: str, as_of_date: str | None = None, limit: int = 10,
                   kind: str = "jjcc") -> dict:
    """Disclosed holdings: ``{report_date, quarter, rows: [{code,name,weight}]}``.

    ``kind`` selects the disclosure: ``jjcc`` for stocks, ``zqcc`` for bonds.
    A bond or money-market ETF has no stock table at all, so the two are tried
    in that order by the caller.

    The page serves several quarters, each as an ``<h4>`` header (with its
    ``截止至`` date) followed by one table, newest first. A historical run asks
    for the year of its own last closed quarter and then picks the newest
    disclosed quarter that had closed by the run date; if none had, the caller
    withholds rather than leaking a quarter published later.
    """
    params = {"type": kind, "code": code, "topline": str(limit)}
    if as_of_date:
        year, month = _last_quarter_end(as_of_date)
        params.update({"year": str(year), "month": str(month)})

    body = _get(_HOLDINGS_URL, params=params, referer=_HOLDINGS_REFERER.format(code=code))
    headers = re.findall(r"<h4[^>]*>(.*?)</h4>", body, re.S)
    tables = re.findall(r"<table[^>]*>(.*?)</table>", body, re.S)

    entries = []
    for header, table in zip(headers, tables, strict=False):
        report_match = re.search(r"截止至：<font[^>]*>([\d-]+)</font>", header)
        quarter_match = re.search(r"(\d{4}年\d季度)", header)
        entries.append({
            "report_date": report_match.group(1) if report_match else "",
            "quarter": quarter_match.group(1) if quarter_match else "",
            "table": table,
        })
    if not entries:
        return {"report_date": "", "quarter": "", "rows": []}

    if as_of_date:
        usable = [entry for entry in entries
                  if entry["report_date"] and entry["report_date"] <= as_of_date]
        if not usable:
            # The vendor served no quarter closed by the run date: that is a
            # point-in-time gap, not an absence of holdings.
            return {"report_date": "", "quarter": "", "rows": [], "before_date": True}
        chosen = max(usable, key=lambda entry: entry["report_date"])
    else:
        chosen = entries[0]

    rows = []
    for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", chosen["table"], re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", raw_row, re.S)]
        if len(cells) < 4:
            continue
        pattern = r"\d{6}" if kind == "jjcc" else r"[0-9A-Za-z]{5,9}"
        code_cell = next((c for c in cells[1:3] if re.fullmatch(pattern, c)), "")
        if not code_cell:
            continue
        weight_cell = next((c for c in cells if c.endswith("%")), "")
        weight = _as_float(weight_cell.rstrip("%"))
        name_index = cells.index(code_cell) + 1
        name = cells[name_index] if name_index < len(cells) else ""
        if weight is None:
            continue
        rows.append({"code": code_cell, "name": name, "weight": weight})
    # ``topline`` is a page hint, not a hard cap: the newest quarter can come
    # back as the full disclosed list, so the limit is enforced here.
    return {"report_date": chosen["report_date"], "quarter": chosen["quarter"],
            "rows": rows[:limit]}


def _last_quarter_end(as_of_date: str) -> tuple[int, int]:
    """The (year, month) of the last quarter end on or before ``as_of_date``."""
    try:
        day = datetime.strptime(as_of_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        today = datetime.today()
        day = today
    for month in reversed(_QUARTER_MONTHS):
        if month <= day.month:
            return day.year, month
    return day.year - 1, 12


def get_etf_holdings(ticker: str, as_of_date: str | None = None, limit: int = 10) -> str:
    """The fund's disclosed top holdings and the weight they cover.

    Equity ETFs disclose stocks; bond and money-market ETFs disclose bonds
    instead, and commodity ETFs (gold, etc.) disclose neither because they hold
    the metal. The stock table is tried first, then the bond table, and a fund
    with neither gets a note rather than an error: that is a fact about the
    product, not a failure to fetch.
    """
    canonical = normalize_symbol(ticker)
    code = _guard(ticker)

    data = _holdings_rows(code, as_of_date, limit, "jjcc")
    label = "holdings"
    if not data["rows"] and not data.get("before_date"):
        bonds = _holdings_rows(code, as_of_date, limit, "zqcc")
        if bonds["rows"] or bonds.get("before_date"):
            data, label = bonds, "bond holdings"

    report_date = data["report_date"]
    if data.get("before_date"):
        return (
            f"# ETF holdings for {canonical}\n\n"
            f"Holdings are withheld for this date: no disclosed quarter had closed "
            f"by {as_of_date}."
        )
    if as_of_date and report_date and report_date > as_of_date:
        return (
            f"# ETF holdings for {canonical}\n\n"
            f"Holdings are withheld for this date: the newest disclosed quarter is "
            f"as of {report_date}, after {as_of_date}, so it could not have been "
            f"known then."
        )
    if not data["rows"]:
        return (
            f"# ETF holdings for {canonical}\n\n"
            "This fund discloses no stock or bond holdings: a commodity or "
            "money-market fund holds neither, so there is no basket to list. The "
            "fund profile still carries price, size and premium."
        )

    total = sum(row["weight"] for row in data["rows"])
    lines = [
        f"# ETF {label} for {canonical} ({data['quarter']}, as of {report_date})",
        "",
        "| # | Code | Name | Weight of NAV |",
        "|---:|---|---|---:|",
    ]
    for rank, row in enumerate(data["rows"], start=1):
        lines.append(f"| {rank} | {row['code']} | {row['name']} | {row['weight']:.2f}% |")
    lines += [
        "",
        f"Top {len(data['rows'])} {label} cover {total:.2f}% of NAV. This is a "
        "quarterly disclosure, not the live basket: weights drift with prices and "
        "with creations/redemptions.",
    ]
    return "\n".join(lines)


def _constituent_valuation(code: str) -> dict | None:
    """Market cap, PE and PB for one A-share constituent, or None if unavailable."""
    try:
        quote = _quote(stock_secid(code), _CONSTITUENT_FIELDS)
    except (NoMarketDataError, VendorUnavailableError):
        return None
    cap = _as_float(quote.get("f116"))
    pe = _as_float(quote.get("f162"))
    pb = _as_float(quote.get("f167"))
    name = quote.get("f58") or code
    if not cap or cap <= 0:
        return None
    return {"code": code, "name": name, "cap": cap, "pe": pe, "pb": pb,
            "industry": quote.get("f127") or ""}


def get_etf_index_fundamentals(ticker: str, as_of_date: str | None = None, limit: int = 10) -> str:
    """Valuation of the basket: aggregate PE/PB/ROE over the disclosed holdings.

    An ETF has no financial statements of its own, so its "fundamentals" are the
    weighted valuation of what it holds. The aggregation is capital-weighted, not
    an arithmetic mean of ratios:

        PE = sum(cap) / sum(cap / PE)   PB = sum(cap) / sum(cap / PB)
        ROE = PB / PE                   (earnings / book)

    Only constituents with a usable PE and PB enter the sums, and both the
    disclosed-weight coverage and the covered share are reported: the model must
    not read a top-10 aggregate as the whole index.
    """
    canonical = normalize_symbol(ticker)
    code = _guard(ticker)
    # The valuation multiples are a present-day snapshot, so a historical run
    # gets no aggregate: the holdings may be point-in-time, but the PE/PB are
    # not, and mixing them would leak today's valuation into a past decision.
    withheld = _withhold_if_historical(canonical, as_of_date, "ETF index fundamentals")
    if withheld:
        return withheld
    data = _holdings_rows(code, as_of_date, limit)

    report_date = data["report_date"]
    if as_of_date and report_date and report_date > as_of_date:
        return (
            f"# ETF index fundamentals for {canonical}\n\n"
            f"Withheld for this date: the newest disclosed holdings are as of "
            f"{report_date}, after {as_of_date}."
        )
    if not data["rows"]:
        # A bond, commodity or money-market fund holds no equities, so a basket
        # PE/PB does not exist for it. Say that, rather than reporting no data.
        return (
            f"# ETF index fundamentals for {canonical}\n\n"
            "Basket valuation does not apply: this fund discloses no equity "
            "holdings (a bond, commodity or money-market fund). Its return is "
            "driven by the yield curve, the metal price or money-market rates, "
            "not by a PE multiple."
        )

    covered = []
    for row in data["rows"]:
        valuation = _constituent_valuation(row["code"])
        if valuation:
            covered.append({**valuation, "weight": row["weight"]})
    if not covered:
        raise NoMarketDataError(ticker, canonical,
                                "no constituent valuation available for the disclosed holdings")

    cap_sum = sum(item["cap"] for item in covered)
    pe_caps = [(item["cap"], item["pe"]) for item in covered if item["pe"] and item["pe"] > 0]
    pb_caps = [(item["cap"], item["pb"]) for item in covered if item["pb"] and item["pb"] > 0]

    index_pe = cap_sum / sum(cap / pe for cap, pe in pe_caps) if pe_caps else None
    index_pb = cap_sum / sum(cap / pb for cap, pb in pb_caps) if pb_caps else None
    index_roe = index_pb / index_pe if index_pe and index_pb else None

    disclosed_weight = sum(row["weight"] for row in data["rows"])
    covered_weight = sum(item["weight"] for item in covered)

    lines = [
        f"# ETF index fundamentals for {canonical} (holdings as of {report_date})",
        "",
        "| Code | Name | Industry | Weight | Market cap (亿) | PE | PB |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for item in covered:
        pe = f"{item['pe']:.1f}" if item["pe"] and item["pe"] > 0 else "n/a"
        pb = f"{item['pb']:.2f}" if item["pb"] and item["pb"] > 0 else "n/a"
        industry = item["industry"] or "n/a"
        lines.append(
            f"| {item['code']} | {item['name']} | {industry} | {item['weight']:.2f}% | "
            f"{item['cap'] / 1e8:,.0f} | {pe} | {pb} |"
        )

    lines += ["", "**Capital-weighted aggregate over the covered constituents:**", ""]
    if index_pe:
        lines.append(f"- **Aggregate PE (TTM)**: {index_pe:.2f}")
    if index_pb:
        lines.append(f"- **Aggregate PB**: {index_pb:.2f}")
    if index_roe:
        lines.append(f"- **Implied ROE (PB/PE)**: {index_roe * 100:.2f}%")
    sectors: dict[str, float] = {}
    for item in covered:
        industry = item["industry"] or "Unknown"
        sectors[industry] = sectors.get(industry, 0.0) + item["weight"]
    if sectors:
        lines += ["", "### Industry exposure (covered constituents)", "",
                  "| Industry | Weight of NAV | Share of covered |", "|---|---:|---:|"]
        for industry, weight in sorted(sectors.items(), key=lambda kv: kv[1], reverse=True):
            share = weight / covered_weight * 100 if covered_weight else 0.0
            lines.append(f"| {industry} | {weight:.2f}% | {share:.1f}% |")
        lines.append("")
        lines.append("Industry labels are the vendor's own sub-industry names; the "
                     "weights cover only the disclosed, valued slice.")

    lines += [
        "",
        f"Coverage: the top {len(data['rows'])} disclosed holdings are "
        f"{disclosed_weight:.2f}% of NAV; constituents with a usable PE/PB are "
        f"{covered_weight:.2f}% of NAV ({len(covered)} of {len(data['rows'])}). "
        "The figures describe that covered slice only — they are not the whole "
        "index and must not be quoted as such.",
    ]
    return "\n".join(lines)
