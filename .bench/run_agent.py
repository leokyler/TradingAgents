#!/usr/bin/env python3
"""Run the TradingAgents CLI and dump LLM call/token/latency stats at exit.

Wraps the CLI in-process so the same StatsCallbackHandler the UI uses can be
read after the run; writes JSON to BENCH_STATS_PATH.
"""

import atexit
import json
import os
import statistics
import sys
import threading
import time

# The venv also has a stale site-packages copy of this project; put the working
# tree first so the benchmark measures the code being edited.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli.run as runmod

_handlers = []
_calls = []          # (start_monotonic, end_monotonic)
_open_starts = []
_lock = threading.Lock()
_orig = runmod.StatsCallbackHandler


class TrackingStats(_orig):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _handlers.append(self)

    def on_chat_model_start(self, *a, **k):
        with _lock:
            _open_starts.append(time.monotonic())
        return super().on_chat_model_start(*a, **k)

    def on_llm_start(self, *a, **k):
        with _lock:
            _open_starts.append(time.monotonic())
        return super().on_llm_start(*a, **k)

    def on_llm_end(self, *a, **k):
        with _lock:
            start = _open_starts.pop() if _open_starts else None
            if start is not None:
                _calls.append(time.monotonic() - start)
        return super().on_llm_end(*a, **k)


runmod.StatsCallbackHandler = TrackingStats


def _dump():
    tot = {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
    for h in _handlers:
        s = h.get_stats()
        for k in tot:
            tot[k] += s.get(k, 0)
    lat = sorted(_calls)
    out = {
        **tot,
        "llm_calls_timed": len(lat),
        "latency_s_p50": round(statistics.median(lat), 2) if lat else None,
        "latency_s_p90": round(lat[int(len(lat) * 0.9)], 2) if lat else None,
        "latency_s_max": round(lat[-1], 2) if lat else None,
        "latency_s_total": round(sum(lat), 1) if lat else None,
    }
    path = os.environ.get("BENCH_STATS_PATH")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    print("\n[bench] " + json.dumps(out), flush=True)


atexit.register(_dump)

from cli.main import app

if __name__ == "__main__":
    app()
