"""What every Yahoo request shares: its retry, how a failure or an empty answer is
reported, and how a price is written out.

A rate limit is remembered for the rest of the process: once Yahoo answers HTTP 429,
every later request fails fast as ``VendorUnavailableError`` instead of sleeping
through a backoff again. A successful request clears that memory, and
``reset_rate_limit_state()`` clears it explicitly (tests and long-lived callers)."""

import logging
import math
import time

import yfinance as yf
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable

logger = logging.getLogger(__name__)

YAHOO_HOST = "https://query2.finance.yahoo.com"

# A 429 gets one retry, briefly. Yahoo throttles per process for minutes, so the old
# exponential backoff only repeated the same answer seven calls into a run; past the
# first 429 the caller waits for the vendor to come back, not for this call.
RATE_LIMIT_RETRIES = 1
RATE_LIMIT_MAX_DELAY = 1.0

# Once Yahoo has answered 429 in this process it is throttled for the whole run, so
# later requests are skipped rather than retried. Set on the first 429, cleared by a
# successful request or by ``reset_rate_limit_state()``.
_rate_limit_reason = None
_rate_limit_logged = False


def reset_rate_limit_state() -> None:
    """Forget that Yahoo rate limited this process.

    Clears the process-level flag and re-arms the one-shot retry warning, so a test
    or a long-lived caller can make the next request attempt the network again.
    Production callers do not need this: a successful request clears the flag itself.
    """
    global _rate_limit_reason, _rate_limit_logged
    _rate_limit_reason = None
    _rate_limit_logged = False


def price_text(value: float) -> str:
    """A price with six significant digits and at least two decimals.

    Fixed decimals print a sub-cent coin as 0.00 and a currency pair without its
    pips; this keeps both readable.
    """
    if not math.isfinite(value):
        return str(value)
    decimals = 2 if value == 0 else max(2, 5 - math.floor(math.log10(abs(value))))
    whole, _, fraction = f"{value:.{decimals}f}".partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def raise_for_empty(symbol: str, canonical: str, what: str) -> None:
    """Report an empty Yahoo answer as an absence, or as an outage if it is one.

    An empty answer from a Yahoo that cannot be reached is not an answer about
    the symbol, so the host is probed before "this symbol has no {what}" is said.
    """
    if not vendor_reachable(YAHOO_HOST):
        raise VendorUnavailableError(f"Yahoo Finance is unreachable; no {what} was retrieved")
    raise NoMarketDataError(symbol, canonical, f"no {what}")


# yfinance answers some failed requests with an empty result, which would read as
# "no data" for a symbol nobody checked; raised, the failure is reported as one.
yf.config.debug.hide_exceptions = False


def _answered_empty(exc: Exception) -> bool:
    """Whether Yahoo answered that it has nothing: no such symbol (HTTP 404), or a
    price window with no prices in it.

    yfinance raises YFPricesMissingError for that answer and also for an answer
    that was an error (an error status, or Yahoo describing a failure), so only
    a chart with no prices, or Yahoo saying the data does not exist, counts.
    A missing time zone is not an answer: yfinance reports a failed lookup the same way.
    """
    if isinstance(exc, YFPricesMissingError):
        reason = exc.yahoo_reason
        return "status_code" not in (exc.debug_info or "") and (
            reason is None or reason.startswith("Data doesn't exist"))
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def yf_retry(func, max_retries=RATE_LIMIT_RETRIES, base_delay=2.0):
    """Execute a yfinance call, retrying a rate limit once and remembering it.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not retry them
    internally, so this wrapper retries them at most once, after
    ``min(base_delay, RATE_LIMIT_MAX_DELAY)`` seconds (``max_retries`` is capped at
    ``RATE_LIMIT_RETRIES``). Yahoo throttles per process, so the 429 is then recorded
    for the rest of the process: every later call raises VendorUnavailableError
    immediately, without touching the network or sleeping, until a request succeeds
    or ``reset_rate_limit_state()`` is called. Any other exception, or a rate limit
    that outlasts the retry, is raised as VendorUnavailableError: it failed in
    transit and says nothing about the symbol. Yahoo answering that it has nothing
    for the symbol returns None, an empty answer. ``func`` should build its own
    Ticker and make the request itself: a Ticker keeps a failed ``info`` fetch as
    done, so asking the same one again reads an empty profile.
    """
    global _rate_limit_reason, _rate_limit_logged

    if _rate_limit_reason is not None:
        logger.debug("Skipping the Yahoo request: %s", _rate_limit_reason)
        raise VendorUnavailableError(_rate_limit_reason)

    retries = min(max_retries, RATE_LIMIT_RETRIES)
    for attempt in range(retries + 1):
        try:
            result = func()
        except YFRateLimitError as exc:
            _rate_limit_reason = (
                "Yahoo Finance is rate limited in this process: HTTP 429 was already "
                "observed, so the request was skipped without retrying"
            )
            if attempt < retries:
                delay = min(base_delay, RATE_LIMIT_MAX_DELAY)
                if not _rate_limit_logged:
                    logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{retries})")
                    _rate_limit_logged = True
                else:
                    logger.debug(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{retries})")
                time.sleep(delay)
            else:
                raise VendorUnavailableError(
                    f"Yahoo Finance rate limited after {retries} retries: {exc}"
                ) from exc
        except Exception as exc:
            if _answered_empty(exc):
                return None
            raise VendorUnavailableError(f"Yahoo Finance request failed: {type(exc).__name__}") from exc
        else:
            _rate_limit_reason = None
            return result
