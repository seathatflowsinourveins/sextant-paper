"""Fetch the prior sessions' minute bars and build a time-matched baseline.

The client is injected and must be an alpaca-py raw ``StockHistoricalDataClient``
(``raw_data=True``): its response is the flat ``{symbol: [bar, ...]}`` mapping
whose rows carry ``t``, ``v``, ``vw`` and ``n``. Nothing here reads the
environment, constructs a client or logs a payload.

Completeness of the answer is the SDK's: ``get_stock_bars`` returns only after
the page whose ``next_page_token`` is null and raises ``APIError`` on any failed
page (alpaca-py@v0.44.0 ``alpaca/common/rest.py:198-207,376-408``). Its merged
mapping is a ``defaultdict(list)`` filled only from symbols present in some
page, so a requested symbol with no bars is simply absent. Such a symbol, or
segment, did not trade in the window: it gets no coverage receipt and therefore
no baseline, and the reader treats it as unknown. A non-empty answer that lacks
a whole requested session for every symbol is refused as a dropped day. A
symbol that traded in some sessions keeps a zero for its quiet ones.
"""

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from sextant.data.alpaca_history import decimal_value, nanos, symbol_name, timestamp
from sextant.paper.sessions import ET
from sextant.ranking.baseline import (
    MINUTE_NS,
    BaselineCoverage,
    BaselineTable,
    MinuteBarRow,
    build_baseline,
    segment_start,
)

SEGMENTS = ("PRE", "RTH", "POST")


class BarsClient(Protocol):
    """The slice of ``StockHistoricalDataClient`` this module needs."""

    def get_stock_bars(self, request: StockBarsRequest) -> Any: ...


