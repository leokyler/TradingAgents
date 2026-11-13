#!/usr/bin/env bash
# One monitored ETF analysis run. All artifacts stay under bench/ so the
# workspace sandbox can write them and the run is reproducible.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
LABEL="${LABEL:-etf-515070-2026-11-12}"
TICKER="${TICKER:-515070.SS}"
DATE="${DATE:-2026-11-12}"
ANALYSTS="${ANALYSTS:-market,etf,news}"

export TRADINGAGENTS_RESULTS_DIR="$PWD/bench/results"
export TRADINGAGENTS_CACHE_DIR="$PWD/bench/cache"
export TRADINGAGENTS_MEMORY_LOG_PATH="$PWD/bench/memory/trading_memory.md"
export TRADINGAGENTS_OUTPUT_LANGUAGE="${TRADINGAGENTS_OUTPUT_LANGUAGE:-Chinese}"
export TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
export TRADINGAGENTS_MAX_RISK_ROUNDS=1
export TRADINGAGENTS_NO_UPDATE_CHECK=1
export BENCH_STATS_PATH="$PWD/bench/logs/${LABEL}.tokens.json"
export PYTHONUNBUFFERED=1

exec "$PYTHON" bench/monitor.py --out bench/logs --label "$LABEL" -- \
  "$PYTHON" bench/run_agent.py \
    --ticker "$TICKER" --date "$DATE" --analysts "$ANALYSTS" --save --no-show
