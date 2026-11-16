"""CSI index valuation as the ETF valuation fallback.

The fundf10 holdings page is a single, sometimes slow, source. CSI publishes
the index's own PE and dividend yield as a public workbook; this vendor serves
that series, point-in-time and with a bounded request, when the primary source
cannot be read.
"""

import copy
from unittest import mock

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import csindex


@pytest.fixture(autouse=True)
def _clean_state():
    csindex._benchmark_name.cache_clear()
    csindex._index_catalogue.cache_clear()
    original = copy.deepcopy(config_module._config)
    yield
    config_module._config = original
    csindex._benchmark_name.cache_clear()
    csindex._index_catalogue.cache_clear()


class _FakeAkshare:
    def __init__(self, benchmark="中证人工智能主题指数收益率", rows=None):
        self.benchmark = benchmark
        self.rows = rows if rows is not None else [
            {"指数代码": "930713", "指数全称": "中证人工智能主题指数", "指数简称": "CS人工智"},
        ]

    def fund_info_ths(self, symbol):
        return pd.DataFrame([{"字段": "业绩比较基准", "值": self.benchmark}])

    def index_csindex_all(self):
        return pd.DataFrame(self.rows)


def _valuation_frame(days=("2026-11-11", "2026-11-13")):
    return pd.DataFrame({
        "日期": pd.to_datetime(list(days)),
        "市盈率1": [46.0] * len(days),
        "市盈率2": [45.0] * len(days),
        "股息率1": [0.8] * len(days),
        "股息率2": [0.7] * len(days),
    })


@pytest.mark.unit
def test_it_resolves_the_benchmark_and_serves_the_latest_row_on_or_before_the_date(monkeypatch):
    monkeypatch.setattr(csindex, "_import_akshare", lambda: _FakeAkshare())
    monkeypatch.setattr(csindex, "_fetch_csindex", lambda code: _valuation_frame())

    out = csindex.get_etf_index_fundamentals("515070.SS", "2026-11-12")

    assert "930713" in out
    assert "中证人工智能主题指数" in out
    assert "valuation as of 2026-11-11" in out
    assert "PE (TTM)**: 45.00" in out
    assert "not the capital-weighted aggregate" in out


@pytest.mark.unit
def test_a_window_before_the_source_has_rows_is_no_data(monkeypatch):
    monkeypatch.setattr(csindex, "_import_akshare", lambda: _FakeAkshare())
    monkeypatch.setattr(csindex, "_fetch_csindex", lambda code: _valuation_frame())

    with pytest.raises(NoMarketDataError):
        csindex.get_etf_index_fundamentals("515070.SS", "2026-10-01")


@pytest.mark.unit
def test_a_missing_akshare_is_not_configured(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "akshare", None)
    with pytest.raises(VendorNotConfiguredError, match="tradingagents\\[cn\\]"):
        csindex._import_akshare()


@pytest.mark.unit
def test_a_failed_valuation_request_is_a_vendor_outage(monkeypatch):
    monkeypatch.setattr(csindex, "_import_akshare", lambda: _FakeAkshare())

    def broken(code):
        raise VendorUnavailableError("CSIndex valuation request failed after 12s: timed out")

    monkeypatch.setattr(csindex, "_fetch_csindex", broken)
    with pytest.raises(VendorUnavailableError, match="timed out"):
        csindex.get_etf_index_fundamentals("515070.SS", "2026-11-13")


@pytest.mark.unit
def test_the_router_falls_back_to_csindex_when_eastmoney_times_out():
    set_config({"data_vendors": {"etf_data": "eastmoney,csindex"}})

    def timed_out(*args, **kwargs):
        raise VendorUnavailableError("fundf10 valuation request timed out after 12s (retried once)")

    with mock.patch.dict(router.VENDOR_METHODS, {
        "get_etf_index_fundamentals": {
            "eastmoney": timed_out,
            "csindex": lambda *a, **k: "CSI_VALUATION",
        },
    }, clear=False):
        out = router.route_to_vendor("get_etf_index_fundamentals", "515070.SS", "2026-11-13")

    assert out == "CSI_VALUATION"


@pytest.mark.unit
def test_the_default_etf_chain_offers_the_second_source():
    assert "csindex" in default_config.DEFAULT_CONFIG["data_vendors"]["etf_data"]


@pytest.mark.unit
def test_a_stalled_metadata_lookup_is_bounded(monkeypatch):
    import time

    def stalled(**kwargs):
        time.sleep(2)
        return None

    with pytest.raises(VendorUnavailableError, match="did not answer within"):
        csindex._bounded("stalled", stalled, timeout=0.05)


@pytest.mark.unit
def test_an_unexpected_column_count_is_a_vendor_error(monkeypatch):
    import io as _io

    buffer = _io.BytesIO()
    pd.DataFrame([[1] * 11]).to_excel(buffer, index=False)
    monkeypatch.setattr(csindex.transport, "get_bytes", lambda *a, **k: buffer.getvalue())

    with pytest.raises(VendorUnavailableError, match="columns, expected"):
        csindex._fetch_csindex("930713")
