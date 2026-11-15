"""What the market clock says about a historical run's present-day snapshot.

Eastmoney's fund quote and valuation endpoints serve only *now*: a live quote,
a live NAV and present-day constituent multiples, with no filing date. The
point-in-time rule withholds them for a run dated in the past. But a run dated
the last session before "now" would not have seen anything else: when no
exchange session has closed after the analysis date, the current snapshot is
exactly what was public then (a Saturday run for Friday's close, a holiday run
for the prior close, a pre-open run for yesterday's close).

This module decides that question from the market data itself rather than from a
hand-maintained holiday table: the newest closed daily bar is the newest session
whose close is public, and a bar for today counts only after the 15:00 China
close. When the bars cannot be read, it falls back to the weekday calendar and
says so, which can only withhold more, never release more.

The decision is recorded in the report, never inferred silently: a released
snapshot carries the date it describes and the reason it is equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tradingagents.dataflows.dates import parse_day

SHANGHAI = ZoneInfo("Asia/Shanghai")
# The A-share session opens at 09:30 and closes at 15:00 China time. A bar dated
# today exists (or the live quote moves) from the open, so the current snapshot
# is no longer what a run dated before today saw once the session is under way.
OPEN = time(9, 30)
CLOSE = time(15, 0)
# A frame whose newest bar is older than this cannot prove what was public
# around the analysis date; like the price vendors, treat it as stale.
MAX_STALE_DAYS = 10


@dataclass(frozen=True)
class SessionClock:
    """The newest closed session and its distance from the analysis date."""

    # Newest session whose close was public when the clock was read.
    last_closed: date | None
    # Sessions that closed strictly after the analysis date.
    sessions_after: int
    # Calendar days between the analysis date and the newest closed session.
    calendar_days_after: int
    # How the answer was read, recorded in the report.
    basis: str
    # Whether a session may be trading right now. A live quote then carries
    # intraday information newer than the last close, so it is not equivalent
    # to a run dated before today even though no session has closed yet.
    session_open: bool = False
    # The bars read are too old to prove what was public near the run date.
    stale: bool = False

    def equivalent_for(self, as_of_date: date) -> bool:
        """Whether the current snapshot is what a run dated ``as_of_date`` saw.

        True only when nothing can have traded after the analysis date: the
        analysis date is the newest closed session (or a non-trading day after
        it), no session is open, and the bars are not stale. An open session is
        newer information, not a snapshot of the last close; a lagging feed
        cannot prove the difference between a holiday and a missing bar.
        """
        return (
            self.last_closed is not None
            and not self.session_open
            and not self.stale
            and self.last_closed <= as_of_date
        )

    def gap_text(self, as_of_date: date) -> str:
        """How far the nearest available snapshot is from the analysis date."""
        if self.last_closed is None:
            return "the nearest available snapshot date could not be determined"
        if self.sessions_after:
            sessions = f"{self.sessions_after} trading session{'s' if self.sessions_after != 1 else ''}"
        else:
            sessions = "no closed trading sessions"
        days = f"{self.calendar_days_after} calendar day{'s' if self.calendar_days_after != 1 else ''}"
        return f"{sessions} and {days} after it"

    def release_note(self, as_of_date: date) -> str:
        """The banner a released snapshot carries, naming the date and the basis."""
        day = self.last_closed.isoformat() if self.last_closed else "unknown"
        return (
            f"as-of {as_of_date.isoformat()} (snapshot-equivalent release: the "
            f"newest closed session was {day}, so no session closed after the "
            f"analysis date; judged from {self.basis})"
        )


def _bars(symbol: str, start: date, end: date):
    """Daily bars for the symbol, or None when the price vendor cannot serve them."""
    try:
        from tradingagents.dataflows import prices
        from tradingagents.dataflows.vendors.cn_market import get_stock_data

        text = get_stock_data(symbol, start.isoformat(), end.isoformat())
        return prices.frame_from_text(text)
    except Exception:  # noqa: BLE001 - a clock fallback is always available
        return None


def _fallback_last_closed(now: datetime) -> date:
    """Newest weekday session whose 15:00 close has passed, ignoring holidays.

    Exchange holidays are not modelled here. A holiday therefore reads as a
    weekday whose session "closed", which can only make ``last_closed`` later
    than reality: a run is withheld when it might have been released, never the
    other way around.
    """
    day = now.date()
    if day.weekday() >= 5 or now.time() < CLOSE:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def session_clock(symbol: str, as_of_date: str, *, now: datetime | None = None) -> SessionClock:
    """Read the market clock for ``symbol`` around ``as_of_date``.

    ``now`` is injectable for tests; the default is the real China-time clock.
    """
    now = now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)
    as_of = parse_day(as_of_date)
    if as_of is None:
        return SessionClock(
            _fallback_last_closed(now), 0, 0, "the weekday calendar fallback"
        )

    # The bars must reach back past the analysis date and forward to today. A
    # few extra days cover the case where the analysis date is a non-trading
    # day; the newest bar is what matters, not the start.
    start = min(as_of - timedelta(days=10), now.date() - timedelta(days=30))
    frame = _bars(symbol, start, now.date())
    if frame is None or frame.empty:
        last = _fallback_last_closed(now)
        sessions = _weekday_sessions_between(as_of, last)
        # On a weekday after the open, a missing today bar could be a lag or a
        # holiday; without a calendar, withhold rather than release.
        opened = now.weekday() < 5 and now.time() >= OPEN
        return SessionClock(
            last, sessions, max((last - as_of).days, 0),
            "the weekday calendar fallback (exchange holidays not verified)",
            session_open=opened, stale=False,
        )

    frame = frame.copy()
    frame["Date"] = frame["Date"].dt.date
    sessions = sorted(set(frame["Date"]))
    # A bar dated today is intraday until the China close; it is not a closed
    # session yet, so the newest *closed* bar is the previous one. Once the
    # session has opened, today's bar also means the live quote carries newer
    # information than the last close, so the snapshot is not equivalent to a
    # run dated before today.
    # A weekday after the open may be trading right now, whether or not the
    # feed already has today's bar: a missing bar cannot prove a holiday, so it
    # must not be read as "no session after the analysis date".
    opened = now.weekday() < 5 and now.time() >= OPEN
    if sessions and sessions[-1] == now.date() and now.time() < CLOSE:
        sessions = sessions[:-1]
    if not sessions:
        last = _fallback_last_closed(now)
        return SessionClock(
            last, _weekday_sessions_between(as_of, last), max((last - as_of).days, 0),
            "the weekday calendar fallback (no usable bars)",
            session_open=opened, stale=False,
        )

    last = sessions[-1]
    stale = (now.date() - last).days > MAX_STALE_DAYS
    after = sum(1 for day in sessions if day > as_of)
    return SessionClock(last, after, max((last - as_of).days, 0),
                        "cn_market daily bars, with today counted only after the 15:00 China close",
                        session_open=opened, stale=stale)


def _weekday_sessions_between(start: date, end: date) -> int:
    """Weekday count in ``(start, end]``, used only by the documented fallback."""
    if end <= start:
        return 0
    return sum(1 for offset in range(1, (end - start).days + 1)
               if (start + timedelta(days=offset)).weekday() < 5)
