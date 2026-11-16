"""Unit wording for the deterministic data blocks.

The data blocks are model input, not model output, so their labels stay in
English (Price, Session volume, ...) while the *units* follow the run's output
language, the same setting ``get_language_instruction`` reads. One helper per
unit keeps the served profile and a withheld run's nearest-snapshot block from
drifting into two different notations.
"""

from __future__ import annotations


def is_chinese(language: str | None = None) -> bool:
    """Whether the run's configured output language is Chinese.

    ``language`` lets a caller that already has the selection use it without
    reading the data-layer config (which the CLI sets only after the graph is
    built).
    """
    if language is None:
        from tradingagents.dataflows.config import get_config

        language = get_config().get("output_language")
    text = str(language or "").strip().lower()
    return "中文" in text or text.startswith("zh") or "chinese" in text


def lots(value: float | None) -> str:
    """A turnover volume in exchange lots (100 shares)."""
    if value is None:
        return "n/a"
    return f"{value / 1e4:,.2f} 万手" if is_chinese() else f"{value / 1e4:,.2f} 10k lots"


def shares(value: float | None) -> str:
    """A share count."""
    if value is None:
        return "n/a"
    return f"{value / 1e8:,.2f} 亿份" if is_chinese() else f"{value / 1e8:,.2f} 100m shares"


def money(value: float | None) -> str:
    """A CNY amount."""
    if value is None:
        return "n/a"
    return f"{value / 1e8:,.2f} 亿元" if is_chinese() else f"CNY {value / 1e8:,.2f} 100m"