def _vwap(value: object) -> Decimal:
    if not isinstance(value, (int, float, Decimal, str)):
        raise ValueError("baseline bar vwap must be numeric")
    try:
        return decimal_value(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("baseline bar vwap must be numeric") from exc


def _minute_bar(symbol: str, row: object) -> MinuteBarRow:
    if not isinstance(row, Mapping):
        raise ValueError("baseline bar rows must be mappings")
    missing = [key for key in ("t", "v", "vw", "n") if key not in row]
    if missing:
        raise ValueError(f"baseline bar is missing {missing[0]}")
    volume, vwap, trade_count = row["v"], row["vw"], row["n"]
    if type(volume) is not int:
        raise ValueError("baseline bar volume must be an integer")
    if type(trade_count) is not int:
        raise ValueError("baseline bar trade count must be an integer")
    return MinuteBarRow(
        symbol,
        nanos(timestamp(row["t"])),
        volume,
        _vwap(vwap),
        trade_count,
    )


def _raw_rows(response: object) -> dict[str, list[MinuteBarRow]]:
    if not isinstance(response, Mapping):
        raise ValueError("baseline bar response must be a symbol mapping")
    rows: dict[str, list[MinuteBarRow]] = {}
    for symbol, values in response.items():
        if not isinstance(symbol, str) or not isinstance(values, list):
            raise ValueError("baseline bar response must map symbols to bar lists")
        name = symbol_name(symbol)
        rows[name] = [_minute_bar(name, value) for value in values]
    return rows


def prior_xnys_sessions(as_of: date, count: int) -> tuple[date, ...]:
    if count <= 0:
        raise ValueError("prior_sessions must be positive")
    sessions: list[date] = []
    day = as_of - timedelta(days=1)
    for _ in range(2 * count + 31):
        segment, session_date, _start = segment_start(
            nanos(datetime.combine(day, time(4), tzinfo=ET))
        )
        if segment is not None and segment.name == "PRE" and session_date == day:
            sessions.append(day)
            if len(sessions) == count:
                return tuple(reversed(sessions))
        day -= timedelta(days=1)
    raise ValueError(f"fewer than {count} prior XNYS sessions before {as_of}")


def _day_coverage(symbol: str, day: date) -> list[BaselineCoverage]:
    pre, pre_day, _pre_start = segment_start(nanos(datetime.combine(day, time(4), tzinfo=ET)))
    rth, rth_day, _rth_start = segment_start(nanos(datetime.combine(day, time(9, 30), tzinfo=ET)))
    if pre is None or rth is None or pre_day != day or rth_day != day:
        raise ValueError(f"{day.isoformat()} is not a complete XNYS session")
    post, post_day, _post_start = segment_start(rth.end_ns + MINUTE_NS)
    if post is None or post_day != day:
        raise ValueError(f"{day.isoformat()} has no usable post-close segment")
    return [
        BaselineCoverage(symbol, pre.start_ns, pre.end_ns),
        BaselineCoverage(symbol, rth.start_ns, rth.end_ns),
        BaselineCoverage(symbol, post.start_ns + MINUTE_NS, post.end_ns),
    ]


def _refuse_dropped_session(days: set[date], sessions: tuple[date, ...]) -> None:
    """A session with no bar for any requested symbol is a dropped day, not a quiet one."""
    missing = [day for day in sessions if day not in days]
    if missing:
        raise ValueError(
            f"vendor bars are missing requested session {missing[0].isoformat()} "
            "for every requested symbol"
        )


def _refuse_incomplete(
    rows: dict[str, list[MinuteBarRow]], sessions: tuple[date, ...]
) -> set[tuple[str, str]]:
    """Refuse a malformed answer; return the (symbol, segment) pairs with usable bars.

    The 16:00 minute is skipped as ``build_baseline`` skips it. An unrequested
    symbol's bars get no receipt, so ``build_baseline`` still refuses them.
    """
    days: set[date] = set()
    present: set[tuple[str, str]] = set()
    for symbol, bars in rows.items():
        for bar in bars:
            segment, day, start = segment_start(bar.ts_event)
            if segment is None or segment.name not in SEGMENTS or day not in sessions:
                raise ValueError(
                    f"vendor minute bar outside the requested sessions: {symbol} {bar.ts_event}"
                )
            days.add(day)
            if not (segment.name == "POST" and bar.ts_event == start):
                present.add((symbol, segment.name))
    if rows:
        _refuse_dropped_session(days, sessions)
    return present


def _overnight_session(ts_event: int, sessions: tuple[date, ...]) -> date | None:
    moment = datetime.fromtimestamp(ts_event / 1_000_000_000, tz=ET)
    for day in sessions:
        start = datetime.combine(day - timedelta(days=1), time(20), tzinfo=ET)
        end = datetime.combine(day, time(4), tzinfo=ET)
        if start <= moment < end:
            return day
    return None


def _refuse_overnight_incomplete(
    rows: dict[str, list[MinuteBarRow]], sessions: tuple[date, ...], symbols: tuple[str, ...]
) -> set[str]:
    """Refuse a malformed overnight answer; return the symbols with an overnight bar.

    Bars outside the overnight windows are left for ``build_baseline``'s
    coverage receipt to refuse.
    """
    requested = {symbol: rows[symbol] for symbol in symbols if symbol in rows}
    days: set[date] = set()
    present: set[str] = set()
    for symbol, bars in requested.items():
        for bar in bars:
            day = _overnight_session(bar.ts_event, sessions)
            if day is not None:
                days.add(day)
                present.add(symbol)
    if requested:
        _refuse_dropped_session(days, sessions)
    return present


def fetch_overnight_baseline(
    client: BarsClient,
    symbols: Iterable[str],
    *,
    as_of: date,
    known_at: int,
    feed: str = "boats",
    prior_sessions: int = 20,
) -> BaselineTable:
    """Request the prior overnight sessions' minute bars and build one baseline.

    Each trading day ``D`` owns the overnight window ``[D-1 20:00, D 04:00)`` ET.
    The bars come from the overnight feed (``boats`` on an Algo Trader Plus
    plan); the same flat ``{symbol: [bar, ...]}`` response shape is expected.
    """
    sessions = prior_xnys_sessions(as_of, prior_sessions)
    names = tuple(symbol_name(symbol) for symbol in symbols)
    if not names:
        raise ValueError("at least one symbol is required")
    feed_value = DataFeed(feed)
    if feed_value != DataFeed.BOATS:
        raise ValueError("the overnight baseline uses the boats feed only")
    start = datetime.combine(sessions[0] - timedelta(days=1), time(20), tzinfo=ET)
    end = datetime.combine(sessions[-1], time(4), tzinfo=ET) - timedelta(microseconds=1)
    request = StockBarsRequest(
        symbol_or_symbols=list(names),
        timeframe=TimeFrame(1, TimeFrameUnit("Min")),
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=feed_value,
        sort=Sort.ASC,
        asof=as_of.isoformat(),
    )
    response = client.get_stock_bars(request)
    rows = _raw_rows(response)
    present = _refuse_overnight_incomplete(rows, sessions, names)
    bars = [bar for symbol in names for bar in rows.get(symbol, ())]
    coverage = [
        BaselineCoverage(
            symbol,
            nanos(datetime.combine(day - timedelta(days=1), time(20), tzinfo=ET)),
            nanos(datetime.combine(day, time(4), tzinfo=ET)),
        )
        for symbol in names
        if symbol in present
        for day in sessions
    ]
    return build_baseline(
        bars,
        coverage=coverage,
        as_of=as_of,
        known_at=known_at,
        provenance=f"alpaca-py {feed_value.value} raw 1-minute bars; overnight segment",
        feed=feed_value.value,
        prior_sessions=prior_sessions,
    )


def fetch_baseline(
    client: BarsClient,
    symbols: Iterable[str],
    *,
    as_of: date,
    known_at: int,
    feed: str,
    prior_sessions: int = 20,
) -> BaselineTable:
    """Request the prior sessions' minute bars and build one baseline table.

    The request window is 04:00 ET on the first prior session through 19:59:59.999999
    ET on the last; ``limit`` is left unset so alpaca-py paginates to exhaustion.
    """
    sessions = prior_xnys_sessions(as_of, prior_sessions)
    names = tuple(symbol_name(symbol) for symbol in symbols)
    start = datetime.combine(sessions[0], time(4), tzinfo=ET)
    end = datetime.combine(sessions[-1], time(20), tzinfo=ET) - timedelta(microseconds=1)
    request = StockBarsRequest(
        symbol_or_symbols=list(names),
        timeframe=TimeFrame(1, TimeFrameUnit("Min")),
        start=start,
        end=end,
        adjustment=Adjustment.RAW,
        feed=DataFeed(feed),
        sort=Sort.ASC,
        asof=as_of.isoformat(),
    )
    rows = _raw_rows(client.get_stock_bars(request))
    present = _refuse_incomplete(rows, sessions)
    coverage = [
        receipt
        for symbol in names
        for day in sessions
        for name, receipt in zip(SEGMENTS, _day_coverage(symbol, day), strict=True)
        if (symbol, name) in present
    ]
    return build_baseline(
        [bar for bars in rows.values() for bar in bars],
        coverage=coverage,
        as_of=as_of,
        known_at=known_at,
        provenance=f"alpaca-py {feed} raw 1-minute bars",
        feed=feed,
        prior_sessions=prior_sessions,
    )
