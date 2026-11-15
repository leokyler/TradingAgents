"""Keyless A-share news, from Eastmoney's public interfaces via ``akshare``.

The Yahoo news vendor (``vendors/yahoo/news.py``) is the framework's default,
but it throttles anonymous clients and covers Chinese listings thinly. This
vendor serves the same two shapes as the Yahoo one — ticker news and global /
macro news — for Shanghai, Shenzhen and Beijing listings, so the news analyst
reads one block format whichever vendor answered:

    akshare.stock_news_em(symbol)         Eastmoney per-symbol news (latest items)
    akshare.stock_info_global_em()        Eastmoney global financial flashes
    akshare.stock_info_global_cls()       财联社 flashes (fallback only)

The global-flash names differ by akshare version (1.20 renames them
``stock_news_global_em`` / ``stock_news_global_cls``); the vendor resolves
either name and reports the feed it used.

Keyless, but *optional*: ``akshare`` is not a core dependency (it pulls a
mini-racer download), so it is imported inside each call and never at module
import. A plain install therefore pays nothing for this vendor, and a missing
install is reported as ``VendorNotConfiguredError`` naming
``pip install "tradingagents[cn]"``, so the router skips to the next vendor
instead of the run failing.

Point-in-time rules match the rest of the data layer:

- both entry points gate on the canonical symbol *before* importing akshare, so
  a US/HK/crypto run never touches the library or its endpoints;
- publish times are China local time (Asia/Shanghai) and are usually naive;
  they are interpreted as Shanghai time and converted to UTC-aware, then kept
  only when the article's Shanghai calendar day is inside the requested window.
  Comparing China-local days rather than ``date_window.in_window``'s UTC days
  is stricter and matches how an A-share reader reads "news up to 2026-11-13":
  a flash at 07:59 China time the day after the end date is after the analysis
  date, even though it is still 23:59 UTC of the end date;
- an article with no publish time is dropped rather than dated "now", and a
  window with nothing in it raises ``NoMarketDataError`` rather than claiming
  the absence of news: ``stock_news_em`` returns only the latest items, so an
  empty historical window means the feed cannot see that window, not that no
  news exists. A configured next vendor then gets its chance.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.dates import require_day
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

# The interfaces stamp China local time, with no offset.
SHANGHAI = ZoneInfo("Asia/Shanghai")

# What the A-share vendor serves; anything else must fall through to the
# configured next vendor (Yahoo) without a request.
_A_SHARE_MARKETS = ("SS", "SZ", "BJ")

_CN_PIP_HINT = 'pip install "tradingagents[cn]"'

# Eastmoney's field names, with the names other akshare versions use.
_TITLE_KEYS = ("新闻标题", "标题")
_SUMMARY_KEYS = ("新闻内容", "内容", "摘要")
_TIME_KEYS = ("发布时间",)
_DATE_KEYS = ("发布日期",)
_SOURCE_KEYS = ("文章来源", "来源")
_LINK_KEYS = ("新闻链接", "链接")

# Publish timestamps as the interfaces spell them: full datetimes for
# per-symbol news and Eastmoney flashes, a bare time for 财联社's feed (whose
# date arrives in a separate column).
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%H:%M:%S",
    "%H:%M",
)

# Eastmoney's article search (the endpoint stock_news_em itself calls). It is
# used for the global feed's fallback because the 7x24 flash endpoint keeps only
# the latest hours: a historical as_of date would otherwise have no macro news
# at all, even though the search index still has those days. The query words are
# the "要闻/宏观" route the specification allows; the response is the same
# article JSON, with English field names.
_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_GLOBAL_SEARCH_QUERIES = ("A股", "宏观", "要闻")

# The global-news interfaces, in preference order: Eastmoney's 7x24 flashes
# first, 财联社's telegraph only when the installed akshare names it differently
# or has dropped Eastmoney's. Each tuple lists the names to look for and the
# source label to report for that feed.
_GLOBAL_FEEDS = (
    (("stock_info_global_em", "stock_news_global_em"), "Eastmoney"),
    (("stock_info_global_cls", "stock_news_global_cls"), "CLS"),
)


def _import_akshare():
    """The installed ``akshare`` module, imported on first use.

    Importing at module import would make an optional dependency mandatory for
    every run (import error on a plain install), so it happens here, where the
    failure is a typed "not configured" the router can skip.
    """
    try:
        import akshare
    except ImportError as exc:
        raise VendorNotConfiguredError(
            "cn_news vendor is not configured: the optional 'akshare' package is "
            f"not installed; install it with {_CN_PIP_HINT}"
        ) from exc
    except Exception as exc:  # an installed-but-broken akshare is an outage
        raise VendorUnavailableError(f"akshare failed to import: {exc}") from exc
    return akshare


def _a_share_code(symbol: str, reason: str) -> tuple[str, str]:
    """``(canonical, six-digit code)`` for an A-share listing, else raise.

    Runs before akshare is imported: a symbol this vendor cannot serve is a
    configuration fact, not a fetch worth making.
    """
    canonical = normalize_symbol(symbol)
    text = canonical if isinstance(canonical, str) else ""
    code, _, market = text.rpartition(".")
    if market not in _A_SHARE_MARKETS or len(code) != 6 or not code.isdigit():
        raise VendorNotConfiguredError(
            f"{reason}; {symbol!r} resolved to {canonical!r}, so this vendor is not "
            f"used and the configured next vendor serves the call"
        )
    return canonical, code


def _text(row: dict, keys: tuple[str, ...]) -> str:
    """The first non-empty field among ``keys``, as stripped text."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("nan", "nat", "none"):
            return text
    return ""


