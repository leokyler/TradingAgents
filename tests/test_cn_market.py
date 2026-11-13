"""Tests for the CN price vendor: parsing, source fallback, units, indicators."""

import json
import unittest
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import cn_market
from tradingagents.dataflows.vendors.cn_market import get_indicators, get_stock_data

_EASTMONEY = {
    "rc": 0,
    "data": {
        "code": "515070",
        "market": 1,
        "name": "人工智能ETF华夏",
        "klines": [
            "2026-11-11,0.880,0.912,0.913,0.878,1250292,113000000.0",
            "2026-11-12,0.912,0.902,0.943,0.900,2140000,195000000.0",
        ],
    },
}

_TENCENT = {
    "code": 0,
    "msg": "",
    "data": {"sh515070": {"qfqday": [
        ["2026-11-11", "0.880", "0.912", "0.913", "0.878", "1250292.000"],
        ["2026-11-12", "0.912", "0.902", "0.943", "0.900", "2140000.000"],
    ]}},
}

_SINA = [
    {"day": "2026-11-11", "open": "0.880", "high": "0.913", "low": "0.878",
     "close": "0.912", "volume": "125029234"},
    {"day": "2026-11-12", "open": "0.912", "high": "0.943", "low": "0.900",
     "close": "0.902", "volume": "214000012"},
]


def _returns(payload):
    return lambda *a, **k: json.dumps(payload)


def _raises(exc):
    def impl(*a, **k):
        raise exc
    return impl


def _patched(eastmoney=None, plain=None):
    """Patch both transports for one test; the real ones are restored on exit."""
    return mock.patch.multiple(
        cn_market,
        _eastmoney_get=eastmoney if eastmoney is not None else _raises(AssertionError("eastmoney used")),
        _plain_get=plain if plain is not None else _returns({"data": {}}),
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    cn_market._cache.clear()
    yield
    cn_market._cache.clear()


@pytest.mark.unit
class TestCnStockData(unittest.TestCase):
    def test_eastmoney_parses_bars_and_reports_shares(self):
        with _patched(eastmoney=_returns(_EASTMONEY)):
            out = get_stock_data("515070.SS", "2026-11-11", "2026-11-12")
        assert "# Stock data for 515070.SS" in out
        assert "# Total records: 2" in out
        assert "Date,Open,High,Low,Close,Volume" in out
        # 1,250,292 lots -> 125,029,200 shares
        assert "2026-11-11,0.88,0.913,0.878,0.912,125029200" in out

    def test_tencent_serves_the_bars_when_eastmoney_is_unreachable(self):
        with _patched(
            eastmoney=_raises(VendorUnavailableError("edge closed the connection")),
            plain=_returns(_TENCENT),
        ):
            out = get_stock_data("515070.SS", "2026-11-11", "2026-11-12")
        assert "2026-11-12,0.912,0.943,0.90,0.902,214000000" in out

    def test_sina_is_the_last_source(self):
        plain = lambda url, params: (  # noqa: E731
            json.dumps(_SINA) if "sina" in url else json.dumps({"data": {}})
        )
        with _patched(
            eastmoney=_raises(VendorUnavailableError("blocked")),
            plain=plain,
        ):
            out = get_stock_data("515070.SS", "2026-11-11", "2026-11-12")
        assert "2026-11-11,0.88,0.913,0.878,0.912,125029234" in out

    def test_non_a_share_symbol_is_refused(self):
        with pytest.raises(NoMarketDataError) as ctx:
            get_stock_data("AAPL", "2026-11-11", "2026-11-12")
        assert "Shanghai/Shenzhen" in str(ctx.value)

    def test_empty_answers_from_every_source_are_no_data(self):
        with _patched(
            eastmoney=_returns({"rc": 0, "data": None}),
            plain=lambda url, params: json.dumps({"data": {}}),
        ):
            with pytest.raises(NoMarketDataError):
                get_stock_data("515070.SS", "2026-11-11", "2026-11-12")

    def test_stale_frame_is_reported_as_no_data(self):
        # Inside the window but 23 days before the run date: the guard's case.
        stale = {"rc": 0, "data": {"klines": ["2026-10-20,1.0,1.0,1.0,1.0,10,1.0"]}}
        with _patched(eastmoney=_returns(stale)):
            with pytest.raises(NoMarketDataError) as ctx:
                get_stock_data("515070.SS", "2026-10-01", "2026-11-12")
        assert "stale" in str(ctx.value)

    def test_dates_are_forwarded_to_the_vendor(self):
        seen = {}

        def fake(url, *, params=None, referer=None):
            seen.update(params or {})
            return json.dumps(_EASTMONEY)

        with _patched(eastmoney=fake):
            get_stock_data("515070.SS", "2026-11-01", "2026-11-12")
        assert seen["beg"] == "20261101"
        assert seen["end"] == "20261112"
        assert seen["klt"] == "101" and seen["fqt"] == "1"


@pytest.mark.unit
class TestCnIndicators(unittest.TestCase):
    def _bars(self, periods=300, end="2026-11-12"):
        rows = []
        for i, day in enumerate(pd.bdate_range(end=end, periods=periods)):
            price = 1.0 + i * 0.001
            rows.append(
                f"{day:%Y-%m-%d},{price:.4f},{price:.4f},"
                f"{price + 0.01:.4f},{price - 0.01:.4f},1000,100000.0"
            )
        return {"rc": 0, "data": {"klines": rows}}

    def test_indicator_window_is_computed_from_cn_bars(self):
        with _patched(eastmoney=_returns(self._bars())):
            out = get_indicators("515070.SS", "rsi", "2026-11-12", 5)
        assert out.startswith("## rsi values from 2026-11-07 to 2026-11-12")
        assert "2026-11-11: " in out
        assert "N/A: Not a trading day" in out  # 11-07/11-08 are a weekend
        assert "RSI: Measures momentum" in out

    def test_unknown_indicator_is_refused(self):
        with pytest.raises(ValueError, match="not supported"):
            get_indicators("515070.SS", "not_an_indicator", "2026-11-12", 5)

    def test_indicators_reject_a_foreign_symbol(self):
        with pytest.raises(NoMarketDataError):
            get_indicators("AAPL", "rsi", "2026-11-12", 5)


@pytest.mark.unit
def test_bar_count_reaches_back_from_today_not_the_run_end():
    """A historical run must ask a latest-N endpoint for every bar since its start."""
    from datetime import datetime

    from tradingagents.dataflows.date_window import get_current_date

    start = datetime.strptime("2026-01-01", "%Y-%m-%d")
    today = datetime.strptime(get_current_date(), "%Y-%m-%d")
    assert cn_market._bar_count(start) >= (today - start).days
    assert cn_market._bar_count(today) >= 40
