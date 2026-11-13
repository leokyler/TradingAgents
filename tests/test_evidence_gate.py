"""Tests for the evidence gate: no price series means REVIEW, not a rating."""

from types import SimpleNamespace

import pytest

from tradingagents.agents import evidence
from tradingagents.agents.rating import RATING_REVIEW
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
class TestPriceProbe:
    def test_csv_answer_means_prices_exist(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.router.route_to_vendor",
            lambda method, ticker, start, end: "# Stock data for 510300.SS\n,Open,Close\n",
        )
        assert evidence.price_data_available("510300.SS", "2026-11-12") is True

    def test_no_data_sentinel_means_no_prices(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.router.route_to_vendor",
            lambda method, ticker, start, end: "NO_DATA_AVAILABLE: No usable market data ...",
        )
        assert evidence.price_data_available("510300.SS", "2026-11-12") is False

    def test_vendor_unavailable_sentinel_means_no_prices(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.router.route_to_vendor",
            lambda method, ticker, start, end: "DATA_UNAVAILABLE: no configured vendor ...",
        )
        assert evidence.price_data_available("510300.SS", "2026-11-12") is False

    def test_a_raising_probe_is_unknown_not_a_verdict(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr("tradingagents.dataflows.router.route_to_vendor", boom)
        assert evidence.price_data_available("510300.SS", "2026-11-12") is None

    def test_probe_asks_for_a_trailing_window(self, monkeypatch):
        seen = {}

        def fake(method, ticker, start, end):
            seen.update(method=method, ticker=ticker, start=start, end=end)
            return "# Stock data\n,Open\n"

        monkeypatch.setattr("tradingagents.dataflows.router.route_to_vendor", fake)
        evidence.price_data_available("510300.SS", "2026-11-12")
        assert seen["method"] == "get_stock_data"
        assert seen["ticker"] == "510300.SS"
        assert seen["end"] == "2026-11-12"
        assert seen["start"] == "2026-10-13"


@pytest.mark.unit
class TestGateSignal:
    def _state(self):
        return {"company_of_interest": "510300.SS", "trade_date": "2026-11-12"}

    def test_rating_stands_when_prices_exist(self, monkeypatch):
        monkeypatch.setattr(evidence, "price_data_available", lambda *a: True)
        signal, note = evidence.gate_signal(self._state(), "Buy", {"evidence_gate": True})
        assert (signal, note) == ("Buy", "")

    def test_rating_becomes_review_without_prices(self, monkeypatch):
        monkeypatch.setattr(evidence, "price_data_available", lambda *a: False)
        signal, note = evidence.gate_signal(self._state(), "Buy", {"evidence_gate": True})
        assert signal == RATING_REVIEW
        assert "no usable price data" in note
        assert "2026-11-12" in note

    def test_gate_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(evidence, "price_data_available", lambda *a: False)
        signal, note = evidence.gate_signal(self._state(), "Sell", {"evidence_gate": False})
        assert (signal, note) == ("Sell", "")

    def test_review_is_not_downgraded_again(self, monkeypatch):
        called = []
        monkeypatch.setattr(evidence, "price_data_available", lambda *a: called.append(1))
        signal, note = evidence.gate_signal(self._state(), RATING_REVIEW, {"evidence_gate": True})
        assert (signal, note) == (RATING_REVIEW, "")
        assert called == []

    def test_an_unknown_probe_leaves_the_rating_alone(self, monkeypatch):
        monkeypatch.setattr(evidence, "price_data_available", lambda *a: None)
        signal, note = evidence.gate_signal(self._state(), "Buy", {"evidence_gate": True})
        assert (signal, note) == ("Buy", "")

    def test_missing_ticker_or_date_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(evidence, "price_data_available", lambda *a: False)
        signal, note = evidence.gate_signal({"company_of_interest": ""}, "Buy", {})
        assert (signal, note) == ("Buy", "")


@pytest.mark.unit
def test_run_graph_publishes_review_when_the_gate_fires(monkeypatch):
    """The wiring, not just the helper: a gated run stores and returns REVIEW."""
    stored = {}
    g = object.__new__(TradingAgentsGraph)
    g.config = {"evidence_gate": True}
    g.selected_analysts = ("market",)
    g.debug = False
    g.propagator = SimpleNamespace(get_graph_args=lambda: {})
    g.checkpoint_input = lambda state: state
    g.create_run_state = lambda *a, **k: {
        "company_of_interest": "510300.SS", "trade_date": "2026-11-12",
    }
    g.graph = SimpleNamespace(invoke=lambda *a, **k: {
        "company_of_interest": "510300.SS", "trade_date": "2026-11-12",
        "final_rating": "Buy", "final_trade_decision": "**Rating**: Buy\n\nBuy it.",
    })
    g._log_state = lambda *a: None
    g.clear_checkpoint_on_success = lambda *a: None
    g.memory_log = SimpleNamespace(store_decision=lambda **kwargs: stored.update(kwargs))

    monkeypatch.setattr(evidence, "price_data_available", lambda *a: False)
    state, signal = g._run_graph("510300.SS", "2026-11-12")

    assert signal == RATING_REVIEW
    assert state["final_rating"] == RATING_REVIEW
    assert "Data coverage" in state["final_trade_decision"]
    assert stored["rating"] == RATING_REVIEW


@pytest.mark.unit
def test_record_decision_gates_a_cli_style_run(monkeypatch):
    """The CLI ends at record_decision too: its run must publish REVIEW."""
    logged, stored = {}, {}
    g = object.__new__(TradingAgentsGraph)
    g.config = {"evidence_gate": True}
    g._log_state = lambda date, state: logged.update(state)
    g.memory_log = SimpleNamespace(store_decision=lambda **kwargs: stored.update(kwargs))
    state = {
        "company_of_interest": "510300.SS",
        "trade_date": "2026-11-12",
        "final_rating": "Buy",
        "final_trade_decision": "**Rating**: Buy\n\nBuy it.",
    }

    monkeypatch.setattr(evidence, "price_data_available", lambda *a: False)
    g.record_decision("510300.SS", "2026-11-12", state)

    assert state["final_rating"] == RATING_REVIEW
    assert "Data coverage" in state["final_trade_decision"]
    assert logged["final_rating"] == RATING_REVIEW
    assert stored["rating"] == RATING_REVIEW


@pytest.mark.unit
def test_record_decision_keeps_a_backed_rating(monkeypatch):
    logged, stored = {}, {}
    g = object.__new__(TradingAgentsGraph)
    g.config = {"evidence_gate": True}
    g._log_state = lambda date, state: logged.update(state)
    g.memory_log = SimpleNamespace(store_decision=lambda **kwargs: stored.update(kwargs))
    state = {
        "company_of_interest": "510300.SS",
        "trade_date": "2026-11-12",
        "final_rating": "Sell",
        "final_trade_decision": "**Rating**: Sell\n\nSell it.",
    }

    monkeypatch.setattr(evidence, "price_data_available", lambda *a: True)
    g.record_decision("510300.SS", "2026-11-12", state)

    assert state["final_rating"] == "Sell"
    assert stored["rating"] == "Sell"
    assert "Data coverage" not in state["final_trade_decision"]
