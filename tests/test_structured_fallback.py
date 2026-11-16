"""A structured-output failure must be visible, not silently degrade to prose.

The Portfolio Manager's fallback used to be indistinguishable from a parsed
decision. It now heads the report with a warning, records the parser's reason
for the run log, validates the decision labels, and lets ``--strict`` fail the
run.
"""

import logging
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.structured import (
    FREETEXT_FALLBACK_MARKER,
    fallback_events,
    free_text_with_warning,
    invoke_structured,
    invoke_structured_or_freetext,
    missing_decision_labels,
    reset_fallback_events,
)


@pytest.fixture(autouse=True)
def _clean_events():
    reset_fallback_events()
    yield
    reset_fallback_events()


@pytest.mark.unit
class TestWarning:
    def test_the_marker_heads_the_report(self):
        out = free_text_with_warning("Trader", "FINAL TRANSACTION PROPOSAL: **HOLD**")
        assert out.startswith(FREETEXT_FALLBACK_MARKER)
        assert out.endswith("FINAL TRANSACTION PROPOSAL: **HOLD**")
        assert fallback_events()[-1][0] == "Trader"

    def test_a_missing_decision_label_is_named_in_the_report(self):
        out = free_text_with_warning("Trader", "I would wait and see.")
        assert "required decision label(s) missing" in out
        assert "FINAL TRANSACTION PROPOSAL" in out

    def test_the_marker_is_not_duplicated(self):
        once = free_text_with_warning("Portfolio Manager", "**Rating**: Hold")
        twice = free_text_with_warning("Portfolio Manager", once)
        assert twice == once
        assert twice.count(FREETEXT_FALLBACK_MARKER) == 1


@pytest.mark.unit
class TestInvoke:
    def test_a_structured_failure_records_the_parser_reason(self, caplog):
        structured = MagicMock()
        structured.invoke.side_effect = ValueError("bad JSON from model")
        with caplog.at_level(logging.WARNING):
            assert invoke_structured(structured, "prompt", "Research Manager") is None
        assert "bad JSON from model" in caplog.text
        assert "WARNING" in caplog.text
        assert fallback_events() == []   # recorded once, when the fallback text is built

    def test_the_fallback_records_one_event_with_the_reason(self):
        structured = MagicMock()
        structured.invoke.side_effect = ValueError("parser gave up")
        plain = MagicMock()
        plain.invoke.return_value = MagicMock(
            content="**Recommendation**: Hold\n\n**Rationale**: x\n\n**Strategic Actions**: y"
        )

        out = invoke_structured_or_freetext(
            structured, plain, "prompt", lambda plan: plan, "Research Manager"
        )

        assert out.startswith(FREETEXT_FALLBACK_MARKER)
        events = fallback_events()
        assert len(events) == 1
        assert events[0][0] == "Research Manager"
        assert "parser gave up" in events[0][1]


@pytest.mark.unit
class TestDecisionLabels:
    def test_both_labels_present_is_clean(self):
        state = {
            "final_trade_decision": "**Rating**: Buy\n\n**Executive Summary**: x",
            "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **BUY**",
        }
        assert missing_decision_labels(state) == []

    def test_missing_labels_are_listed(self):
        missing = missing_decision_labels({
            "final_trade_decision": "Executive Summary only",
            "trader_investment_plan": "buy it",
        })
        assert any("Rating" in label for label in missing)
        assert any("FINAL TRANSACTION PROPOSAL" in label for label in missing)


@pytest.mark.unit
def test_strict_fails_on_a_fallback_even_when_the_labels_survived():
    from cli import run

    free_text_with_warning("Portfolio Manager", "**Rating**: Hold")
    state = {
        "final_trade_decision": "**Rating**: Hold",
        "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**",
    }
    assert run.strict_problems(state)   # any fallback is a strict failure


@pytest.mark.unit
def test_strict_fails_when_a_decision_label_is_missing():
    from cli import run

    assert run.strict_problems({
        "final_trade_decision": "**Rating**: Hold",
        "trader_investment_plan": "no transaction line",
    })