def _records(frame) -> list[dict]:
    """Rows from an akshare result (a DataFrame, or a list of dicts)."""
    if frame is None:
        return []
    if isinstance(frame, dict):
        return [frame]
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        rows = to_dict("records")
    elif isinstance(frame, (list, tuple)):
        rows = list(frame)
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _parse_local(value: str) -> datetime | None:
    """A timestamp the interface spelled, parsed as-is (naive means Shanghai)."""
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _published(row: dict) -> datetime | None:
    """The article's publish time as a UTC-aware datetime, or None when undated.

    Never substitutes "now": an article the interface did not date cannot be
    proven to belong to the analysis window, so the caller drops it.
    """
    raw = _text(row, _TIME_KEYS)
    if not raw:
        return None
    day = _text(row, _DATE_KEYS)
    if day and ":" in raw and "-" not in raw and "/" not in raw:
        raw = f"{day} {raw}"  # a feed that splits the date and the clock
    local = _parse_local(raw)
    if local is None:
        return None
    if local.tzinfo is None:
        local = local.replace(tzinfo=SHANGHAI)
    return local.astimezone(UTC)


def _in_local_window(published: datetime, start_day: date, end_day: date) -> bool:
    """Whether the article's China-local calendar day is inside the window.

    ``date_window.in_window`` closes a window at midnight UTC, which is 08:00
    the next day in Shanghai: a flash at 07:59 China time on the day after the
    analysis date would fall inside it, a future leak. This vendor compares the
    article's Shanghai date instead, which is also how an A-share reader reads
    "news up to 2026-11-13".
    """
    return start_day <= published.astimezone(SHANGHAI).date() <= end_day


def _article(row: dict, publisher_override: str) -> dict | None:
    """One formatted-article record, or None when the row cannot be used."""
    title = _text(row, _TITLE_KEYS)
    published = _published(row)
    if not title or published is None:
        return None
    return {
        "title": title,
        "summary": _text(row, _SUMMARY_KEYS),
        "publisher": _text(row, _SOURCE_KEYS) or publisher_override,
        "link": _text(row, _LINK_KEYS),
        "published": published,
    }


def _articles(rows: list[dict], publisher_override: str) -> list[dict]:
    """Usable article records, newest first."""
    articles = [a for row in rows if (a := _article(row, publisher_override)) is not None]
    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles


def _format_articles(articles: list[dict]) -> str:
    """The per-article block the Yahoo news vendor renders, plus its publish time."""
    out = ""
    for data in articles:
        out += f"### {data['title']} (source: {data['publisher']})\n"
        out += f"Published: {data['published'].astimezone(SHANGHAI):%Y-%m-%d %H:%M}\n"
        if data["summary"]:
            out += f"{data['summary']}\n"
        if data["link"]:
            out += f"Link: {data['link']}\n"
        out += "\n"
    return out


