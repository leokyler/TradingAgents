# A-share ETFs

TradingAgents treats a Shanghai- or Shenzhen-listed ETF as a fund, a basket rather than a company, so it runs a fund-aware analyst and reads fund-level data from A-share vendors.

## Detection

A ticker is an A-share ETF when its canonical symbol carries the fund-code range of its exchange:

| Exchange | Suffix | Fund codes | Examples |
|---|---|---|---|
| Shanghai | `.SS` | `51xxxx`, `56xxxx`, `58xxxx` | `510300.SS`, `588000.SS` |
| Shenzhen | `.SZ` | `159xxx` | `159919.SZ` |

Detection is syntactic on purpose. Yahoo classifies Chinese ETFs as `quoteType=EQUITY` and returns "No Fund data found" for them, so neither `quoteType` nor Yahoo's fund data can tell an A-share ETF from a stock; the code range can, with no network call. A bare six-digit code carries no exchange and is not treated as an ETF. Shenzhen LOFs (`16xxxx`) are deliberately excluded: they trade like closed-end funds and their pricing is not an index basket.

A US ETF such as `SPY` is not an A-share ETF and is not routed to the ETF path: it stays on the stock pipeline, with the stock analysts and the configured data vendors; the ETF analyst and the Eastmoney fund data do not apply to it.

## Analyst team

Each asset type offers the analysts it can use:

| Asset type | Analysts |
|---|---|
| ETF | Market, ETF, News |
| Stock | Market, Sentiment, News, Fundamentals |
| Crypto | Market, Sentiment, News |

An ETF run drops the fundamentals and sentiment analysts because a fund files no company statements and has no US retail cashtag stream. The ETF analyst is only offered for ETFs. In the interactive CLI the ETF menu therefore shows exactly Market, ETF and News; a backtest without `--analysts` runs all three.

## What the ETF analyst reads

Before the model is called, three fund-level blocks are fetched and placed in the prompt, the same pattern the sentiment analyst uses: the model reports on data it was given rather than inventing holdings, and a block that cannot be read becomes a placeholder naming what failed, not a made-up value. By default all three come from Eastmoney's public fund endpoints (no key), which is where the fund-level facts live — Yahoo has no fund data for Chinese ETFs:

- **Profile**: price, shares outstanding, fund size (shares × price), the latest published NAV and the premium/discount it implies, and turnover.
- **Disclosed holdings**: the newest quarterly top holdings and the weight they cover. A bond or money-market fund falls back to its bond table; a commodity fund discloses neither and says so.
- **Basket valuation**: capital-weighted PE/PB/ROE over the disclosed constituents, plus the industry weights they imply, with the coverage stated — a top-10 aggregate is not the index.

The ETF report is injected into the bull/bear and risk debates, so "what is this fund actually long" becomes an argument, not a footnote. The NAV is the latest *published* one, not a live IOPV: an intraday run compares a live price with the previous close NAV, so the premium it implies is an estimate.

A configured `tushare` profile vendor replaces the profile block with exchange-sourced NAV and shares outstanding; the holdings and basket valuation blocks remain Eastmoney.

## Data vendors

A-share prices and fund facts come from A-share vendors rather than the framework's default Yahoo path. Configure the chains per data category with `TRADINGAGENTS_DATA_VENDORS`, a JSON object mapping a category to an ordered, comma-separated list of vendors:

```bash
export TRADINGAGENTS_DATA_VENDORS='{"core_stock_apis": "cn_market,yfinance", "technical_indicators": "cn_market,yfinance"}'
```

The configuration categories that matter here are `core_stock_apis` (daily bars), `technical_indicators` (indicators) and `etf_data` (fund profile, disclosed holdings and basket valuation). The setting is merged per category onto the defaults, so naming one chain does not drop the others; a category left unset keeps its default. Without any override, `core_stock_apis` and `technical_indicators` are `yfinance`, and `etf_data` is `eastmoney`. The same chains can be set programmatically through `data_vendors` on the config.

The configured chain is exact: requests are not silently routed to vendors you did not choose. Vendors are tried in the order given, and the first that can serve the call wins; one that reports no data, is unavailable or is not configured (a missing token, for example) falls through to the next. When every vendor in the chain reports no data or is unavailable, the analyst is told the data is unavailable and not to estimate it rather than receiving a fabricated value.

The vendors that matter here:

