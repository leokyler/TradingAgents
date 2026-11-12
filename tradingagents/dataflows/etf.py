"""ETF identification and A-share fund-code conventions.

The rest of the data layer assumes a single listed equity: statements, insiders,
retail cashtags. An exchange-traded fund is a basket, so those inputs do not
exist and the pipeline switches to fund-level data (see
``dataflows.vendors.eastmoney``) and a fund-aware analyst.

Identification is syntactic on purpose. Yahoo classifies Chinese ETFs as
``quoteType=EQUITY`` and returns "No Fund data found" for them, so neither
``quoteType`` nor ``funds_data`` can tell an A-share ETF from a stock; the code
ranges below can, with no network call.

Code ranges (2026):
    Shanghai (``.SS``): 51xxxx, 56xxxx, 58xxxx   (510300, 588000, 561300, ...)
    Shenzhen (``.SZ``): 159xxx                    (159919, 159915, ...)

A bare 6-digit code carries no exchange, so it is not treated as an ETF: the
suffix decides which market the vendor is asked about.
"""

from __future__ import annotations

from tradingagents.dataflows.symbols import normalize_symbol

# Fund-product prefixes by exchange. 51x = ETF (equity/bond/gold/cross-border),
# 56x/58x = newer Shanghai ETF ranges (STAR 50 and sector funds).
_SH_FUND_PREFIXES = ("51", "56", "58")
# 159xxx = Shenzhen ETF. LOFs (16xxxx) are deliberately excluded: they trade
# like closed-end funds and their pricing is not an index basket.
_SZ_FUND_PREFIXES = ("159",)


def etf_code(ticker: str) -> str | None:
    """The 6-digit fund code for an A-share ETF, or ``None`` for anything else.

    ``600519.SS`` (a stock) and ``SPY`` (a US ETF) both return ``None``: the
    first is excluded by prefix, the second by exchange.
    """
    canonical = normalize_symbol(ticker)
    if canonical.endswith(".SS"):
        code = canonical[:-3]
        return code if code.startswith(_SH_FUND_PREFIXES) else None
    if canonical.endswith(".SZ"):
        code = canonical[:-3]
        return code if code.startswith(_SZ_FUND_PREFIXES) else None
    return None


def is_a_share_etf(ticker: str) -> bool:
    """Whether ``ticker`` is a Shanghai/Shenzhen listed fund."""
    return etf_code(ticker) is not None


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

    Shanghai codes start with 6 (main board), 5 (funds) or 9 (B shares);
    Shenzhen codes start with 0 or 3. The rule mirrors ``eastmoney_secid`` but
    works on the codes a holdings file reports, which carry no exchange suffix.
    """
    return f"1.{code}" if code[:1] in ("5", "6", "9") else f"0.{code}"
