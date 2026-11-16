"""Snapshot-equivalence releases and the two unavailable reasons for Eastmoney.

A run dated the last session before "now" may serve the present-day fund
snapshot: nothing has closed since, so the snapshot is what was public then. A
run dated further back is still withheld, but the notice names
``STRUCTURALLY_UNAVAILABLE`` (the vendor has no historical vintage) or
``DATA_UNAVAILABLE`` (the nearest-snapshot read failed) and shows the nearest
snapshot with its gap, so the analyst can judge what to do.
"""

import json
from datetime import date, datetime

import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.snapshot_equivalence import SHANGHAI, SessionClock, session_clock
from tradingagents.dataflows.vendors import eastmoney

_TODAY = "2026-11-14"   # Saturday


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(date_window, "get_current_date", lambda: _TODAY)


def _fake_clock(last=date(2026, 11, 13), after=0, days=0):
    return SessionClock(last, after, days, "test bars")


def _quote_pages():
    return {
        eastmoney._QUOTE_URL: {"data": {
            "f43": 0.885, "f47": 1040351, "f48": 93694477.0, "f58": "人工智能ETF华夏",
            "f60": 0.910, "f84": 7203000000.0, "f116": 6374655000.0,
            "f169": -0.025, "f170": -2.75, "f168": 1.45,
        }},
        eastmoney._FUND_JS_URL.split("{code}")[0]: (
            'var Data_netWorthTrend = [{"x":1763020800000,"y":0.8853}];'
        ),
    }


def _fake_get(pages):
    def fake(url, *, params=None, referer=None, timeout=None):
        for fragment, body in pages.items():
            if fragment in url:
                return body if isinstance(body, str) else json.dumps(body)
        raise AssertionError(f"unexpected URL {url} {params}")
    return fake


