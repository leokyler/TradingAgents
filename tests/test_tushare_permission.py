"""Tushare answers a fund endpoint with 40203: say "no permission", stop retrying.

The account's points tier decides which endpoints it can read. Reporting a
permission answer as an ordinary outage kept Tushare in the chain as if it were
a usable fallback, when it is a dead end for that endpoint.
"""

import logging

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorNotConfiguredError
from tradingagents.dataflows.vendors import tushare as ts


class _PermissionResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"code": 40203, "msg": "抱歉，您没有访问该接口的权限，权限的具体详情访问..."}


class _Session:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return _PermissionResponse()


@pytest.fixture(autouse=True)
def _clear_permissions():
    original = config_module._config
    ts._NO_PERMISSION.clear()
    yield
    ts._NO_PERMISSION.clear()
    config_module._config = original


@pytest.mark.unit
def test_a_permission_answer_names_the_endpoint(caplog, monkeypatch):
    session = _Session()
    monkeypatch.setattr(ts, "_http", lambda: session)
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(VendorNotConfiguredError, match="no permission for fund_daily"),
    ):
        ts._post({"api_name": "fund_daily"})
    assert "no permission for fund_daily" in caplog.text
    assert session.calls == 1


@pytest.mark.unit
def test_the_permission_is_remembered_without_another_request(monkeypatch):
    session = _Session()
    monkeypatch.setattr(ts, "_http", lambda: session)

    with pytest.raises(VendorNotConfiguredError):
        ts._post({"api_name": "fund_daily"})
    # The second call fails at once: no HTTP request, same endpoint.
    with pytest.raises(VendorNotConfiguredError, match="recorded earlier"):
        ts._post({"api_name": "fund_daily"})
    assert session.calls == 1


@pytest.mark.unit
def test_the_router_moves_on_to_the_next_vendor():
    config_module._config = {
        **default_config.DEFAULT_CONFIG,
        "data_vendors": {"core_stock_apis": "tushare,cn_market"},
    }

    def no_permission(*args, **kwargs):
        raise VendorNotConfiguredError("Tushare has no permission for fund_daily (code 40203)")

    with _patch_router(no_permission):
        out = router.route_to_vendor("get_stock_data", "515070.SS", "2026-11-01", "2026-11-13")

    assert out == "CN_BARS"


def _patch_router(no_permission):
    from contextlib import contextmanager
    from unittest import mock

    @contextmanager
    def patched():
        with mock.patch.dict(router.VENDOR_METHODS, {
            "get_stock_data": {
                "tushare": no_permission,
                "cn_market": lambda *a, **k: "CN_BARS",
            },
        }, clear=False):
            yield
    return patched()


@pytest.mark.unit
def test_the_default_etf_chain_does_not_name_tushare():
    assert "tushare" not in default_config.DEFAULT_CONFIG["data_vendors"]["etf_data"]
