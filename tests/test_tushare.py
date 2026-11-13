"""Tests for the Tushare Pro vendor: symbols, bars, indicators and the ETF profile."""

import copy
import os
import unittest
from unittest import mock

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import tushare as ts


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _bars(periods=300, end="2026-11-12"):
    rows = []
    for i, day in enumerate(pd.bdate_range(end=end, periods=periods)):
        price = 1.0 + i * 0.001
        rows.append([
            day.strftime("%Y%m%d"), f"{price:.4f}", f"{price + 0.01:.4f}",
            f"{price - 0.01:.4f}", f"{price:.4f}", "1000", "100000",
        ])
    return pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "vol", "amount"])


def _fake_api(handlers):
    def api(api_name, **params):
        handler = handlers.get(api_name)
        if handler is None:
            raise AssertionError(f"unexpected Tushare endpoint {api_name}")
        return handler(**params)
    return api


@pytest.mark.unit
class TestTusharePriceData(unittest.TestCase):
    def test_symbols_map_to_tushare_codes(self):
        assert ts._ts_code("515070.SS") == ("515070.SS", "515070.SH")
        assert ts._ts_code("159919.SZ") == ("159919.SZ", "159919.SZ")
        with pytest.raises(NoMarketDataError):
            ts._ts_code("AAPL")

    def test_etf_bars_come_from_fund_daily_and_volume_is_shares(self):
        seen = {}

        def fund_daily(**params):
            seen.update(params)
            return _bars(periods=5)

        with mock.patch.object(ts, "_api", _fake_api({"fund_daily": fund_daily})):
            out = ts.get_stock_data("515070.SS", "2026-11-01", "2026-11-12")

        assert seen["ts_code"] == "515070.SH"
        assert out.startswith("# Stock data for 515070.SS")
        assert "Date,Open,High,Low,Close,Volume" in out
        # 1000 lots -> 100,000 shares
        assert "100000" in out

    def test_stock_bars_come_from_daily(self):
        seen = {}

        def daily(**params):
            seen.update(params)
            return _bars(periods=5)

        with mock.patch.object(ts, "_api", _fake_api({"daily": daily})):
            ts.get_stock_data("600519.SS", "2026-11-01", "2026-11-12")
        assert seen["ts_code"] == "600519.SH"

    def test_empty_answer_is_no_data(self):
        with mock.patch.object(ts, "_api", lambda *a, **k: pd.DataFrame()):
            with pytest.raises(NoMarketDataError):
                ts.get_stock_data("515070.SS", "2026-11-01", "2026-11-12")

    def test_stale_frame_is_no_data(self):
        stale = _bars(periods=2, end="2026-10-20")
        with mock.patch.object(ts, "_api", lambda *a, **k: stale):
            with pytest.raises(NoMarketDataError):
                ts.get_stock_data("515070.SS", "2026-10-01", "2026-11-12")

    def test_missing_token_is_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TUSHARE_TOKEN", None)
            with pytest.raises(VendorNotConfiguredError, match="TUSHARE_TOKEN"):
                ts._api("fund_daily", ts_code="515070.SH")

    def test_api_error_is_a_vendor_error(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 40101, "msg": "您的token不对"}

        class Session:
            def post(self, *a, **k):
                return Response()

        with mock.patch.object(ts, "_http", lambda: Session()):
            with pytest.raises(VendorUnavailableError, match="40101"):
                ts._post({"api_name": "fund_daily"})


@pytest.mark.unit
class TestTushareIndicators(unittest.TestCase):
    def test_indicator_window_is_computed_from_tushare_bars(self):
        with mock.patch.object(ts, "_api", lambda *a, **k: _bars(periods=300)):
            out = ts.get_indicators("515070.SS", "rsi", "2026-11-12", 3)
        assert out.startswith("## rsi values from 2026-11-09 to 2026-11-12")
        assert "2026-11-11: " in out
        assert "RSI: Measures momentum" in out

    def test_unknown_indicator_is_refused(self):
        with pytest.raises(ValueError, match="not supported"):
            ts.get_indicators("515070.SS", "nope", "2026-11-12", 3)


@pytest.mark.unit
class TestTushareEtfProfile(unittest.TestCase):
    def _handlers(self):
        nav = pd.DataFrame(
            [["515070.SH", "20261111", "20261112", "0.9123", "1.2345"]],
            columns=["ts_code", "ann_date", "nav_date", "unit_nav", "accum_nav"],
        )
        shares = pd.DataFrame(
            [["515070.SH", "20261112", "4634238.77"]],
            columns=["ts_code", "trade_date", "fd_share"],
        )
        basic = pd.DataFrame([["515070.SH", "人工智能ETF华夏"]], columns=["ts_code", "name"])
        return {
            "fund_daily": lambda **k: _bars(periods=20),
            "fund_nav": lambda **k: nav,
            "fund_share": lambda **k: shares,
            "fund_basic": lambda **k: basic,
        }

    def test_profile_reports_price_nav_shares_and_premium(self):
        with mock.patch.object(ts, "_api", _fake_api(self._handlers())):
            out = ts.get_etf_profile("515070.SS", "2026-11-12")

        assert out.startswith("# ETF profile for 515070.SS (人工智能ETF华夏)")
        assert "**Price**" in out
        assert "**Latest published NAV**: 0.9123 (2026-11-12)" in out
        assert "**Shares outstanding**" in out
        assert "**Fund size (shares x price)**" in out
        assert "Premium/discount vs latest NAV" in out

    def test_historical_profile_uses_rows_published_by_then(self):
        handlers = self._handlers()
        # A NAV published after the run date must not be used.
        handlers["fund_nav"] = lambda **k: pd.DataFrame(
            [["515070.SH", "20261114", "20261115", "0.99", "1.0"]],
            columns=["ts_code", "ann_date", "nav_date", "unit_nav", "accum_nav"],
        )
        with mock.patch.object(ts, "_api", _fake_api(handlers)):
            out = ts.get_etf_profile("515070.SS", "2026-11-12")
        assert "Latest published NAV" not in out

    def test_non_etf_is_refused(self):
        with pytest.raises(NoMarketDataError, match="exchange-traded fund"):
            ts.get_etf_profile("600519.SS", "2026-11-12")


@pytest.mark.unit
def test_router_serves_prices_from_tushare_when_configured():
    _reset_config()
    try:
        set_config({"data_vendors": {"core_stock_apis": "tushare"}})
        with mock.patch.object(ts, "_api", lambda *a, **k: _bars(periods=5)):
            from tradingagents.dataflows.router import route_to_vendor

            out = route_to_vendor("get_stock_data", "515070.SS", "2026-11-01", "2026-11-12")
        assert out.startswith("# Stock data for 515070.SS")
    finally:
        _reset_config()