@pytest.mark.unit
class TestSnapshotEquivalence:
    def test_a_weekend_run_for_fridays_close_serves_the_snapshot(self, monkeypatch):
        monkeypatch.setattr(eastmoney, "session_clock", lambda canonical, as_of: _fake_clock())
        monkeypatch.setattr(eastmoney, "_get", _fake_get(_quote_pages()))

        out = eastmoney.get_etf_profile("515070.SS", "2026-11-13")

        assert "STRUCTURALLY_UNAVAILABLE" not in out
        assert "snapshot-equivalent release" in out
        assert "as-of 2026-11-13" in out
        assert "newest closed session was 2026-11-13" in out
        assert "0.885" in out
        # The note is in the header, not buried at the end.
        assert "snapshot-equivalent release" in "\n".join(out.splitlines()[:3])

    def test_a_run_further_back_is_withheld_with_the_nearest_snapshot(self, monkeypatch):
        monkeypatch.setattr(
            eastmoney, "session_clock",
            lambda canonical, as_of: _fake_clock(after=3, days=3),
        )
        monkeypatch.setattr(eastmoney, "_get", _fake_get(_quote_pages()))

        out = eastmoney.get_etf_profile("515070.SS", "2026-11-10")

        assert "STRUCTURALLY_UNAVAILABLE" in out
        assert "its newest closed session is 2026-11-13" in out
        assert "3 trading sessions and 3 calendar days after it" in out
        assert "0.885" in out
        assert "Do not present any snapshot figure above as information available on 2026-11-10" in out

    def test_a_failed_nearest_snapshot_read_is_data_unavailable_not_structural(self, monkeypatch):
        monkeypatch.setattr(eastmoney, "session_clock", lambda canonical, as_of: _fake_clock(days=3))

        def refused(url, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(eastmoney, "_get", refused)

        out = eastmoney.get_etf_profile("515070.SS", "2026-11-10")

        assert "DATA_UNAVAILABLE" in out
        assert "STRUCTURALLY_UNAVAILABLE" not in out
        assert "failed request" in out
        assert "withheld" in out.lower()

    def test_withheld_valuation_shows_the_current_aggregate_and_gap(self, monkeypatch):
        holdings = """var apidata={ content:"<div class='box'>
<h4 class='t'>...2026年3季度股票投资明细...截止至：<font class='px12'>2026-09-30</font>...</h4>
<table class='w782 comm tzxq t2'><tbody>
<tr><td>1</td><td>600519</td><td>贵州茅台</td><td>3.09%</td></tr>
</tbody></table>
",rc:0};
"""

        def fake(url, *, params=None, referer=None, timeout=None):
            if eastmoney._HOLDINGS_URL in url:
                return holdings
            if params and params.get("secid") == "1.600519":
                return json.dumps({"data": {"f57": "600519", "f58": "贵州茅台",
                                            "f116": 1000.0, "f162": 10.0,
                                            "f167": 2.0, "f127": "白酒Ⅱ"}})
            raise AssertionError(f"unexpected {url} {params}")

        monkeypatch.setattr(
            eastmoney, "session_clock", lambda canonical, as_of: _fake_clock(after=3, days=3)
        )
        monkeypatch.setattr(eastmoney, "_get", fake)

        out = eastmoney.get_etf_index_fundamentals("515070.SS", "2026-11-10")

        assert "STRUCTURALLY_UNAVAILABLE" in out
        assert "its newest closed session is 2026-11-13" in out
        assert "Aggregate PE (TTM)" in out
        assert "10.00" in out


@pytest.mark.unit
class TestSessionClock:
    def _frame(self, days):
        import pandas as pd

        return pd.DataFrame({"Date": pd.to_datetime(days), "Close": [1.0] * len(days)})

    def test_a_bar_for_today_is_not_closed_before_the_close(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.snapshot_equivalence._bars",
            lambda symbol, start, end: self._frame(["2026-11-12", "2026-11-13", "2026-11-14"]),
        )
        # Saturday: no session can be open, so the last close is equivalent.
        clock = session_clock(
            "515070.SS", "2026-11-13",
            now=datetime(2026, 11, 14, 10, 0, tzinfo=SHANGHAI),
        )
        assert clock.last_closed == date(2026, 11, 13)
        assert clock.sessions_after == 0
        assert not clock.session_open
        assert clock.equivalent_for(date(2026, 11, 13))

    def test_a_pre_open_run_is_equivalent_but_an_open_session_is_not(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.snapshot_equivalence._bars",
            lambda symbol, start, end: self._frame(["2026-11-13", "2026-11-16"]),
        )
        pre_open = session_clock(
            "515070.SS", "2026-11-13",
            now=datetime(2026, 11, 16, 8, 30, tzinfo=SHANGHAI),
        )
        assert pre_open.last_closed == date(2026, 11, 13)
        assert not pre_open.session_open
        assert pre_open.equivalent_for(date(2026, 11, 13))

        # The same Monday at 10:00 has a live intraday quote; no close has
        # happened, but the snapshot is newer than Friday's, so it is withheld.
        open_session = session_clock(
            "515070.SS", "2026-11-13",
            now=datetime(2026, 11, 16, 10, 0, tzinfo=SHANGHAI),
        )
        assert open_session.last_closed == date(2026, 11, 13)
        assert open_session.session_open
        assert not open_session.equivalent_for(date(2026, 11, 13))

    def test_todays_bar_counts_after_the_close(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.snapshot_equivalence._bars",
            lambda symbol, start, end: self._frame(["2026-11-13", "2026-11-14"]),
        )
        clock = session_clock(
            "515070.SS", "2026-11-13",
            now=datetime(2026, 11, 14, 15, 30, tzinfo=SHANGHAI),
        )
        assert clock.last_closed == date(2026, 11, 14)
        assert clock.sessions_after == 1
        assert not clock.equivalent_for(date(2026, 11, 13))

    def test_weekday_fallback_when_the_bars_cannot_be_read(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.snapshot_equivalence._bars",
            lambda symbol, start, end: None,
        )
        clock = session_clock(
            "515070.SS", "2026-11-10",
            now=datetime(2026, 11, 14, 10, 0, tzinfo=SHANGHAI),
        )
        assert clock.last_closed == date(2026, 11, 13)
        assert "weekday calendar fallback" in clock.basis
        assert clock.sessions_after == 3
        assert not clock.equivalent_for(date(2026, 11, 10))


    def test_a_lagging_feed_does_not_release_an_open_session(self, monkeypatch):
        """A missing today bar could be a holiday or a lag; on a weekday after the
        open it must be read as a possibly-open session, not as a release."""
        monkeypatch.setattr(
            "tradingagents.dataflows.snapshot_equivalence._bars",
            lambda symbol, start, end: self._frame(["2026-11-13"]),
        )
        clock = session_clock(
            "515070.SS", "2026-11-13",
            now=datetime(2026, 11, 16, 10, 0, tzinfo=SHANGHAI),
        )
        assert clock.session_open
        assert not clock.equivalent_for(date(2026, 11, 13))

    def test_a_stale_frame_cannot_prove_equivalence(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.dataflows.snapshot_equivalence._bars",
            lambda symbol, start, end: self._frame(["2026-10-01"]),
        )
        clock = session_clock(
            "515070.SS", "2026-10-01",
            now=datetime(2026, 11, 14, 10, 0, tzinfo=SHANGHAI),
        )
        assert clock.stale
        assert not clock.equivalent_for(date(2026, 10, 1))


@pytest.mark.unit
def test_the_profile_uses_the_real_clock_and_the_daily_bars(monkeypatch):
    """The production clock path (session_clock -> cn_market bars) can release or
    withhold; the fake-clock tests above never exercised it."""
    import pandas as pd

    from tradingagents.dataflows import snapshot_equivalence

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 14, 10, 0, tzinfo=tz)

    # 2026-11-14 is a Saturday, so the real clock has no open session.
    monkeypatch.setattr(snapshot_equivalence, "datetime", _FixedNow)
    monkeypatch.setattr(
        "tradingagents.dataflows.snapshot_equivalence._bars",
        lambda symbol, start, end: pd.DataFrame({
            "Date": pd.to_datetime(["2026-11-11", "2026-11-12", "2026-11-13"]),
            "Close": [0.9, 0.902, 0.885],
        }),
    )
    monkeypatch.setattr(eastmoney, "_get", _fake_get(_quote_pages()))

    released = eastmoney.get_etf_profile("515070.SS", "2026-11-13")
    withheld = eastmoney.get_etf_profile("515070.SS", "2026-11-10")

    assert "snapshot-equivalent release" in released
    assert "0.885" in released
    assert "STRUCTURALLY_UNAVAILABLE" in withheld
    assert "its newest closed session is 2026-11-13" in withheld


