# Maintaining this fork

This repository is a fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents). It adds four things on top of upstream, and it is merged with upstream periodically, so the shape of the additions matters as much as what they do:

- an A-share ETF workflow — [`a-share-etf.md`](a-share-etf.md)
- an OpenCode Go LLM provider — [`opencode-go.md`](opencode-go.md)
- keyless and keyed A-share data vendors with an ordered, configurable chain — [`a-share-etf.md`](a-share-etf.md#data-vendors)
- a local benchmark harness — [`../bench/README.md`](../bench/README.md)

## The rule

**Upstream files are additive only.** A new capability means a new module plus the smallest registration the shared code needs; it does not mean rewriting an upstream function to call it.

The reason is mechanical: git conflicts on *lines*, and a fork that deletes an upstream block re-conflicts every time upstream edits that block — even when the two changes are unrelated. A dict entry appended at the end of a registry merges cleanly almost every time. The fork's whole upstream footprint is currently **47 deleted lines** across 12 files, down from 160; 118 of those were one function whose body had been moved into a new module.

## Where the fork's own code lives

| Module | What it owns |
|---|---|
| `tradingagents/agents/asset_profiles.py` | per-asset-type wording (one row per type, replaces conditionals in five prompts) |
| `tradingagents/agents/evidence.py` | the REVIEW gate for runs with no price series |
| `tradingagents/agents/analysts/etf_analyst.py` | the ETF analyst |
| `tradingagents/dataflows/etf.py` | A-share fund-code detection |
| `tradingagents/dataflows/indicators.py` | indicator rendering shared by the A-share price vendors |
| `tradingagents/dataflows/prices.py` | the price CSV format, written and read back |
| `tradingagents/dataflows/vendors/transport.py` | proxy-bypass session, request spacing, per-host breaker, browser fingerprint |
| `tradingagents/dataflows/vendors/{cn_market,tushare,eastmoney}.py` | the three data vendors |
| `tradingagents/llm_clients/opencode_go.py` | the gateway's `ProviderSpec` and client (registers itself on import) |

## The upstream edit points that remain

Every entry below is a deliberate edit to an upstream file, and the list is meant to stay short and greppable. Before adding a sixth one, check whether a new module plus one line will do instead.

| File | What it is | Why it cannot be additive |
|---|---|---|
| `tradingagents/dataflows/vendors/yahoo/market.py` | **untouched** — the fork keeps its own copy of the indicator table | (reverted: this was the 118-line deletion) |
| `cli/prompts.py` | `filter_analysts_for_asset_type` body is a table lookup | the rule itself changed: a stock run now excludes the ETF analyst |
| `tradingagents/agents/context.py` | asset labels read `profile_for(...)` | three conditionals are replaced by one lookup |
| `agents/researchers/{bull,bear}_researcher.py` | label block reads `profile_for(...)` | same, and the prompt strings above it stay upstream-identical |
| `agents/analysts/news_analyst.py` | one label line | same |
| `dataflows/vendors/yahoo/snapshot.py` | `render_verified_snapshot` extracted from the builder | the snapshot must be renderable from any vendor's frame |
| `agents/tools.py` | the two error branches try the routed vendor first | the tool's behaviour changed, not its plumbing |
| `llm_clients/factory.py` | the effort-provider tuple gains a member; a tier's effort overrides the shared one | a one-tuple edit and a default cascade |
| `llm_clients/openai_client.py` | one condition: the native-OpenAI model-name gate skips the gateway | the gateway accepts effort levels native OpenAI would reject |
| `graph/trading_graph.py` | `record_decision` runs the gate; the state log carries `etf_report`; one warning when a fund run selects no ETF analyst | the gate must sit on the path both the CLI and the API share |
| `cli/{main,display}.py` | one help string, and the analyst registries' entries | additive, listed for completeness |
| `default_config.py` | new keys only (`evidence_gate`, the tier efforts, `etf_data`) | purely additive |

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