def _global_feed(akshare) -> tuple[str, object, str]:
    """``(name, callable, source label)`` for the first global feed this install has."""
    for names, label in _GLOBAL_FEEDS:
        for name in names:
            fetch = getattr(akshare, name, None)
            if callable(fetch):
                return name, fetch, label
    tried = ", ".join(name for names, _ in _GLOBAL_FEEDS for name in names)
    raise VendorUnavailableError(
        f"the installed akshare provides none of {tried}, so it has no A-share global news feed"
    )


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """News about ``ticker`` published in ``[start_date, end_date]``, from Eastmoney.

    Serves only Shanghai/Shenzhen/Beijing listings. The interface returns the
    latest items for the symbol, so the window is applied point-in-time on the
    article's China-local calendar day: an article dated after ``end_date`` in
    Shanghai is dropped, never served into the run.
    """
    canonical, code = _a_share_code(
        ticker, "cn_news serves Shanghai/Shenzhen/Beijing A-share listings only"
    )
    start_day = require_day(start_date, "start_date")
    end_day = require_day(end_date, "end_date")
    if start_day > end_day:
        raise ValueError(f"start date {start_date} is after end date {end_date}")

    akshare = _import_akshare()
    fetch = getattr(akshare, "stock_news_em", None)
    if not callable(fetch):
        raise VendorUnavailableError(
            f"the installed akshare ({(getattr(akshare, '__version__', 'unknown'))}) "
            f"provides no stock_news_em function"
        )
    try:
        frame = fetch(symbol=code)
    except Exception as exc:
        raise VendorUnavailableError(
            f"akshare stock_news_em failed for {canonical}: {exc}"
        ) from exc

    try:
        articles = _articles(_records(frame), "Eastmoney")
    except Exception as exc:  # a moved column or a non-table result is an outage
        raise VendorUnavailableError(
            f"akshare stock_news_em returned an unreadable table for {canonical}: {exc}"
        ) from exc
    in_range = [
        article
        for article in articles
        if _in_local_window(article["published"], start_day, end_day)
    ]
    in_range = in_range[: get_config()["news_article_limit"]]
    if not in_range:
        # The per-symbol feed serves only its latest ~10 items, so a historical
        # window can be empty even when the search index has the days. Ask the
        # same dated search the global feed uses before giving up; the window is
        # still applied on the China-local day, so nothing after it leaks.
        searched = _articles(_search_rows(code), "Eastmoney")
        in_range = [
            article for article in searched
            if _in_local_window(article["published"], start_day, end_day)
        ][: get_config()["news_article_limit"]]
    if not in_range:
        # An empty window is not an absence of news: the interfaces serve only
        # recent items, so a historical window is one they cannot observe.
        # Raising lets a configured next vendor answer.
        raise NoMarketDataError(
            ticker,
            canonical,
            f"no articles between {start_date} and {end_date}; Eastmoney's per-symbol "
            f"feed and dated search returned nothing in the window",
        )
    return (
        f"## {ticker} News, from {start_date} to {end_date}:\n\n"
        f"{_format_articles(in_range)}"
    )


def _clean_markup(value: object) -> str:
    """Eastmoney search wraps the query in <em> tags; strip them."""
    return re.sub(r"</?em>", "", str(value or "")).strip()


