"""The report a pipe receives, and the analyst errors that name the choices.

`--show` piped into `tee` must still contain the whole report (rich's renderer
can wait on a pipe after the reports are saved), and an analyst the asset type
cannot use must say which ones it can.
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from cli import display
from cli.display import display_complete_report
from cli.models import AssetType
from cli.prompts import parse_analysts

# Every section display_complete_report knows about, with text unique to it.
REPORT_STATE = {
    "market_report": "MARKET-REPORT-TEXT",
    "etf_report": "ETF-REPORT-TEXT",
    "news_report": "NEWS-REPORT-TEXT",
    "investment_debate_state": {
        "bull_history": "BULL-ARGUMENT-TEXT",
        "bear_history": "BEAR-ARGUMENT-TEXT",
        "history": "",
        "current_response": "",
        "count": 0,
    },
    "investment_plan": "RESEARCH-MANAGER-TEXT",
    "trader_investment_plan": "TRADER-PLAN-TEXT",
    "risk_debate_state": {
        "aggressive_history": "AGGRESSIVE-TEXT",
        "conservative_history": "CONSERVATIVE-TEXT",
        "neutral_history": "NEUTRAL-TEXT",
        "history": "",
        "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "count": 0,
    },
    "final_trade_decision": "PORTFOLIO-MANAGER-TEXT",
}

# The order the report is read in.
FLOW = [
    "I. Analyst Team Reports",
    "Market Analyst", "MARKET-REPORT-TEXT",
    "ETF Analyst", "ETF-REPORT-TEXT",
    "News Analyst", "NEWS-REPORT-TEXT",
    "II. Research Team Decision",
    "Bull Researcher", "BULL-ARGUMENT-TEXT",
    "Bear Researcher", "BEAR-ARGUMENT-TEXT",
    "Research Manager", "RESEARCH-MANAGER-TEXT",
    "III. Trading Team Plan",
    "Trader", "TRADER-PLAN-TEXT",
    "IV. Risk Management Team Decision",
    "Aggressive Analyst", "AGGRESSIVE-TEXT",
    "Conservative Analyst", "CONSERVATIVE-TEXT",
    "Neutral Analyst", "NEUTRAL-TEXT",
    "V. Portfolio Manager Decision",
    "Portfolio Manager", "PORTFOLIO-MANAGER-TEXT",
]

MARKUP_CHARS = ("╭", "╰", "│", "─", "┃", "━")


@pytest.mark.unit
def test_a_piped_report_is_plain_text_that_keeps_every_section_in_order(
    monkeypatch, capsys
):
    """The report used to hang the process when stdout was a pipe."""
    non_terminal = io.StringIO()
    monkeypatch.setattr(display, "console", Console(file=non_terminal))

    display_complete_report(REPORT_STATE)  # returns; a hang would never reach here

    out = capsys.readouterr().out
    assert "Complete Analysis Report" in out
    positions = [out.index(text) for text in FLOW]
    assert positions == sorted(positions)
    assert "\x1b" not in out  # no ANSI escapes
    assert not [char for char in MARKUP_CHARS if char in out]


@pytest.mark.unit
def test_a_piped_report_is_plain_when_stdout_itself_is_not_a_tty(monkeypatch):
    """A console that still reports a terminal must not override a piped stdout."""
    pipe = io.StringIO()
    terminal_console = Console(force_terminal=True, width=100, height=40, file=pipe)
    monkeypatch.setattr(display, "console", terminal_console)
    monkeypatch.setattr(sys, "stdout", pipe)

    display_complete_report(REPORT_STATE)

    out = pipe.getvalue()
    assert "MARKET-REPORT-TEXT" in out and "PORTFOLIO-MANAGER-TEXT" in out
    assert "\x1b" not in out


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.unit
def test_at_a_terminal_the_rich_report_is_unchanged(monkeypatch):
    """The visual path still renders panels; only a pipe takes the plain path."""
    fake_tty = _FakeTty()
    monkeypatch.setattr(sys, "stdout", fake_tty)
    monkeypatch.setattr(
        display, "console",
        Console(force_terminal=True, width=100, height=40, file=fake_tty),
    )

    display_complete_report(REPORT_STATE)

    rendered = fake_tty.getvalue()
    assert "Complete Analysis Report" in rendered
    assert "MARKET-REPORT-TEXT" in rendered
    assert "╭" in rendered  # the rich panels are still used at a terminal


@pytest.mark.unit
def test_an_analyst_an_etf_cannot_use_names_the_ones_it_can():
    with pytest.raises(ValueError) as caught:
        parse_analysts("social", AssetType.ETF)

    message = str(caught.value)
    assert "social is not available for etf" in message
    choices = message.split("choose from", 1)[1]
    for name in ("etf", "market", "news"):
        assert name in choices


@pytest.mark.unit
def test_a_stock_rejects_the_etf_analyst_and_names_the_stock_set():
    with pytest.raises(ValueError) as caught:
        parse_analysts("etf", AssetType.STOCK)

    message = str(caught.value)
    assert "etf is not available for stock" in message
    choices = message.split("choose from", 1)[1]
    assert "etf" not in choices
    for name in ("market", "news", "fundamentals", "sentiment"):
        assert name in choices


# What the unattended path needs; nothing here reaches the network.
UNATTENDED_ENV = {
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
    "TRADINGAGENTS_LLM_PROVIDER": "openai",
    "TRADINGAGENTS_QUICK_THINK_LLM": "gpt-6-luna",
}


@pytest.mark.unit
def test_the_command_exits_one_and_names_the_available_analysts(monkeypatch):
    from typer.testing import CliRunner

    from cli import main as cli_main

    for name, value in UNATTENDED_ENV.items():
        monkeypatch.setenv(name, value)

    result = CliRunner().invoke(
        cli_main.app,
        ["--ticker", "515070.SS", "--date", "2024-06-03", "--analysts", "social",
         "--save", "--no-show", "--no-html"],
    )

    assert result.exit_code == 1, result.output
    assert "social is not available for etf" in result.output
    choices = result.output.split("choose from", 1)[1]
    for name in ("etf", "market", "news"):
        assert name in choices


@pytest.mark.unit
def test_a_pipe_run_does_not_start_the_rich_live_view(monkeypatch):
    """A pipe has no alternate screen; Live is skipped so its refresh thread and
    teardown cannot delay the exit after the report is written."""
    import contextlib

    from cli import run

    monkeypatch.setattr(run, "stdout_is_a_terminal", lambda console: False)
    assert isinstance(run._live_view(object()), contextlib.nullcontext)

    monkeypatch.setattr(run, "stdout_is_a_terminal", lambda console: True)
    assert isinstance(run._live_view(object()), run.Live)
