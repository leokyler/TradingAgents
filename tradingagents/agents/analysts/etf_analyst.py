"""ETF analyst: fund-level facts pre-fetched into the prompt.

A fund has no financial statements, no insiders and no earnings call, so the
company-research tools the fundamentals analyst uses return nothing for it. What
matters instead is the basket and the product: what the fund tracks, how the
basket is valued, whether the price is at a premium or a discount to NAV, and how
much of the index the disclosed holdings actually cover.

The three sources are fetched before the model is called and placed in the
prompt, the same pattern the sentiment analyst uses: the model reports on data it
was given rather than inventing holdings, and a provider that cannot call tools
still produces the report. Each fetch degrades to a placeholder, so one
unavailable source does not sink the run.
"""

import logging

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS
from tradingagents.dataflows.router import route_to_vendor

logger = logging.getLogger(__name__)


def _fetch(method: str, ticker: str, trade_date: str) -> str:
    """One fund-level source, or a placeholder naming what could not be read."""
    try:
        return route_to_vendor(method, ticker, trade_date)
    except Exception as exc:  # noqa: BLE001 - one source must not end the run
        logger.warning("ETF Analyst: %s failed for %s: %s", method, ticker, exc)
        return (
            f"<{method} unavailable: {type(exc).__name__}. This is not an absence "
            f"of the data, only a failed read; do not estimate it.>"
        )


def create_etf_analyst(llm):
    def etf_analyst_node(state):
        ticker = state["company_of_interest"]
        trade_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        profile = _fetch("get_etf_profile", ticker, trade_date)
        holdings = _fetch("get_etf_holdings", ticker, trade_date)
        valuation = _fetch("get_etf_index_fundamentals", ticker, trade_date)

        system_message = _build_system_message(
            ticker=ticker,
            trade_date=trade_date,
            profile=profile,
            holdings=holdings,
            valuation=valuation,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Report what your data supports; another agent decides the trade."
                    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
                    " " + NO_EXTERNAL_TOOLS +
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=trade_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        report = llm.invoke(prompt.format_messages(messages=state["messages"])).content
        return {
            "messages": [AIMessage(content=report)],
            "etf_report": report,
        }

    return etf_analyst_node


def _build_system_message(*, ticker: str, trade_date: str, profile: str,
                          holdings: str, valuation: str) -> str:
    return f"""You are an ETF analyst covering {ticker} as of {trade_date}. An exchange-traded fund is a basket, not a company: it has no income statement, no insiders and no earnings call. Everything you can know about it is in the three data blocks below, already collected for you.

## Data blocks

### Fund profile and pricing
<start_of_profile>
{profile}
<end_of_profile>

### Disclosed holdings
<start_of_holdings>
{holdings}
<end_of_holdings>

### Basket valuation
<start_of_valuation>
{valuation}
<end_of_valuation>

## How to analyze an ETF

1. **What the fund is.** Name the tracked index (or theme) from the fund name and the holdings you can see. Say plainly what exposure a buyer gets — broad index, sector, factor, cross-border, bond, gold or money-market — because the risk differs completely across those.
2. **Read the product type first.** An equity fund is valued on PE/PB and driven by its industries; a bond fund is driven by duration, credit and the yield curve; a commodity fund by the metal price and the roll; a money-market fund by short rates. When the valuation or holdings block says it does not apply, that is the product's nature, not missing data — do not force an equity frame onto it.
3. **Valuation of the basket.** Use the capital-weighted aggregate PE/PB/ROE when the block provides them. State the coverage: the figures describe the disclosed slice, not the whole index. Never present a top-10 aggregate as the index multiple.
4. **Industry exposure.** Name the top industries and their combined weight from the industry table, and say what macro drivers that ties the fund to. This is what the fund is actually long: it should carry more weight in your conclusion than the chart's shape.
5. **Premium/discount.** Compare price with the latest published NAV. A discount is not automatically cheap and a premium is not automatically expensive: say what it means for entry, and note that the NAV may be the previous close rather than a live IOPV.
6. **Size and liquidity.** Use fund size, shares outstanding, turnover and traded amount to judge whether a position can be entered and exited without moving the price. Small funds and thin turnover are a distinct risk from index direction.
7. **Concentration.** Use the disclosed weights to say how concentrated the top holdings are, and what single names or industries drive the fund's return.
8. **What you cannot see.** Be explicit when something is withheld, unavailable, or only quarterly. Absence of data is not evidence of absence, and an unavailable block must not be filled in from memory. Industry weights cover only the disclosed, valued slice, so state that when you quote them.

## Output

Write a detailed report titled for {ticker}, in these sections:
- **Fund overview**: what it tracks, product type, size and liquidity
- **Basket valuation**: PE/PB/ROE with the coverage caveat
- **Holdings, industry exposure and concentration**: the disclosed weights, the top industries and their combined weight, and what they imply
- **Premium/discount and trading considerations**: entry, liquidity, tracking
- **Risks specific to this ETF**: product, liquidity, index and (where relevant) currency
- **Bottom line**: what the evidence supports for a trader

End with a Markdown table summarizing the key metrics.

{get_language_instruction()}"""
