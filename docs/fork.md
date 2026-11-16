# Maintaining this fork

This repository is a fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents). It adds four things on top of upstream, and it is merged with upstream periodically, so the shape of the additions matters as much as what they do:

- an A-share ETF workflow — [`a-share-etf.md`](a-share-etf.md)
- an OpenCode Go LLM provider — [`opencode-go.md`](opencode-go.md)
- keyless and keyed A-share data vendors with an ordered, configurable chain — [`a-share-etf.md`](a-share-etf.md#data-vendors)
- a local benchmark harness — [`../bench/README.md`](../bench/README.md)

## The rule

**Upstream files are additive only.** A new capability means a new module plus the smallest registration the shared code needs; it does not mean rewriting an upstream function to call it.

The reason is mechanical: git conflicts on *lines*, and a fork that deletes an upstream block re-conflicts every time upstream edits that block — even when the two changes are unrelated. A dict entry appended at the end of a registry merges cleanly almost every time. The footprint is what the sync check below prints; every *source* file it lists is in the edit-point table, and the test files it lists are only those whose pinned behaviour necessarily changed.

## Where the fork's own code lives

| Module | What it owns |
|---|---|
| `tradingagents/agents/asset_profiles.py` | per-asset-type wording (one row per type, replaces conditionals in five prompts) |
| `tradingagents/agents/evidence.py` | the REVIEW gate for runs with no price series |
| `tradingagents/agents/analysts/etf_analyst.py` | the ETF analyst |
| `tradingagents/agents/analysts/etf_price.py` | the ETF price-window block (deterministic price/liquidity facts) |
| `tradingagents/agents/fallback.py` | what a structured-output fallback means (marker, events, label checks, strict policy) |
| `tradingagents/dataflows/dates.py` | the shared `YYYY-MM-DD` parser for the fork's new data paths |
| `tradingagents/dataflows/units.py` | unit wording (万手/亿份/亿元 vs lots/shares/CNY) for the data blocks |
| `tradingagents/dataflows/etf.py` | A-share fund-code detection |
| `tradingagents/dataflows/snapshot_equivalence.py` | when a live-only fund snapshot is point-in-time equivalent |
| `tradingagents/dataflows/indicators.py` | indicator rendering shared by the A-share price vendors |
| `tradingagents/dataflows/prices.py` | the price CSV format, written and read back |
| `tradingagents/dataflows/vendors/transport.py` | proxy-bypass session, request spacing, per-host breaker, browser fingerprint |
| `tradingagents/dataflows/vendors/{cn_market,tushare,eastmoney}.py` | the three data vendors |
| `tradingagents/dataflows/vendors/cn_news.py` | keyless A-share news (optional `akshare`) |
| `tradingagents/dataflows/vendors/csindex.py` | keyless CSI index valuation, the second source for the basket block |
| `tradingagents/llm_clients/opencode_go.py` | the gateway's `ProviderSpec` and client (registers itself on import) |
| `tradingagents/env.py` | the shared `~/.tradingagents/.env` loader, the pasted-key target, and the files `tradingagents env` reports on |
| `cli/env_cmd.py` | the `tradingagents env` report (registered by one line in `cli/main.py`) |
| `cli/plain_report.py` | the complete report's section order and its plain-text rendering for a piped `--show` |

## The upstream edit points that remain

Every entry below is a deliberate edit to an upstream file, and the list is meant to stay short and greppable. Before adding a sixth one, check whether a new module plus one line will do instead.

| File | What it is | Why it cannot be additive |
|---|---|---|
| `tradingagents/dataflows/vendors/yahoo/market.py` | **untouched** — the fork keeps its own copy of the indicator table | (reverted: this was the 118-line deletion) |
| `cli/prompts.py` | `filter_analysts_for_asset_type` body is a table lookup; the pasted-key target is one expression (`api_key_target`) | the rule itself changed: a stock run now excludes the ETF analyst |
| `tradingagents/agents/context.py` | asset labels read `profile_for(...)` | three conditionals are replaced by one lookup |
| `agents/researchers/{bull,bear}_researcher.py` | label block reads `profile_for(...)` | same, and the prompt strings above it stay upstream-identical |
| `agents/analysts/news_analyst.py` | one label line, plus one brief paragraph requiring dated/sourced news | the analyst's output contract changed, not its plumbing |
| `dataflows/vendors/yahoo/snapshot.py` | `render_verified_snapshot` extracted from the builder | the snapshot must be renderable from any vendor's frame |
| `dataflows/vendors/yahoo/common.py` | 429 is remembered per process: one retry, then fail fast | the retry policy itself changed (P1-4); a new module cannot intercept `yf_retry` otherwise |
| `dataflows/vendors/{yahoo,alpha_vantage}/news.py` | global news accepts an optional `symbol` | symbol-gated `cn_news` needs the run's instrument; the other vendors ignore it |
| `dataflows/router.py` | an explicitly empty *news* chain returns "no news vendor configured" instead of walking the default; registers `csindex` for the valuation method | the short-circuit and the vendor registry must sit where the chain is read |
| `agents/tools.py` | the two error branches try the routed vendor first; global news passes the symbol | the tool's behaviour changed, not its plumbing |
| `agents/structured.py` | re-exports the fallback policy and records a failure | the policy lives in `agents/fallback.py`; this upstream file keeps its shape |
| `agents/managers/portfolio_manager.py` | the provider-without-schema branch keeps its raw text; a real failure goes through the fallback policy | the PM's own fallback branch must tell the two apart |
| `llm_clients/factory.py` | the effort-provider tuple gains a member; a tier's effort overrides the shared one | a one-tuple edit and a default cascade |
| `llm_clients/openai_client.py` | one condition: the native-OpenAI model-name gate skips the gateway | the gateway accepts effort levels native OpenAI would reject |
| `graph/trading_graph.py` | `record_decision` runs the gate; the state log carries `etf_report`; one warning when a fund run selects no ETF analyst | the gate must sit on the path both the CLI and the API share |
| `cli/{main,display}.py` | one help string, the analyst registries' entries, the `env` command registration, the `--strict` flag, and one non-tty branch calling `cli/plain_report.py` | additive, listed for completeness |
| `cli/run.py` | `--strict` exit code, the fallback warning line, the A-share ETF `cn_news` chain, the start-up akshare warning, and the non-tty Live skip | one post-run check and a few per-run CLI decisions; the policies live in the fork modules |
| `tradingagents/__init__.py` | a few added lines at the end call `load_user_env()` after the two upstream loaders | additive, listed for completeness |
| `default_config.py` | new keys only (`evidence_gate`, the tier efforts, `etf_data`) | purely additive; `news_data` stays the upstream `yfinance` chain |

## Syncing

```bash
git fetch upstream
git rebase upstream/main            # or merge, if you prefer to keep hashes
```

Then prove the fork is still shaped right:

```bash
# 1. Nothing outside the documented edit points deletes upstream lines.
git diff upstream/main...HEAD --diff-filter=M --numstat | awk '$2 > 0'

# 2. The repo is still clean under the rules CI enforces.
ruff check .
pytest -q
```

If the first command lists a file that is not in the table above, the merge brought in — or the change added — an edit that will cost again next time. Prefer moving it into a fork module.

## Notes

- The fork keeps the upstream `main` history intact; work happens on feature branches cut from it.
- `bench/results/` and `bench/logs/` are ignored: the harness is source, its output is not.
- `dataflows/vendors/transport.py` is deliberately *not* `dataflows/net.py`: upstream owns that module (it scrubs secrets out of errors and probes reachability), and the fork does not edit it.
