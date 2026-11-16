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
from dataclasses import dataclass
from datetime import datetime

from tradingagents.dataflows import units
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.dates import parse_day
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.etf import etf_code
from tradingagents.dataflows.snapshot_equivalence import session_clock
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
# the run has other work, and Eastmoney has other hosts. The holdings page is
# the exception: it is the only route to the basket, its host is slow under
# load, and a five-second cutoff cost the ETF analyst its whole valuation block.
_TIMEOUT = 5.0
_HOLDINGS_TIMEOUT = 12.0

# The quarterly disclosure lags the quarter end by a few weeks. Asking for the
# quarter that just ended usually returns the previous one, which the parser
# detects from the page's own 截止至 date.
_QUARTER_MONTHS = (3, 6, 9, 12)


def _get(url: str, *, params: dict | None = None, referer: str | None = None,
         timeout: float | None = None) -> str:
    """GET an Eastmoney endpoint through the shared transport.

    Spacing, the per-host cooldown, the single retry and the browser
    fingerprint all live in ``vendors.transport`` because the CN price vendor
    hits the same edge. ``timeout`` lets the holdings page use its own budget.
    """
    return transport.get(url, params=params, referer=referer,
                         timeout=timeout or _TIMEOUT, browser=True)


def _is_timeout(exc: Exception) -> bool:
    """Whether a transport failure was a timeout rather than a refused connection."""
    cause = exc.__cause__
    if isinstance(cause, (TimeoutError,)):
        return True
    try:
        import requests
        if isinstance(cause, requests.Timeout):
            return True
    except ImportError:  # pragma: no cover - requests is a declared dependency
        pass
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


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


def _point_in_time(canonical: str, as_of_date: str | None) -> tuple[bool, str]:
    """Whether the present-day snapshot may be served, and the note to head it with.

    A run dated today is served as before. A run dated in the past is served
    only when the market clock proves no session closed after its date — the
    snapshot is then equivalent to what was public then, and the note says so.
    Otherwise the caller withholds and explains the two possible reasons.
    """
    if not as_of_date or not is_historical(as_of_date):
        return True, ""
    clock = session_clock(canonical, as_of_date)
    as_of = parse_day(as_of_date)
    if as_of is None:
        return False, ""
    if clock.equivalent_for(as_of):
        return True, f"> {clock.release_note(as_of)}"
    return False, ""


def _structural_notice(
    what: str, canonical: str, as_of_date: str, clock, detail: str
) -> str:
    """A withheld present-day snapshot whose values *are* shown, clearly dated.

    ``STRUCTURALLY_UNAVAILABLE`` says the vendor has no historical vintage, not
    that the request failed. The nearest snapshot and the gap let the analyst
    judge whether reading present-day figures is defensible; the header and the
    trailing warning say they are not values the run could have seen.
    """
    as_of = parse_day(as_of_date)
    gap = clock.gap_text(as_of) if as_of is not None else "an unknown distance after it"
    return (
        f"# {what} for {canonical}\n"
        f"# Point-in-time as of: {as_of_date}\n\n"
        f"STRUCTURALLY_UNAVAILABLE: {what} are withheld for {as_of_date}. The "
        f"vendor serves only the current snapshot, with no filing date, so it "
        f"cannot show what was public on {as_of_date}. This is a vendor limit, "
        f"not a network failure.\n\n"
        f"Nearest available snapshot: the current live vendor snapshot; its newest "
        f"closed session is {clock.last_closed.isoformat() if clock.last_closed else 'unknown'} "
        f"({gap}).\n\n"
        f"{detail}\n"
        f"Do not present any snapshot figure above as information available on "
        f"{as_of_date}; the price history for this run still comes from the "
        f"market data vendor."
    )


def _data_unavailable(what: str, canonical: str, as_of_date: str, exc: Exception) -> str:
    """A withheld snapshot whose nearest-snapshot read failed: not an absence claim."""
    return (
        f"# {what} for {canonical}\n"
        f"# Point-in-time as of: {as_of_date}\n\n"
        f"DATA_UNAVAILABLE: {what} are withheld for {as_of_date}, but the nearest "
        f"snapshot could not be read ({type(exc).__name__}: {str(exc)[:200]}). This "
        f"is a failed request, not an absence of the data and not a point-in-time "
        f"judgement; do not estimate or fabricate values."
    )


@dataclass(frozen=True)
class ProfileMetrics:
    """One quote's profile figures, read once and rendered by both branches."""

    code: str
    name: str
    price: float | None
    prev: float | None
    change: float | None
    pct: float | None
    volume: float | None
    amount: float | None
    shares: float | None
    market_value: float | None
    turnover: float | None
    nav: float | None
    nav_date: str


def _premium(price: float | None, nav: float | None) -> tuple[float, str] | None:
    """The premium/discount a price implies against a published NAV."""
    if price is None or not nav:
        return None
    premium = (price - nav) / nav * 100
    return premium, ("premium" if premium > 0 else "discount")