@pytest.mark.unit
def test_strict_is_clean_for_a_structured_run():
    from cli import run

    assert run.strict_problems({
        "final_trade_decision": "**Rating**: Hold",
        "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**",
    }) == []


@pytest.mark.unit
def test_a_non_decision_agent_falls_back_without_a_marker():
    out = free_text_with_warning("Sentiment Analyst", "**Overall Sentiment:** Neutral")
    assert out == "**Overall Sentiment:** Neutral"
    assert fallback_events() == []


@pytest.mark.unit
def test_a_provider_without_structured_output_is_not_a_degraded_run():
    from tradingagents.agents.structured import invoke_structured_or_freetext

    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="text from a provider without schemas")
    out = invoke_structured_or_freetext(None, plain, "prompt", lambda r: r, "Trader")
    assert out == "text from a provider without schemas"
    assert fallback_events() == []


@pytest.mark.unit
def test_the_fallback_warning_and_parser_reason_reach_the_run_log(tmp_path):
    from cli.run import _report_structured_fallbacks

    free_text_with_warning("Trader", "FINAL TRANSACTION PROPOSAL: **HOLD**", "bad JSON: x" * 50)
    log = tmp_path / "message_tool.log"

    _report_structured_fallbacks(log)

    text = log.read_text(encoding="utf-8")
    assert "WARNING: Trader structured output failed" in text
    assert "bad JSON" in text
    assert len(text) < 1000      # the reason is truncated to 200 chars


@pytest.mark.unit
def test_strict_mode_exits_1_after_a_fallback():
    import typer

    from cli.run import _enforce_strict

    free_text_with_warning("Trader", "FINAL TRANSACTION PROPOSAL: **HOLD**")
    with pytest.raises(typer.Exit) as exc:
        _enforce_strict(
            {"final_trade_decision": "**Rating**: Hold",
             "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**"},
            {"strict": True},
        )
    assert exc.value.exit_code == 1


@pytest.mark.unit
def test_strict_mode_is_clean_without_a_fallback_or_missing_label():
    from cli.run import _enforce_strict

    _enforce_strict(
        {"final_trade_decision": "**Rating**: Hold",
         "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**"},
        {"strict": True},
    )


@pytest.mark.unit
def test_the_forced_structural_failure_lands_in_the_report_stdout_and_strict(tmp_path, capsys):
    """The three P1-6 acceptance points, driven by a real PM node fallback."""
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.reporting import write_report_tree

    class _Structured:
        def invoke(self, prompt):
            raise ValueError("forced schema failure")

    llm = MagicMock()
    llm.with_structured_output.return_value = _Structured()
    llm.invoke.return_value = MagicMock(content="**Rating**: Hold\n\nEvidence is balanced.")

    state = {
        "company_of_interest": "515070.SS",
        "trade_date": "2026-11-13",
        "asset_type": "etf",
        "investment_plan": "**Recommendation**: Hold",
        "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**",
        "past_context": "",
        "risk_debate_state": {
            "history": "risk debate", "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "latest_speaker": "Judge",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "count": 1,
        },
    }
    result = create_portfolio_manager(llm)(state)
    decision = result["final_trade_decision"]
    assert decision.startswith(FREETEXT_FALLBACK_MARKER)

    write_report_tree(
        {"final_trade_decision": decision,
         "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**"},
        "515070.SS", tmp_path, settings=None, html=False,
    )
    report = (tmp_path / "5_portfolio" / "decision.md").read_text(encoding="utf-8")
    assert report.splitlines()[0] == FREETEXT_FALLBACK_MARKER

    from cli.run import _enforce_strict, _report_structured_fallbacks

    _report_structured_fallbacks(tmp_path / "message_tool.log")
    assert "WARNING: Portfolio Manager structured output failed" in capsys.readouterr().out

    import typer
    with pytest.raises(typer.Exit) as exc:
        _enforce_strict(result, {"strict": True})
    assert exc.value.exit_code == 1
