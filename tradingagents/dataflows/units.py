"""Unit wording for the deterministic data blocks.

The data blocks are model input, not model output, so their labels stay in
English (Price, Session volume, ...) while the *units* follow the run's output
language, the same setting ``get_language_instruction`` reads. One helper per
unit keeps the served profile and a withheld run's nearest-snapshot block from
drifting into two different notations.
"""

from __future__ import annotations


def is_chinese() -> bool:
    """Whether the run's configured output language is Chinese."""
    from tradingagents.dataflows.config import get_config

    language = str(get_config().get("output_language") or "").strip().lower()
    return "中文" in language or language.startswith("zh") or "chinese" in language


def lots(value: float) -> str:
    """A share volume in lots (100 shares)."""
    return f"{value / 1e4:,.2f} 万手" if is_chinese() else f"{value / 1e4:,.2f} 10k lots"


def shares(value: float) -> str:
    """A share count."""
    return f"{value / 1e8:,.2f} 亿份" if is_chinese() else f"{value / 1e8:,.2f} 100m shares"


def money(value: float) -> str:
    """A CNY amount."""
    return f"{value / 1e8:,.2f} 亿元" if is_chinese() else f"CNY {value / 1e8:,.2f} 100m"
