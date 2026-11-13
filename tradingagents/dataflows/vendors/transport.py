"""HTTP transport for the fork's mainland-China vendors.

Eastmoney, Tencent, Sina and Tushare each need the same three things, and each
grew its own copy:

* a session that ignores proxy environment variables — a proxy in front of a
  mainland endpoint answers with an empty reply rather than an error, and a
  developer machine outside China usually has one configured;
* transport failures mapped to ``VendorUnavailableError``, so the router can
  move down the vendor chain instead of the run dying;
* for the scraped endpoints, request spacing and a per-host cooldown. Their edge
  closes connections on a burst and keeps an IP in a penalty box for hours
  (observed: a morning of runs, then hours of resets, then recovery), so a
  failed host is skipped rather than retried.

``get`` is that transport. The browser-fingerprint option is not cosmetic:
Eastmoney's edge rejects a plain requests/http.client handshake whatever headers
are sent, and ``curl_cffi`` replays a browser ClientHello instead. It is a
declared dependency; the fallback keeps an install without it working.

This module is deliberately separate from ``dataflows.net`` (the upstream helper
that scrubs secrets out of errors and probes reachability) so that the fork's
transport never sits between upstream and its own edits. Two properties are
deliberate: the cooldown state is per process, so a fresh run starts clean, and
the transport is unaware of what it downloads — parsing and vendor semantics
stay with the vendor module.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

from tradingagents.dataflows.errors import VendorUnavailableError

USER_AGENT = "Mozilla/5.0 (compatible; TradingAgents)"

# Space requests to one host, so a run does not build the burst that gets an IP
# blocked in the first place.
MIN_REQUEST_INTERVAL = 0.7
HOST_COOLDOWN_SECONDS = 600.0

_host_state_lock = threading.Lock()
_host_last_request_at: dict[str, float] = {}
_host_blocked_until: dict[str, float] = {}


try:  # pragma: no cover - the import depends on the environment
    from curl_cffi import requests as _curl_cffi
    from curl_cffi.requests.errors import RequestsError as _CurlError
except ImportError:  # pragma: no cover
    _curl_cffi = None
    _CurlError = None

_TRANSPORT_ERRORS = (requests.RequestException,) + ((_CurlError,) if _CurlError else ())

_SESSION: requests.Session | None = None


def session() -> requests.Session:
    """A shared plain session that ignores proxy environment variables."""
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.trust_env = False
        _SESSION.headers.update({"User-Agent": USER_AGENT})
    return _SESSION


def _fetch(url: str, params: dict | None, headers: dict, timeout: float, browser: bool) -> str:
    """One HTTP GET, browser-fingerprinted when asked and available."""
    if browser and _curl_cffi is not None:
        response = _curl_cffi.get(url, params=params, headers=headers,
                                  timeout=timeout, impersonate="chrome")
        if response.status_code >= 400:
            raise VendorUnavailableError(f"{url} returned HTTP {response.status_code}")
        return response.text
    response = session().get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    referer: str | None = None,
    timeout: float = 10.0,
    browser: bool = False,
) -> str:
    """GET ``url``, spacing requests and tripping a per-host breaker on failure.

    An HTTP error answer is not evidence about the host (the path may be wrong,
    the id stale), so it opens no breaker; only a failure of the connection
    itself — timeout, reset, empty reply — does, because retrying a blocked IP
    only extends the block.
    """
    headers = dict(headers or {})
    if referer:
        headers["Referer"] = referer
    host = urlparse(url).netloc
    now = time.monotonic()
    with _host_state_lock:
        blocked_until = _host_blocked_until.get(host, 0.0)
        if now < blocked_until:
            raise VendorUnavailableError(
                f"{host} recently dropped requests; skipping it for another "
                f"{blocked_until - now:.0f}s"
            )
        wait = MIN_REQUEST_INTERVAL - (now - _host_last_request_at.get(host, 0.0))
        _host_last_request_at[host] = now + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)

    last: Exception | None = None
    # One retry: the vendor intermittently drops a connection mid-run, and a
    # single re-request covers it without hiding a real outage.
    for attempt in range(2):
        try:
            body = _fetch(url, params, headers, timeout, browser)
        except _TRANSPORT_ERRORS + (VendorUnavailableError,) as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.3)
            continue
        with _host_state_lock:
            _host_blocked_until[host] = 0.0
        return body

    if last is not None and isinstance(last, _TRANSPORT_ERRORS):
        with _host_state_lock:
            _host_blocked_until[host] = time.monotonic() + HOST_COOLDOWN_SECONDS
    raise VendorUnavailableError(f"request failed for {url}: {last}") from last


def json_body(body: str, source: str) -> Any:
    """A vendor's JSON body, or a typed error when it is unparseable."""
    try:
        return json.loads(body)
    except ValueError as exc:
        raise VendorUnavailableError(f"{source} returned unparseable JSON") from exc