@pytest.mark.unit
def test_one_snapshot_fetch_per_profile_call(monkeypatch):
    """The served block and the withheld notice render the same metrics object."""
    calls = []
    metrics = {
        "code": "515070", "name": "人工智能ETF华夏", "price": 0.885, "prev": 0.902,
        "change": -0.017, "pct": -1.88, "volume": 1040351, "amount": 93694477.0,
        "shares": 7203000000.0, "market_value": 6374655000.0, "turnover": 1.44,
        "nav": 0.8853, "nav_date": "2026-11-13",
    }
    monkeypatch.setattr(eastmoney, "_profile_metrics",
                        lambda code, secid: calls.append(1) or metrics)
    monkeypatch.setattr(eastmoney, "_point_in_time", lambda canonical, as_of: (False, ""))
    monkeypatch.setattr(eastmoney, "session_clock", lambda *a: _fake_clock(after=3, days=3))

    out = eastmoney.get_etf_profile("515070.SS", "2026-11-10")

    assert calls == [1]
    assert "0.885" in out and "72.03" in out


@pytest.mark.unit
def test_the_served_and_withheld_blocks_use_one_unit_style(monkeypatch):
    """Both branches localize through the same helper, so one report has one style."""
    import copy

    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows.config import set_config

    original = copy.deepcopy(config_module._config)
    try:
        set_config({"output_language": "中文"})
        metrics = {
            "code": "515070", "name": "人工智能ETF华夏", "price": 0.885, "prev": 0.902,
            "change": -0.017, "pct": -1.88, "volume": 1040351, "amount": 93694477.0,
            "shares": 7203000000.0, "market_value": 6374655000.0, "turnover": 1.44,
            "nav": 0.8853, "nav_date": "2026-11-13",
        }
        monkeypatch.setattr(eastmoney, "_profile_metrics",
                            lambda code, secid: metrics)
        monkeypatch.setattr(eastmoney, "_point_in_time", lambda canonical, as_of: (True, ""))
        served = eastmoney.get_etf_profile("515070.SS", "2026-11-13")

        monkeypatch.setattr(eastmoney, "_point_in_time", lambda canonical, as_of: (False, ""))
        monkeypatch.setattr(eastmoney, "session_clock", lambda *a: _fake_clock(after=3, days=3))
        withheld = eastmoney.get_etf_profile("515070.SS", "2026-11-10")

        for out in (served, withheld):
            assert "72.03 亿份" in out
            assert "63.75 亿元" in out
            assert "0.94 亿元" in out
            assert "100m shares" not in out
    finally:
        config_module._config = original