def _search_rows(keyword: str, pages: int = 2) -> list[dict]:
    """Dated articles from Eastmoney's keyword search, as the vendor's row dicts.

    Same public interface ``stock_news_em`` uses, with a larger page and a
    different keyword, so the global feed can reach days the 12-hour flash list
    no longer holds. Failures return no rows: the caller decides whether the
    flash feed or another vendor can still answer.
    """
    import json

    from tradingagents.dataflows.vendors import transport

    rows: list[dict] = []
    for page in range(1, pages + 1):
        inner = {
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": page,
                    "pageSize": 50,
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        params = {
            "cb": "jQuery35101792940631092459_1764599530165",
            "param": json.dumps(inner, ensure_ascii=False),
            "_": "1764599530176",
        }
        try:
            body = transport.get(_SEARCH_URL, params=params, timeout=10, browser=True)
            start = body.find("(") + 1
            payload = json.loads(body[start:body.rfind(")")])
            items = (payload.get("result") or {}).get("cmsArticleWebOld") or []
        except Exception as exc:  # noqa: BLE001 - one query failing is not the feed
            logger.debug("cn_news search %r page %s failed: %s", keyword, page, exc)
            continue
        for item in items:
            code = item.get("code") or ""
            rows.append({
                "新闻标题": _clean_markup(item.get("title")),
                "新闻内容": _clean_markup(item.get("content")),
                "发布时间": item.get("date"),
                "文章来源": item.get("mediaName") or "Eastmoney",
                "新闻链接": f"http://finance.eastmoney.com/a/{code}.html" if code else "",
            })
    return rows


def _select_in_window(articles: list[dict], start_day, end_day, limit: int) -> list[dict]:
    """In-window articles, newest first, de-duplicated by title, capped at ``limit``."""
    selected: list[dict] = []
    seen: set[str] = set()
    for article in articles:
        if not _in_local_window(article["published"], start_day, end_day):
            continue
        if article["title"] in seen:
            continue
        seen.add(article["title"])
        selected.append(article)
        if len(selected) >= limit:
            break
    return selected


def get_global_news(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
    symbol: str | None = None,
) -> str:
    """China macro / market flashes in ``[as_of_date - look_back_days, as_of_date]``.

    The window is read on China-local calendar days, like ``get_news``: a flash
    dated after ``as_of_date`` in Shanghai is dropped, whatever UTC day it falls
    in. Gated on ``symbol`` like ``get_news``: only an A-share run is served, so a
    US/HK/crypto run's configured chain falls through to yfinance without
    touching akshare. Defaults for the window and the count come from the run's
    config (``global_news_lookback_days``, ``global_news_article_limit``).
    """
    if symbol is None:
        raise VendorNotConfiguredError(
            "cn_news global news only serves A-share runs; no symbol was given, so "
            "this vendor is not used and the configured next vendor serves the call"
        )
    canonical, _code = _a_share_code(
        symbol, "cn_news global news only serves A-share runs"
    )

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    if limit <= 0:
        raise NoMarketDataError(
            symbol, canonical, "the global news limit is zero, so no article is served"
        )
    as_of_day = require_day(as_of_date, "as_of_date")
    start_day = as_of_day - relativedelta(days=look_back_days)
    start_date = start_day.strftime("%Y-%m-%d")

    # The optional akshare install is what makes this vendor configured at all;
    # the search fallback below uses the same public endpoint, not another key.
    akshare = _import_akshare()

    flash_error = None
    selected: list[dict] = []
    try:
        name, fetch, label = _global_feed(akshare)
        frame = fetch()
        articles = _articles(_records(frame), label)
        selected = _select_in_window(articles, start_day, as_of_day, limit)
    except Exception as exc:  # noqa: BLE001 - the dated search is the fallback
        flash_error = exc

    if not selected:
        # The 7x24 flash feed keeps only the latest hours, so a historical run
        # reaches the same Eastmoney search index the ticker feed uses. The
        # search is dated, so the window still holds: no article after
        # ``as_of_date`` (China-local) is served.
        rows: list[dict] = []
        for keyword in _GLOBAL_SEARCH_QUERIES:
            rows.extend(_search_rows(keyword))
        try:
            searched = _articles(rows, "Eastmoney")
        except Exception as exc:  # noqa: BLE001 - an unreadable table is an outage
            searched = []
            flash_error = flash_error or exc
        selected = _select_in_window(searched, start_day, as_of_day, limit)

    if not selected:
        if flash_error is not None:
            # The feed itself failed and the dated search could not replace it:
            # a transit failure, not an absence of news.
            raise VendorUnavailableError(
                f"akshare global news failed for {canonical}: {flash_error}; the dated "
                f"search fallback returned nothing"
            )
        raise NoMarketDataError(
            symbol,
            canonical,
            f"no global news between {start_date} and {as_of_date}; the flash feed "
            f"serves only its latest items and the dated search found none in the window",
        )
    return (
        f"## Global Market News, from {start_date} to {as_of_date}:\n\n"
        f"{_format_articles(selected[:limit])}"
    )
