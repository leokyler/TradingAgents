"""The complete report as data, and its plain-text rendering for a pipe.

``--show`` used to go through rich's Panel/Markdown renderer even when stdout
was a pipe, and the process kept drawing after the reports were saved. This
module is the fork's own home for the report's section order: the rich live view
and the plain pipe path both read the same groups, so adding a report section is
one edit, not two, and a pipe gets the full text with no rich renderer at all.
"""

from __future__ import annotations

import sys


def report_groups(final_state) -> list[tuple[str, list[tuple[str, str]]]]:
    """The report's sections in display order: ``(heading, [(author, text), ...])``.

    Empty sections are dropped, so the same list drives the rich panels and the
    plain pipe output; a report key added here appears in both.
    """
    groups: list[tuple[str, list[tuple[str, str]]]] = []

    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("etf_report"):
        analysts.append(("ETF Analyst", final_state["etf_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analysts:
        groups.append(("I. Analyst Team Reports", analysts))

    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if final_state.get("investment_plan"):
            research.append(("Research Manager", final_state["investment_plan"]))
        if research:
            groups.append(("II. Research Team Decision", research))

    if final_state.get("trader_investment_plan"):
        groups.append(
            ("III. Trading Team Plan", [("Trader", final_state["trader_investment_plan"])])
        )

    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            groups.append(("IV. Risk Management Team Decision", risk_reports))

    if final_state.get("final_trade_decision"):
        groups.append(
            ("V. Portfolio Manager Decision",
             [("Portfolio Manager", final_state["final_trade_decision"])])
        )

    return groups


def stdout_is_a_terminal(console) -> bool:
    """Whether the complete report can be drawn with rich, read at call time.

    ``console.is_terminal`` is a property over the stream rich writes to, so a
    console created at import still answers for the stdout of the moment; a pipe
    seen directly on stdout counts as non-terminal either way.
    """
    try:
        if not console.is_terminal:
            return False
        isatty = getattr(sys.stdout, "isatty", None)
        return bool(isatty()) if isatty is not None else False
    except (AttributeError, ValueError):
        return False


def print_plain_report(final_state) -> None:
    """Write the complete report as plain text, for stdout that is not a terminal.

    No Panel, Markdown, Live or pager: those can wait on a pipe once the reports
    are saved. The flush at the end gives a pipe every line before the process
    exits.
    """
    print("Complete Analysis Report")
    print("=" * 24)
    for heading, reports in report_groups(final_state):
        print()
        print(heading)
        print("-" * len(heading))
        for title, content in reports:
            print()
            print(title)
            print(content if isinstance(content, str) else str(content))
    sys.stdout.flush()
