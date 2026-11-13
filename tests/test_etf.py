"""Tests for ETF identification, the Eastmoney fund vendor and ETF analyst selection."""

import json
import unittest

import pytest

from tradingagents.agents.context import build_instrument_context
from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.etf import etf_code, is_a_share_etf
from tradingagents.dataflows.vendors import eastmoney
from tradingagents.dataflows.vendors.eastmoney import eastmoney_secid, stock_secid
from tradingagents.graph.trading_graph import TradingAgentsGraph

# The Eastmoney fixtures describe this date. Pinning "today" keeps them honest:
# a day later every present-day snapshot is withheld as historical and the
# profile assertions would read the withheld notice instead.
FIXTURE_TODAY = "2026-11-12"


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(date_window, "get_current_date", lambda: FIXTURE_TODAY)


@pytest.mark.unit
class TestEtfIdentification(unittest.TestCase):
    def test_shanghai_fund_ranges(self):
        for symbol in ("510300.SS", "510050.SS", "588000.SS", "561300.SS"):
            self.assertTrue(is_a_share_etf(symbol), symbol)

    def test_shenzhen_fund_range(self):
        self.assertTrue(is_a_share_etf("159919.SZ"))

    def test_stocks_indices_and_foreign_funds_are_not_matched(self):
        for symbol in ("600519.SS", "000001.SZ", "000300.SS", "SPY", "QQQ", "BTC-USD"):
            self.assertFalse(is_a_share_etf(symbol), symbol)

    def test_bare_code_carries_no_exchange(self):
        self.assertIsNone(etf_code("510300"))

    def test_etf_code_strips_the_exchange_suffix(self):
        self.assertEqual(etf_code("510300.SS"), "510300")
        self.assertEqual(etf_code("159919.SZ"), "159919")

    def test_eastmoney_secid_uses_the_market_prefix(self):
        self.assertEqual(eastmoney_secid("510300.SS"), "1.510300")
        self.assertEqual(eastmoney_secid("159919.SZ"), "0.159919")
        self.assertIsNone(eastmoney_secid("SPY"))

    def test_stock_secid_from_bare_code(self):
        self.assertEqual(stock_secid("600519"), "1.600519")
        self.assertEqual(stock_secid("688825"), "1.688825")
        self.assertEqual(stock_secid("300750"), "0.300750")
        self.assertEqual(stock_secid("000333"), "0.000333")


_QUOTE = {
    "f43": 4.114, "f44": 4.177, "f45": 4.108, "f46": 4.145, "f47": 11631446,
    "f48": 4818464209.0, "f57": "510300", "f58": "沪深300ETF华泰柏瑞", "f59": 3,
    "f60": 4.15, "f84": 46342387712.0, "f116": 190652583047.168, "f168": 2.51,
    "f169": -0.036, "f170": -0.87,
}

_HOLDINGS_PAGE = """var apidata={ content:"<div class='box'>
<h4 class='t'>...2026年3季度股票投资明细...截止至：<font class='px12'>2026-09-30</font>...</h4>
<table class='w782 comm tzxq t2'><thead><tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr></thead>
<tbody>
<tr><td>1</td><td>600519</td><td>贵州茅台</td><td>3.09%</td></tr>
<tr><td>2</td><td>300750</td><td>宁德时代</td><td>3.03%</td></tr>
</tbody></table>
<h4 class='t'>...2026年2季度股票投资明细...截止至：<font class='px12'>2026-06-30</font>...</h4>
<table class='w782 comm tzxq t2'><tbody>
<tr><td>1</td><td>601318</td><td>中国平安</td><td>2.85%</td></tr>
</tbody></table>
",rc:0};
"""


def _fake_get(pages):
    """An ``eastmoney._get`` replacement keyed by the first matching URL fragment."""
    def fake(url, *, params=None, referer=None):
        for fragment, body in pages.items():
            if fragment in url:
                return body if isinstance(body, str) else json.dumps(body)
        raise AssertionError(f"unexpected URL {url}")
    return fake


@pytest.mark.unit
class TestEtfProfile(unittest.TestCase):
    def _call(self, **overrides):
        pages = {
            eastmoney._QUOTE_URL: {"data": {**_QUOTE, **overrides.pop("quote", {})}},
            eastmoney._FUND_JS_URL.split("{code}")[0]: (
                'var Data_netWorthTrend = [{"x":1762790400000,"y":4.1528}];'
            ),
        }
        original = eastmoney._get
        eastmoney._get = _fake_get(pages)
        try:
            return eastmoney.get_etf_profile("510300.SS", "2026-11-12")
        finally:
            eastmoney._get = original

    def test_profile_reports_size_nav_and_premium(self):
        out = self._call()
        self.assertIn("沪深300ETF华泰柏瑞", out)
        self.assertIn("4.114", out)
        self.assertIn("463.42 亿份", out)
        self.assertIn("1,906.53 亿元", out)
        self.assertIn("4.1528", out)
        # (4.114 - 4.1528) / 4.1528 = -0.934%
        self.assertIn("-0.93%", out)
        self.assertIn("discount", out)

    def test_historical_run_withholds_the_live_snapshot(self):
        out = eastmoney.get_etf_profile("510300.SS", "2026-06-30")
        self.assertIn("withheld", out.lower())
        self.assertNotIn("Premium/discount", out)

    def test_non_fund_symbol_is_refused(self):
        for symbol in ("600519.SS", "SPY", "000300.SS"):
            with self.assertRaises(NoMarketDataError):
                eastmoney.get_etf_profile(symbol, "2026-11-12")


