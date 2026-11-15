"""The ETF analyst's price window: deterministic figures that survive a withheld profile.

The analyst used to receive only the three fund-level blocks. When the profile
was withheld, the report had no price, volume or drawdown at all. The price
window is now a fourth block, fetched from the market-data path and computed
here, so the model quotes numbers instead of calculating (or omitting) them.
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from tradingagents.agents.analysts import etf_analyst, etf_price
from tradingagents.dataflows import prices


def _frame(periods=100, end="2026-11-13", extra_future=False):
    dates = pd.bdate_range(end=end, periods=periods)
    closes = [1.0 + i * 0.01 for i in range(periods)]
    frame = pd.DataFrame({
        "Date": dates,
        "Open": closes,
        "High": [c + 0.02 for c in closes],
        "Low": [c - 0.02 for c in closes],
        "Close": closes,
        "Volume": [100_000_000] * periods,
    })
    if extra_future:
        frame = pd.concat([frame, pd.DataFrame({
            "Date": pd.to_datetime(["2026-11-14"]), "Open": [9.0], "High": [9.0],
            "Low": [9.0], "Close": [9.0], "Volume": [1],
        })], ignore_index=True)
    return frame


def _csv(frame, symbol="515070.SS", start="2026-05-01", end="2026-11-14"):
    return prices.render_ohlcv(frame, symbol, start, end, "test")


@pytest.mark.unit
class TestPriceWindow:
    def test_the_block_carries_every_deterministic_figure(self, monkeypatch):
        monkeypatch.setattr(etf_price, "get_cn_market_stock",
                            lambda *a, **k: _csv(_frame()))
        out = etf_price.build_price_window("515070.SS", "2026-11-13")

        assert "# Source: cn_market" in out
        assert "Latest close**: 1.990" in out
        # 1.99 / 1.94 - 1 = +2.58%; / 1.79 = +11.17%; / 1.39 = +43.17%
        assert "Change over 5 sessions**: +2.58%" in out
        assert "Change over 20 sessions**: +11.17%" in out
        assert "Change over 60 sessions**: +43.17%" in out
        assert "60-session high**: 2.010" in out
        assert "60-session low**: 1.380" in out
        assert "Drawdown from the 60-session high**: -1.00%" in out
        assert "CNY 1.90 100m" in out
        assert "estimated as close x volume" in out

    def test_a_bar_after_the_analysis_date_is_not_used(self, monkeypatch):
        monkeypatch.setattr(etf_price, "get_cn_market_stock",
                            lambda *a, **k: _csv(_frame(extra_future=True)))
        out = etf_price.build_price_window("515070.SS", "2026-11-13")

        assert "through the latest session 2026-11-13" in out
        assert "9.000" not in out   # the future bar's close

    def test_a_total_failure_is_a_placeholder_not_a_zero(self, monkeypatch):
        def refused(*a, **k):
            raise OSError("blocked")

        monkeypatch.setattr(etf_price, "get_cn_market_stock", refused)

        out = etf_price.build_price_window("515070.SS", "2026-11-13")

        assert out.startswith("<price window unavailable")
        assert "not an absence of price data" in out
        assert "do not estimate" in out


@pytest.mark.unit
class TestAnalystPrompt:
    def test_the_price_block_is_injected_even_when_the_profile_is_withheld(self, monkeypatch):
        monkeypatch.setattr(etf_analyst, "route_to_vendor",
                            lambda method, *a, **k: f"WITHHELD:{method}")
        monkeypatch.setattr(etf_analyst, "build_price_window",
                            lambda *a, **k: "PRICE_BLOCK 1.990 +2.58%")

        captured = {}

        class _LLM:
            def invoke(self, messages, *a, **k):
                captured["prompt"] = " ".join(str(m) for m in messages)
                return MagicMock(content="ETF report")

        node = etf_analyst.create_etf_analyst(_LLM())
        result = node({
            "company_of_interest": "515070.SS",
            "trade_date": "2026-11-13",
            "asset_type": "etf",
            "messages": [],
        })

        prompt = captured["prompt"]
        assert "PRICE_BLOCK 1.990 +2.58%" in prompt
        assert "WITHHELD:get_etf_profile" in prompt
        assert result["etf_report"] == "ETF report"
        # The brief promises four blocks, not three.
        assert "four data blocks" in prompt
        assert "Price action and drawdown" in prompt
