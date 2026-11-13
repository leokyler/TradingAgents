# Benchmark harness

One-off, local performance measurement of a full analysis run. This is not part
of the test suite and not run in CI: results depend on the machine and the live
market data, so they are only meaningful as a before/after comparison on the
same box.

## Entry points

- `run_etf.sh` — end-to-end: one monitored ETF analysis through the CLI.
  Override with `TICKER`, `DATE`, `LABEL`, `ANALYSTS` and `PYTHON`
  (default `.venv/bin/python`).
- `monitor.py --out DIR --label NAME -- <command...>` — runs and samples any
  command's process tree (RSS/PSS, threads, CPU, block-IO, network) every
  `--interval` seconds; writes `<label>.log`, `<label>.csv` and
  `<label>.json` into `DIR`.
- `run_agent.py` — in-process CLI wrapper that records LLM call counts, token
  usage and latency percentiles, writing JSON to `$BENCH_STATS_PATH` and stdout.
  It reads the CLI's `StatsCallbackHandler`, i.e. CLI internals; if that class is
  renamed or moved, update the lookup here.

## Output

Monitor logs, CSVs, summaries and token stats land in `bench/logs/`; analysis
artifacts land in `bench/results/`. Both directories are gitignored.

Linux only: `monitor.py` reads `/proc` for process and system metrics.
