"""The historical valuation anchor follows the configured etf_data chain.

Eastmoney returns the point-in-time notice; only the ETF analyst may append a
dated CSI index multiple, and only when the effective chain names csindex. A
run that configured `etf_data="eastmoney"` must not reach csindex at all.
"""

import copy

import pytest

import tradingagents.dataflows.config as config_module
from tradingagents.agents.analysts import etf_analyst
from tradingagents.dataflows.config import set_config

_NOTICE = (
    "# ETF index fundamentals for 515070.SS\n"
    "# Point-in-time as of: 2026-11-10\n\n"
    "STRUCTURALLY_UNAVAILABLE: withheld.\n\n"
    "Do not present any snapshot figure above as information available on 2026-11-10."
)
_ANCHOR = "Point-in-time index valuation (CSIndex, 2026-11-10): PE (TTM) 44.38."


@pytest.fixture(autouse=True)
def _restore_config():
    original = copy.deepcopy(config_module._config)
    yield
    config_module._config = original


@pytest.mark.unit
def test_the_anchor_is_appended_only_when_csindex_is_in_the_chain(monkeypatch):
    from tradingagents.dataflows.vendors import csindex

    calls = []
    monkeypatch.setattr(etf_analyst, "_fetch", lambda *a, **k: _NOTICE)
    monkeypatch.setattr(csindex, "point_in_time_summary",
                        lambda ticker, day: calls.append((ticker, day)) or _ANCHOR)
    set_config({"data_vendors": {"etf_data": "eastmoney,csindex"}})

    out = etf_analyst._fetch_valuation("515070.SS", "2026-11-10")

    assert out.startswith(_NOTICE)
    assert out.endswith(_ANCHOR)          # after the point-in-time warning
    assert calls == [("515070.SS", "2026-11-10")]


@pytest.mark.unit
def test_an_explicit_eastmoney_only_chain_never_calls_csindex(monkeypatch):
    from tradingagents.dataflows.vendors import csindex

    def must_not_run(*args, **kwargs):
        raise AssertionError("csindex was called outside the configured chain")

    monkeypatch.setattr(etf_analyst, "_fetch", lambda *a, **k: _NOTICE)
    monkeypatch.setattr(csindex, "point_in_time_summary", must_not_run)
    set_config({"data_vendors": {"etf_data": "eastmoney"}})

    assert etf_analyst._fetch_valuation("515070.SS", "2026-11-10") == _NOTICE


@pytest.mark.unit
def test_a_served_valuation_block_gets_no_anchor(monkeypatch):
    from tradingagents.dataflows.vendors import csindex

    def must_not_run(*args, **kwargs):
        raise AssertionError("a served valuation does not need the anchor")

    monkeypatch.setattr(etf_analyst, "_fetch", lambda *a, **k: "# Aggregate PE: 30.17")
    monkeypatch.setattr(csindex, "point_in_time_summary", must_not_run)
    set_config({"data_vendors": {"etf_data": "eastmoney,csindex"}})

    assert etf_analyst._fetch_valuation("515070.SS", "2026-11-10") == "# Aggregate PE: 30.17"