@pytest.mark.unit
class TestEtfHoldings(unittest.TestCase):
    def _get(self, as_of_date, limit=10):
        original = eastmoney._get
        eastmoney._get = _fake_get({eastmoney._HOLDINGS_URL: _HOLDINGS_PAGE})
        try:
            return eastmoney.get_etf_holdings("510300.SS", as_of_date, limit)
        finally:
            eastmoney._get = original

    def test_current_run_reads_the_newest_quarter(self):
        out = self._get("2026-11-12")
        self.assertIn("2026年3季度", out)
        self.assertIn("2026-09-30", out)
        self.assertIn("贵州茅台", out)
        self.assertNotIn("中国平安", out)

    def test_older_run_reads_the_quarter_that_had_closed(self):
        out = self._get("2026-08-01")
        self.assertIn("2026年2季度", out)
        self.assertIn("中国平安", out)

    def test_limit_caps_the_disclosed_rows(self):
        out = self._get("2026-11-12", limit=1)
        self.assertIn("贵州茅台", out)
        self.assertNotIn("宁德时代", out)

    def test_run_before_any_disclosed_quarter_is_withheld(self):
        original = eastmoney._get
        eastmoney._get = _fake_get({eastmoney._HOLDINGS_URL: _HOLDINGS_PAGE})
        try:
            out = eastmoney.get_etf_holdings("510300.SS", "2026-01-05")
        finally:
            eastmoney._get = original
        self.assertIn("withheld", out.lower())


@pytest.mark.unit
class TestEtfBasketValuation(unittest.TestCase):
    def test_aggregate_is_capital_weighted_and_states_coverage(self):
        def fake(url, *, params=None, referer=None):
            if eastmoney._HOLDINGS_URL in url:
                return _HOLDINGS_PAGE
            if params and params.get("secid") == "1.600519":
                return json.dumps({"data": {"f57": "600519", "f58": "贵州茅台",
                                            "f116": 1000.0, "f162": 10.0, "f167": 2.0,
                                            "f127": "白酒Ⅱ"}})
            if params and params.get("secid") == "0.300750":
                return json.dumps({"data": {"f57": "300750", "f58": "宁德时代",
                                            "f116": 1000.0, "f162": 20.0, "f167": 4.0,
                                            "f127": "电池"}})
            raise AssertionError(f"unexpected {url} {params}")

        original = eastmoney._get
        eastmoney._get = fake
        try:
            out = eastmoney.get_etf_index_fundamentals("510300.SS", "2026-11-12")
        finally:
            eastmoney._get = original

        # PE = 2000 / (1000/10 + 1000/20) = 13.33 ; PB = 2000 / (500 + 250) = 2.67
        self.assertIn("13.33", out)
        self.assertIn("2.67", out)
        # ROE = PB / PE = 0.2 -> 20.00%
        self.assertIn("20.00%", out)
        self.assertIn("6.12%", out)  # 3.09 + 3.03 coverage
        # Industry exposure is aggregated over the covered constituents: each
        # name is its own sub-industry here, so the weights pass straight through.
        self.assertIn("### Industry exposure", out)
        self.assertIn("白酒Ⅱ", out)
        self.assertIn("电池", out)

    def test_historical_run_gets_no_present_day_valuation(self):
        out = eastmoney.get_etf_index_fundamentals("510300.SS", "2026-06-30")
        self.assertIn("withheld", out.lower())


_BOND_PAGE = """var apidata={ content:"<div class='box'>
<h4 class='t'>...2026年3季度债券投资明细...截止至：<font class='px12'>2026-09-30</font>...</h4>
<table class='w782 comm tzxq t2'><tbody>
<tr><td>1</td><td>019834</td><td>26国债08</td><td>26.72%</td><td>195,775.03</td></tr>
<tr><td>2</td><td>260015</td><td>26附息国债15</td><td>14.92%</td><td>109,314.99</td></tr>
</tbody></table>
",rc:0};
"""

_EMPTY_PAGE = 'var apidata={ content:"",rc:0};'