- `cn_market` — keyless. Serves daily bars and indicators for Shanghai and Shenzhen listings from public China endpoints (Eastmoney, then Tencent, then Sina); any other symbol is refused so the chain falls through to Yahoo. A blocked or throttled Yahoo therefore does not cost an A-share run its prices.
- `tushare` — keyed. Set `TUSHARE_TOKEN`, then list it in the chains. It serves daily bars and indicators for stocks and funds, plus the ETF profile (published NAV, shares outstanding, fund size, premium/discount). Without a token it counts as not configured, so a chain with a fallback behind it moves on.
- `eastmoney` — keyless. Serves the ETF fund-level blocks: profile, disclosed holdings and basket valuation. It refuses a symbol that is not an A-share listed fund.
- `yfinance` — the framework's default for every other symbol; keep it after the A-share vendors in a mixed chain.

With a Tushare token, the example chain is:

```bash
export TUSHARE_TOKEN=...
export TRADINGAGENTS_DATA_VENDORS='{"core_stock_apis": "cn_market,tushare,yfinance", "technical_indicators": "cn_market,tushare,yfinance", "etf_data": "tushare,eastmoney"}'
```

`tushare` in `etf_data` serves the profile only: the chain is filtered per call, so holdings and basket valuation still come from Eastmoney until a Tushare equivalent lands. The order inside a chain is the preference order, so `cn_market,tushare,yfinance` asks the keyless CN endpoints first and Tushare second; list `tushare` first (`"tushare,cn_market,yfinance"`) to make it the primary price source with `cn_market` as the fallback.

## Running an analysis

```bash
tradingagents --ticker 510300.SS --date 2026-11-12 --analysts market,etf,news
```

The analysis CLI detects the ETF from the ticker and announces the type when it is not a stock. `--analysts` skips the analyst prompt; omit it to choose from the menu. `tradingagents backtest` does not detect the type from the ticker, so pass `--asset-type etf` for an ETF grid.

The date above is in the past, so this particular run gets the historical treatment below: the Eastmoney profile and basket valuation are withheld, and the report says so rather than showing present-day figures. Use today's date to see the live blocks.

## Point-in-time rules

A historical run is restricted by the same point-in-time rules as the rest of the data layer.

- Holdings: a historical run takes the newest disclosed quarter that had closed by its date. If the vendor serves no quarter that had closed by then, holdings are withheld rather than showing a quarter published later. Holdings are a quarterly disclosure in every case: weights drift with prices and with creations/redemptions.
- Eastmoney profile and basket valuation: the live profile and the present-day valuation multiples are withheld for a historical run. The quote, NAV and fund-size figures are present-day snapshots with no filing date, and the PE/PB of the constituents are present-day values, so showing either for a past date would leak today's data into a past decision. The run's price history still comes from the market data vendor.
- Tushare profile: the profile is assembled from dated series instead. NAV and shares are filtered to rows dated on or before the run date and, where the endpoint reports it, an announcement date on or before the run date too (`nav_date`/`trade_date` with `ann_date`); daily bars are cut at the run date. With `tushare` first in `etf_data`, a historical run therefore gets what had been published then, not a withheld notice.

## The evidence gate

Independently of the asset type, a run that cannot read a price series for its instrument is published as **REVIEW** rather than Buy/Hold/Sell. The debate always produces an opinion; the gate refuses to present one that no price data supports.

The gate runs when a decision is recorded, so the Python API and the CLI both publish REVIEW, and the report, the returned rating and the memory log agree. It asks the configured price vendors for a 30-day window ending on the analysis date. An explicit "no data" answer — every vendor answered with no usable series, or every vendor was throttled or unavailable — rewrites the decision's opening rating to REVIEW and appends a note saying no usable price data could be read on that date. A run that already carries REVIEW stays there without a note.

Technical evidence exists whenever prices exist, so a run with prices but no news or fundamentals is still a call. The gate is deliberately narrow: it asks only whether the instrument can be priced, not whether the reports are good.

Two behaviours follow from the probe running after the graph:

- It reads the series a second time, so it can disagree with what the run itself saw; the gate takes the later answer, because a run whose prices are unreachable now is the one a reader would re-run.
- A probe that fails with an exception is not a verdict: the rating stands, so a transient blip does not cost the whole run. An exhausted vendor chain is a data sentinel rather than an exception, so it does count as no data — the conservative reading.

The gate is on by default. Set `evidence_gate: false` in the config, or `TRADINGAGENTS_EVIDENCE_GATE=0`, to turn it off. The variable accepts true/1/yes/on and false/0/no/off; an invalid value stops the run at start-up rather than guessing.

A backtest cell published as REVIEW has no direction to score, so it is counted as unscored rather than as a rating.
