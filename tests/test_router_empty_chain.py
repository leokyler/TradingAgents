"""An explicitly empty vendor chain is a configuration choice, not a fallback.

Setting ``news_data`` to "" used to walk the default chain (alpha_vantage then
Yahoo), spending 14 seconds before reporting a failure that never happened. It
must now say "no news vendor configured" at once.
"""

import copy
import time
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config


@pytest.fixture(autouse=True)
def _restore_config():
    original = copy.deepcopy(config_module._config)
    yield
    config_module._config = original


@pytest.mark.unit
def test_an_empty_news_chain_returns_at_once_and_names_the_configuration():
    set_config({"data_vendors": {"news_data": ""}})

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("an empty chain must not call a vendor")

    with mock.patch.dict(router.VENDOR_METHODS,
                         {"get_news": {"yfinance": must_not_be_called,
                                       "alpha_vantage": must_not_be_called}},
                         clear=False):
        started = time.monotonic()
        out = router.route_to_vendor("get_news", "AAPL", "2026-11-01", "2026-11-10")
        elapsed = time.monotonic() - started

    assert "no news vendor configured" in out
    assert "NO_DATA_AVAILABLE" in out
    assert elapsed < 0.2


@pytest.mark.unit
def test_an_empty_chain_is_not_the_default_sentinel():
    set_config({"data_vendors": {"news_data": "default"}})
    with mock.patch.dict(router.VENDOR_METHODS,
                         {"get_news": {"yfinance": lambda *a, **k: "YF"}}, clear=False):
        assert router.route_to_vendor("get_news", "AAPL", "2026-11-01", "2026-11-10") == "YF"


@pytest.mark.unit
def test_the_default_news_chain_is_unchanged_for_other_markets():
    # cn_news is added to the chain by the A-share ETF CLI run, not globally.
    assert default_config.DEFAULT_CONFIG["data_vendors"]["news_data"] == "yfinance"


@pytest.mark.unit
def test_the_empty_chain_short_circuit_is_limited_to_news():
    """Only news_data gained the immediate "not configured" answer; another
    category set to "" keeps its previous all-vendors behaviour."""
    set_config({"data_vendors": {"core_stock_apis": ""}})
    with mock.patch.dict(router.VENDOR_METHODS,
                         {"get_stock_data": {"yfinance": lambda *a, **k: "YF"}},
                         clear=False):
        assert router.route_to_vendor(
            "get_stock_data", "AAPL", "2026-11-01", "2026-11-10"
        ) == "YF"
