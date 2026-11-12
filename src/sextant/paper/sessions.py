"""XNYS session slice from northstar-trading@8256c16c:sessions.py.

This describes the exchange schedule, never broker/instrument eligibility.
IB's narrower dated windows and maintenance are supplied by its account slice.
"""

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import cache
from zoneinfo import ZoneInfo

from exchange_calendars import get_calendar

ET = ZoneInfo("America/New_York")
SECOND = 1_000_000_000


class Session(StrEnum):
    OVERNIGHT = "overnight"
    PRE = "pre"
    RTH = "rth"
    POST = "post"
    CLOSED = "closed"


@cache
def _calendar(year: int):
    return get_calendar("XNYS", start=date(year - 1, 12, 1), end=date(year + 1, 1, 31), side="left")


def session_at(ts_ns: int) -> Session:
    """Half-open event-clock sessions, actual XNYS close, and 17:00 early post close."""
    if type(ts_ns) is not int or ts_ns < 0:
        raise ValueError("integer event nanoseconds required")
    local = datetime.fromtimestamp(ts_ns // SECOND, tz=ET)
    day = (local + timedelta(hours=4)).date()
    calendar = _calendar(day.year)
    if not calendar.is_session(day):
        return Session.CLOSED
    at = local.time()
    if at >= time(20) or at < time(4):
        return Session.OVERNIGHT
    if at < time(9, 30):
        return Session.PRE
    close = calendar.session_close(local.date()).tz_convert(ET).time()
    if at < close:
        return Session.RTH
    if close < time(16) and at >= time(17):
        return Session.CLOSED
    return Session.POST


def session_start_ns(ts_ns: int) -> int:
    """The event-clock start of the half-open session containing ``ts_ns``."""
    if type(ts_ns) is not int or ts_ns < 0:
        raise ValueError("integer event nanoseconds required")
    local = datetime.fromtimestamp(ts_ns // SECOND, tz=ET)
    day = (local + timedelta(hours=4)).date()
    calendar = _calendar(day.year)
    session = session_at(ts_ns)
    if session is Session.OVERNIGHT:
        # The overnight segment is 20:00 the prior ET day through 04:00.
        start = datetime.combine(day - timedelta(days=1), time(20), tzinfo=ET)
    elif session is Session.PRE:
        start = datetime.combine(day, time(4), tzinfo=ET)
    elif session is Session.RTH:
        start = datetime.combine(day, time(9, 30), tzinfo=ET)
    elif session is Session.POST:
        close = calendar.session_close(local.date()).tz_convert(ET)
        start = datetime.combine(local.date(), close.time(), tzinfo=ET)
    else:
        start = datetime.combine(local.date(), time(0), tzinfo=ET)
    return int(start.timestamp()) * SECOND
