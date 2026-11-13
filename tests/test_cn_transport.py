"""CN transport: request spacing, per-host circuit breaker, short timeouts.

The transport is shared by Eastmoney, Tencent, Sina and Tushare; the host that
punishes a burst is Eastmoney's, and the breaker is keyed by host, so these tests
use its URL.
"""

import time
import unittest
from unittest import mock

import pytest
import requests

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import eastmoney, transport

URL = "https://push2his.eastmoney.com/api/qt/stock/get?secid=1.515070"
HOST = "push2his.eastmoney.com"


@pytest.fixture(autouse=True)
def _clear_state():
    transport._host_blocked_until.clear()
    transport._host_last_request_at.clear()
    yield
    transport._host_blocked_until.clear()
    transport._host_last_request_at.clear()


def _raises(exc):
    def impl(*a, **k):
        raise exc
    return impl


@pytest.mark.unit
class TestCnTransport(unittest.TestCase):
    def test_timeout_is_short(self):
        # A blocked edge would otherwise stall an analyst for ten seconds per call.
        assert eastmoney._TIMEOUT <= 5.0
        assert transport.MIN_REQUEST_INTERVAL > 0

    def test_transport_failure_opens_the_breaker(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise requests.ConnectionError("connection reset")

        with mock.patch.object(transport, "_fetch", boom), \
                mock.patch.object(transport.time, "sleep"), pytest.raises(VendorUnavailableError):
            transport.get(URL, timeout=5.0, browser=True)
        assert len(calls) == 2  # the one retry before giving up
        assert transport._host_blocked_until[HOST] > time.monotonic()

        # While open, the next call is refused without touching the network.
        with mock.patch.object(transport, "_fetch", lambda *a, **k: "body"), \
                mock.patch.object(transport.time, "sleep"), \
                pytest.raises(VendorUnavailableError, match="skipping"):
            transport.get(URL, timeout=5.0, browser=True)

    def test_http_error_answer_does_not_open_the_breaker(self):
        with mock.patch.object(
            transport, "_fetch", _raises(VendorUnavailableError("HTTP 500"))
        ), mock.patch.object(transport.time, "sleep"), pytest.raises(VendorUnavailableError):
            transport.get(URL, timeout=5.0, browser=True)
        assert transport._host_blocked_until.get(HOST, 0.0) == 0.0

    def test_success_clears_the_breaker(self):
        transport._host_blocked_until[HOST] = time.monotonic() - 1  # cooldown elapsed
        with mock.patch.object(transport, "_fetch", lambda *a, **k: "body"), \
                mock.patch.object(transport.time, "sleep"):
            assert transport.get(URL, timeout=5.0) == "body"
        assert transport._host_blocked_until[HOST] == 0.0

    def test_requests_to_one_host_are_spaced(self):
        slept = []
        with mock.patch.object(transport.time, "sleep", lambda seconds: slept.append(seconds)), \
                mock.patch.object(transport, "_fetch", lambda *a, **k: "body"):
            transport._host_last_request_at[HOST] = time.monotonic()
            transport.get(URL, timeout=5.0)
        assert slept and slept[0] > 0