def _profile_metrics(code: str, secid: str) -> ProfileMetrics:
    """The profile's numbers, read once so the served and withheld blocks agree."""
    quote = _quote(secid)
    nav_pair = _latest_nav(code)
    nav, nav_date = nav_pair if nav_pair else (None, "")
    return ProfileMetrics(
        code=code,
        name=quote.get("f58") or code,
        price=_as_float(quote.get("f43")),
        prev=_as_float(quote.get("f60")),
        change=_as_float(quote.get("f169")),
        pct=_as_float(quote.get("f170")),
        volume=_as_float(quote.get("f47")),      # lots (100 shares)
        amount=_as_float(quote.get("f48")),      # CNY
        shares=_as_float(quote.get("f84")),
        market_value=_as_float(quote.get("f116")),
        turnover=_as_float(quote.get("f168")),   # percent
        nav=nav,
        nav_date=nav_date,
    )


def _profile_snapshot_lines(metrics: ProfileMetrics) -> list[str]:
    """Current profile figures for a withheld run's nearest-snapshot block.

    It renders the same metrics the served profile would, so a report never
    carries two snapshots (and two share counts) taken seconds apart.
    """
    price, nav = metrics.price, metrics.nav
    lines = [f"Snapshot values for {metrics.code} ({metrics.name}):"]
    if price is not None:
        lines.append(f"- **Price**: {price:.3f} CNY")
    if metrics.volume is not None and metrics.amount is not None:
        lines.append(f"- **Session volume**: {units.lots(metrics.volume)} "
                     f"({units.money(metrics.amount)} traded)")
    if metrics.shares is not None:
        lines.append(f"- **Shares outstanding**: {units.shares(metrics.shares)}")
    if metrics.market_value is not None:
        lines.append(f"- **Fund size (shares x price)**: {units.money(metrics.market_value)}")
    if nav is not None:
        lines.append(f"- **Latest published NAV**: {nav:.4f} ({metrics.nav_date})")
        premium = _premium(price, nav)
        if premium is not None:
            value, state = premium
            lines.append(f"- **Premium/discount vs that NAV**: {value:+.2f}% ({state})")
    return lines



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
    secid = eastmoney_secid(canonical) or f"1.{code}"

    # One snapshot read per call: the served block and the withheld notice both
    # render these metrics, so they cannot disagree on shares, size or volume.
    try:
        metrics = _profile_metrics(code, secid)
    except Exception as exc:  # noqa: BLE001
        if as_of_date and is_historical(as_of_date):
            return _data_unavailable("ETF profile", canonical, as_of_date, exc)
        raise

    serve, note = _point_in_time(canonical, as_of_date)
    if not serve:
        clock = session_clock(canonical, as_of_date or "")
        detail = "\n".join(_profile_snapshot_lines(metrics))
        return _structural_notice("ETF profile", canonical, as_of_date or "", clock, detail)
    name = metrics.name
    price, prev = metrics.price, metrics.prev
    change, pct = metrics.change, metrics.pct
    volume, amount = metrics.volume, metrics.amount
    shares, market_value = metrics.shares, metrics.market_value
    turnover = metrics.turnover
    nav, nav_date = metrics.nav, metrics.nav_date

    lines = [f"# ETF profile for {canonical} ({name})"]
    if note:
        lines.append(note)
    lines.append("")
    if price is not None:
        move = ""
        if change is not None and pct is not None:
            move = f" ({change:+.3f}, {pct:+.2f}%)"
        lines.append(f"- **Price**: {price:.3f} CNY{move}")
    if prev is not None:
        lines.append(f"- **Previous close**: {prev:.3f} CNY")
    if volume is not None and amount is not None:
        lines.append(f"- **Session volume**: {units.lots(volume)} "
                     f"({units.money(amount)} traded)")
    if shares is not None:
        lines.append(f"- **Shares outstanding**: {units.shares(shares)}")
    if market_value is not None:
        lines.append(f"- **Fund size (shares x price)**: {units.money(market_value)}")
    if nav is not None:
        lines.append(f"- **Latest published NAV**: {nav:.4f} ({nav_date})")
        premium = _premium(price, nav)
        if premium is not None:
            value, state = premium
            lines.append(f"- **Premium/discount vs latest NAV**: {value:+.2f}% ({state})")
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

    body = _get(_HOLDINGS_URL, params=params, referer=_HOLDINGS_REFERER.format(code=code),
                timeout=_HOLDINGS_TIMEOUT)
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
    day = parse_day(as_of_date) or datetime.today().date()
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