@pytest.mark.unit
class TestFundTypeDisclosure(unittest.TestCase):
    def _holdings(self, ticker):
        def fake(url, *, params=None, referer=None):
            if eastmoney._HOLDINGS_URL not in url:
                raise AssertionError(url)
            if params.get("code") == "518880":
                return _EMPTY_PAGE
            return _BOND_PAGE if params.get("type") == "zqcc" else _EMPTY_PAGE

        original = eastmoney._get
        eastmoney._get = fake
        try:
            return eastmoney.get_etf_holdings(ticker, "2026-11-12")
        finally:
            eastmoney._get = original

    def test_bond_fund_falls_back_to_bond_holdings(self):
        out = self._holdings("511010.SS")
        self.assertIn("bond holdings", out)
        self.assertIn("26国债08", out)
        self.assertIn("26.72%", out)

    def test_commodity_fund_says_it_discloses_no_basket(self):
        out = self._holdings("518880.SS")
        self.assertIn("discloses no stock or bond holdings", out)

    def test_basket_valuation_does_not_apply_without_equity_holdings(self):
        original = eastmoney._get
        eastmoney._get = _fake_get({eastmoney._HOLDINGS_URL: _EMPTY_PAGE})
        try:
            out = eastmoney.get_etf_index_fundamentals("511010.SS", "2026-11-12")
        finally:
            eastmoney._get = original
        self.assertIn("does not apply", out)
        self.assertNotIn("Aggregate PE", out)


@pytest.mark.unit
class TestEtfInstrumentContext(unittest.TestCase):
    def test_context_says_fund_not_company(self):
        context = build_instrument_context("510300.SS", "etf", {"company_name": "沪深300ETF"})
        self.assertIn("exchange-traded fund", context)
        self.assertIn("no company financial statements", context)
        self.assertIn("T+1", context)

    def test_stock_context_has_no_fund_wording(self):
        context = build_instrument_context("600519.SS", "stock", {"company_name": "贵州茅台"})
        self.assertNotIn("exchange-traded fund", context)


@pytest.mark.unit
class TestEtfAnalystSelection(unittest.TestCase):
    def test_plan_accepts_the_etf_analyst(self):
        from tradingagents.graph.analyst_execution import build_analyst_execution_plan

        plan = build_analyst_execution_plan(["market", "etf", "news"])
        self.assertEqual([spec.key for spec in plan.specs], ["market", "etf", "news"])
        etf_spec = plan.specs[1]
        self.assertEqual(etf_spec.agent_node, "ETF Analyst")
        self.assertEqual(etf_spec.report_key, "etf_report")
        self.assertEqual(etf_spec.tools, ())

    def test_asset_type_routing(self):
        from cli.models import AnalystType, AssetType
        from cli.prompts import detect_asset_type, filter_analysts_for_asset_type

        self.assertEqual(detect_asset_type("510300.SS"), AssetType.ETF)
        self.assertEqual(detect_asset_type("159919.SZ"), AssetType.ETF)
        self.assertEqual(detect_asset_type("600519.SS"), AssetType.STOCK)

        etf_analysts = filter_analysts_for_asset_type(list(AnalystType), AssetType.ETF)
        self.assertEqual(etf_analysts, [AnalystType.MARKET, AnalystType.ETF, AnalystType.NEWS])

        stock_analysts = filter_analysts_for_asset_type(list(AnalystType), AssetType.STOCK)
        self.assertNotIn(AnalystType.ETF, stock_analysts)
        # The stock roster is unchanged by the ETF addition.
        self.assertEqual(
            [analyst.value for analyst in stock_analysts],
            ["market", "social", "news", "fundamentals"],
        )

        crypto_analysts = filter_analysts_for_asset_type(list(AnalystType), AssetType.CRYPTO)
        self.assertNotIn(AnalystType.ETF, crypto_analysts)
        self.assertNotIn(AnalystType.FUNDAMENTALS, crypto_analysts)


@pytest.mark.unit
class TestFundRunShapeWarning(unittest.TestCase):
    """The two halves of an ETF run are named separately, so say when they disagree."""

    def _graph(self, selected):
        graph = object.__new__(TradingAgentsGraph)
        graph.selected_analysts = tuple(selected)
        return graph

    def test_etf_asset_type_without_the_etf_analyst_warns(self):
        graph = self._graph(["market", "news"])
        with self.assertLogs("tradingagents.graph.trading_graph", level="WARNING") as logs:
            graph._warn_if_asset_type_has_no_analyst("etf")
        self.assertIn("no fund-level data", "\n".join(logs.output))

    def test_a_fund_run_that_selects_the_etf_analyst_is_quiet(self):
        graph = self._graph(["market", "etf", "news"])
        with self.assertNoLogs("tradingagents.graph.trading_graph", level="WARNING"):
            graph._warn_if_asset_type_has_no_analyst("etf")

    def test_a_stock_run_is_quiet(self):
        graph = self._graph(["market", "news"])
        with self.assertNoLogs("tradingagents.graph.trading_graph", level="WARNING"):
            graph._warn_if_asset_type_has_no_analyst("stock")
