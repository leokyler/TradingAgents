from enum import StrEnum


class AnalystType(StrEnum):
    MARKET = "market"
    # Fund-level analysis: profile, holdings and basket valuation. Offered only
    # for ETFs; a stock run has no fund to describe.
    ETF = "etf"
    # Wire value stays "social" for saved-config and string-keyed-caller
    # back-compat; the user-facing label is "Sentiment Analyst".
    SOCIAL = "social"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"


class AssetType(StrEnum):
    STOCK = "stock"
    # Exchange-traded fund: a basket, analyzed with fund-level data instead of
    # company filings. Detected from A-share fund code ranges.
    ETF = "etf"
    CRYPTO = "crypto"