def _covered_valuation(rows: list[dict]) -> dict | None:
    """The capital-weighted aggregate over the disclosed rows, or None if none valued.

    Only constituents with a usable PE and PB enter the sums:
    ``PE = sum(cap) / sum(cap / PE)``, ``PB = sum(cap) / sum(cap / PB)`` and
    ``ROE = PB / PE``. The result also carries the disclosed and covered weights,
    so the caller can state how much of the fund the figure describes.
    """
    covered = []
    for row in rows:
        valuation = _constituent_valuation(row["code"])
        if valuation:
            covered.append({**valuation, "weight": row["weight"]})
    if not covered:
        return None

    cap_sum = sum(item["cap"] for item in covered)
    pe_caps = [(item["cap"], item["pe"]) for item in covered if item["pe"] and item["pe"] > 0]
    pb_caps = [(item["cap"], item["pb"]) for item in covered if item["pb"] and item["pb"] > 0]

    index_pe = cap_sum / sum(cap / pe for cap, pe in pe_caps) if pe_caps else None
    index_pb = cap_sum / sum(cap / pb for cap, pb in pb_caps) if pb_caps else None
    index_roe = index_pb / index_pe if index_pe and index_pb else None
    return {
        "covered": covered,
        "pe": index_pe,
        "pb": index_pb,
        "roe": index_roe,
        "disclosed_weight": sum(row["weight"] for row in rows),
        "covered_weight": sum(item["weight"] for item in covered),
    }


def _index_valuation_summary(canonical: str, as_of_date: str | None) -> str:
    """The dated CSI index multiple, or "" when the second source cannot answer."""
    try:
        from tradingagents.dataflows.vendors.csindex import point_in_time_summary

        return point_in_time_summary(canonical, as_of_date)
    except Exception as exc:  # noqa: BLE001 - the notice is still useful without it
        logger.info("ETF Analyst: no dated index valuation for %s: %s", canonical, exc)
        return ""


def _valuation_snapshot_lines(code: str, limit: int) -> list[str]:
    """Present-day basket valuation, for the nearest-snapshot block of a withheld run."""
    current = _holdings_rows(code, None, limit)
    if not current["rows"]:
        return ["Snapshot values: the vendor discloses no equity holdings for this fund."]
    aggregate = _covered_valuation(current["rows"])
    if aggregate is None:
        return ["Snapshot values: no constituent valuation could be read."]
    lines = [
        f"Snapshot values over the newest disclosed holdings "
        f"({current.get('quarter') or 'current quarter'}, as of {current['report_date']}):"
    ]
    if aggregate["pe"]:
        lines.append(f"- **Aggregate PE (TTM)**: {aggregate['pe']:.2f}")
    if aggregate["pb"]:
        lines.append(f"- **Aggregate PB**: {aggregate['pb']:.2f}")
    if aggregate["roe"]:
        lines.append(f"- **Implied ROE (PB/PE)**: {aggregate['roe'] * 100:.2f}%")
    lines.append(
        f"- **Coverage**: disclosed rows are {aggregate['disclosed_weight']:.2f}% of NAV; "
        f"valued rows are {aggregate['covered_weight']:.2f}% of NAV"
    )
    return lines


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
    serve, note = _point_in_time(canonical, as_of_date)
    if not serve:
        clock = session_clock(canonical, as_of_date or "")
        try:
            detail = "\n".join(_valuation_snapshot_lines(code, limit))
        except Exception as exc:  # noqa: BLE001 - report the failed read, don't end the run
            return _data_unavailable("ETF index fundamentals", canonical, as_of_date or "", exc)
        anchor = _index_valuation_summary(canonical, as_of_date)
        if anchor:
            detail = f"{detail}\n\n{anchor}"
        return _structural_notice(
            "ETF index fundamentals", canonical, as_of_date or "", clock, detail
        )

    try:
        data = _holdings_rows(code, as_of_date, limit)
    except VendorUnavailableError as exc:
        if _is_timeout(exc):
            raise VendorUnavailableError(
                f"fundf10 valuation request timed out after {_HOLDINGS_TIMEOUT:.0f}s "
                f"(retried once): {exc}"
            ) from exc
        raise

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
        # A timeout is a different answer and was raised above, not described here.
        return (
            f"# ETF index fundamentals for {canonical}\n\n"
            "Basket valuation does not apply: this fund discloses no equity "
            "holdings (a bond, commodity or money-market fund). Its return is "
            "driven by the yield curve, the metal price or money-market rates, "
            "not by a PE multiple. (The vendor returned no equity rows; this is "
            "not a timeout.)"
        )

    aggregate = _covered_valuation(data["rows"])
    if aggregate is None:
        raise NoMarketDataError(ticker, canonical,
                                "no constituent valuation available for the disclosed holdings")
    covered = aggregate["covered"]
    index_pe, index_pb, index_roe = aggregate["pe"], aggregate["pb"], aggregate["roe"]
    disclosed_weight = aggregate["disclosed_weight"]
    covered_weight = aggregate["covered_weight"]

    lines = [f"# ETF index fundamentals for {canonical} (holdings as of {report_date})"]
    if note:
        lines.append(note)
    lines += [
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
