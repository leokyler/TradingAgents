"""Per-asset-type wording, in one table.

The pipeline carries the asset type as a plain string in graph state, and every
prompt that names the instrument used to spell out the same conditional. One
asset type now means one row here, not another ternary in five prompts: the
stock wording is the default, a fund is a basket rather than a company, and a
crypto asset has no filings at all.

Kept separate from `context.py` so the prompts that need a label do not pull in
the instrument-identity machinery.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetProfile:
    """How one asset type is described to the agents."""

    # "instrument" / "fund" / "asset": the noun in "the X to analyze".
    instrument_label: str
    # "Company" / "Fund" / "Name": the label before a resolved name.
    name_label: str
    # "stock" / "fund" / "asset": the noun in "the case against investing in the X".
    target_label: str
    # "company" / "fund" / "asset": the noun in "news about the X".
    news_label: str
    # What the company-fundamentals slot holds for this asset type.
    fundamentals_label: str
    # An extra paragraph appended to the instrument context, or "" for none.
    guidance: str


_ETF_GUIDANCE = (
    " This instrument is an exchange-traded fund: a basket of securities held "
    "in a fund, not a company. It files no company financial statements and has "
    "no insiders or earnings call. Analyze the tracked index, the valuation of "
    "the basket, the fund's size and liquidity, and the premium or discount to "
    "NAV; do not look for company fundamentals that do not exist."
)

_CRYPTO_GUIDANCE = (
    " Treat it as a crypto asset rather than a company, and do not "
    "assume company fundamentals are available."
)


PROFILES: dict[str, AssetProfile] = {
    "stock": AssetProfile(
        instrument_label="instrument",
        name_label="Company",
        target_label="stock",
        news_label="company",
        fundamentals_label="Company fundamentals report",
        guidance="",
    ),
    "etf": AssetProfile(
        instrument_label="fund",
        name_label="Fund",
        target_label="fund",
        news_label="fund",
        # The slot is still filled — the fundamentals analyst reports that it
        # has nothing to file — so the label says why instead of hiding it.
        fundamentals_label=(
            "Company fundamentals report (not applicable: a fund files none)"
        ),
        guidance=_ETF_GUIDANCE,
    ),
    "crypto": AssetProfile(
        instrument_label="asset",
        name_label="Name",
        target_label="asset",
        news_label="asset",
        fundamentals_label="Asset fundamentals report (may be unavailable for crypto)",
        guidance=_CRYPTO_GUIDANCE,
    ),
}

_DEFAULT = PROFILES["stock"]


def profile_for(asset_type: str | None) -> AssetProfile:
    """The profile for ``asset_type``, falling back to the stock wording."""
    return PROFILES.get((asset_type or "stock").lower(), _DEFAULT)
