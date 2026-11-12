"""Fixture witnesses for the time-matched baseline builder and bar fetcher.

Hand-built minute bars only: no vendor call, no network, no clock.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrameUnit

from sextant.data.baseline_bars import fetch_baseline
from sextant.ranking.baseline import (
    BaselineCoverage,
    BaselineTable,
    MinuteBarRow,
    build_baseline,
    dump_baseline,
    load_baseline,
    segment_start,
)

D = Decimal
NS = 1_000_000_000
ET = ZoneInfo("America/New_York")

AS_OF = date(2026, 10, 27)
DAY = date(2026, 10, 26)
SESSIONS = (date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26))


def ns(day: date, hour: int, minute: int = 0) -> int:
    moment = datetime.combine(day, time(hour, minute), tzinfo=ET)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * NS


KNOWN_AT = ns(date(2026, 10, 27), 12)


def bar(
    day: date,
    hour: int,
    minute: int,
    *,
    volume: int = 10,
    vwap: str = "2",
    trades: int = 1,
) -> MinuteBarRow:
    return MinuteBarRow("SPY", ns(day, hour, minute), volume, D(vwap), trades)


def pre(day: date, symbol: str = "SPY") -> BaselineCoverage:
    return BaselineCoverage(symbol, ns(day, 4), ns(day, 9, 30))


def rth(day: date, symbol: str = "SPY") -> BaselineCoverage:
    return BaselineCoverage(symbol, ns(day, 9, 30), ns(day, 16))


def post(day: date, symbol: str = "SPY") -> BaselineCoverage:
    return BaselineCoverage(symbol, ns(day, 16, 1), ns(day, 20))


def build(
    bars: list[MinuteBarRow],
    coverage: list[BaselineCoverage],
    *,
    as_of: date = AS_OF,
    known_at: int = KNOWN_AT,
    prior_sessions: int = 20,
) -> BaselineTable:
    return build_baseline(
        bars,
        coverage=coverage,
        as_of=as_of,
        known_at=known_at,
        provenance="fixture XNYS bars",
        feed="sip",
        prior_sessions=prior_sessions,
    )


def test_median_of_three_complete_sessions_at_each_minute_boundary() -> None:
    bars = [
        bar(SESSIONS[0], 4, 0, vwap="2", trades=1),
        bar(SESSIONS[1], 4, 0, vwap="4", trades=2),
        bar(SESSIONS[2], 4, 0, vwap="6", trades=3),
    ]
    table = build(bars, [pre(day) for day in SESSIONS])

    cumulative = table.cumulative("SPY", "PRE", 60)
    assert cumulative is not None
    assert cumulative.dollar_volume == D(40)
    assert cumulative.trade_count == D(2)
    assert cumulative.sessions == 3
    window = table.window("SPY", "PRE", 60, 60)
    assert window is not None and window.dollar_volume == D(40)
    assert table.latest_session == SESSIONS[-1]
    assert table.feed == "sip" and table.known_at == KNOWN_AT
    assert table.provenance == (
        "fixture XNYS bars; v*vw, n; explicit complete coverage; linear within minute; "
        "first POST minute omitted"
    )


def test_current_and_future_sessions_are_dropped_not_counted_as_zero() -> None:
    days = (*SESSIONS, AS_OF, date(2026, 10, 28))
    bars = [bar(day, 4, 0, volume=1000 if day >= AS_OF else 10) for day in days]
    table = build(bars, [pre(day) for day in days])

    assert table.latest_session == SESSIONS[-1]
    assert all(date.fromisoformat(sample.session) < AS_OF for sample in table.samples)
    cumulative = table.cumulative("SPY", "PRE", 60)
    assert cumulative is not None and cumulative.dollar_volume == D(20)


def test_bar_outside_declared_coverage_raises() -> None:
    with pytest.raises(ValueError, match="outside declared coverage"):
        build([bar(DAY, 10, 0)], [pre(DAY)])


def test_duplicate_vendor_bar_raises() -> None:
    with pytest.raises(ValueError, match="duplicate vendor minute bar"):
        build([bar(DAY, 4, 0), bar(DAY, 4, 0)], [pre(DAY)])


def test_prior_sessions_cap_keeps_the_most_recent_days() -> None:
    days = (date(2026, 10, 21), *SESSIONS)
    values = (1, 2, 3, 4)
    bars = [
        bar(day, 4, 0, vwap=str(value), trades=value)
        for day, value in zip(days, values, strict=True)
    ]
    table = build(bars, [pre(day) for day in days], prior_sessions=3)

    cumulative = table.cumulative("SPY", "PRE", 60)
    assert cumulative is not None
    assert cumulative.dollar_volume == D(30)
    assert cumulative.trade_count == D(3)
    assert cumulative.sessions == 3
    assert {sample.session for sample in table.samples} == {day.isoformat() for day in SESSIONS}


def test_post_first_minute_is_omitted() -> None:
    table = build(
        [
            bar(DAY, 16, 0, volume=1000, vwap="1"),
            bar(DAY, 16, 1, volume=10, vwap="2", trades=2),
        ],
        [post(DAY)],
    )

    assert table.cumulative("SPY", "POST", 0) is None
    opening = table.cumulative("SPY", "POST", 60)
    assert opening is not None and opening.dollar_volume == D(0)
    counted = table.cumulative("SPY", "POST", 120)
    assert counted is not None
    assert counted.dollar_volume == D(20)
    assert counted.trade_count == D(2)


def test_quiet_session_with_coverage_contributes_zero() -> None:
    active, quiet = date(2026, 10, 23), date(2026, 10, 26)
    table = build(
        [bar(active, 4, 0, vwap="2", trades=1)],
        [pre(active), pre(quiet)],
        prior_sessions=2,
    )

    cumulative = table.cumulative("SPY", "PRE", 60)
    assert cumulative is not None
    assert cumulative.dollar_volume == D(10)
    assert cumulative.trade_count == D("0.5")
    assert cumulative.sessions == 2


def test_coverage_not_known_at_the_supplied_time_raises() -> None:
    with pytest.raises(ValueError, match="not known"):
        build([], [pre(DAY)], known_at=ns(DAY, 9, 29))


def test_minute_bar_row_refuses_unaligned_and_negative_fields() -> None:
    with pytest.raises(ValueError, match="minute-aligned"):
        MinuteBarRow("SPY", ns(DAY, 4, 0) + 1, 10, D("2"), 1)
    with pytest.raises(ValueError, match="negative"):
        MinuteBarRow("SPY", ns(DAY, 4, 0), -1, D("2"), 1)
    with pytest.raises(ValueError, match="invalid bar vwap"):
        MinuteBarRow("SPY", ns(DAY, 4, 0), 10, D("-1"), 1)


def test_early_close_post_ends_before_the_closed_gap() -> None:
    early = date(2026, 11, 27)  # XNYS early close, 13:00 ET
    table = build(
        [bar(early, 13, 1, vwap="2", trades=2)],
        [BaselineCoverage("SPY", ns(early, 13, 1), ns(early, 17))],
        as_of=date(2026, 11, 30),
        known_at=ns(date(2026, 11, 30), 12),
    )

    counted = table.cumulative("SPY", "POST", 120)
    assert counted is not None and counted.dollar_volume == D(20)
    assert table.cumulative("SPY", "POST", 0) is None
    segment, _, _ = segment_start(ns(early, 17, 30))
    assert segment is None


def test_overnight_belongs_to_the_next_session_and_holidays_are_closed() -> None:
    sunday, monday = date(2026, 11, 8), date(2026, 11, 9)
    segment, session_date, start = segment_start(ns(sunday, 20, 0))
    assert segment is not None and segment.name == "OVERNIGHT"
    assert session_date == monday and start == ns(sunday, 20, 0)
    late, late_date, late_start = segment_start(ns(monday, 3, 0))
    assert late is not None and late.name == "OVERNIGHT"
    assert late_date == monday and late_start == ns(sunday, 20, 0)
    closed, _, _ = segment_start(ns(date(2026, 11, 26), 12, 0))  # Thanksgiving
    assert closed is None


FETCH_DAYS = (date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6))
FETCH_AS_OF = date(2026, 11, 9)
FETCH_KNOWN_AT = ns(FETCH_AS_OF, 12)


class FakeClient:
    def __init__(self, response: dict[str, list[dict[str, Any]]]) -> None:
        self.response = response
        self.requests: list[StockBarsRequest] = []

    def get_stock_bars(self, request: StockBarsRequest) -> dict[str, list[dict[str, Any]]]:
        self.requests.append(request)
        return self.response


def raw_bar(day: date, hour: int, minute: int) -> dict[str, Any]:
    return {
        "t": datetime.combine(day, time(hour, minute), tzinfo=ET).isoformat(),
        "v": 10,
        "vw": 2.0,
        "n": 1,
    }


def fetch(client: FakeClient) -> BaselineTable:
    return fetch_baseline(
        client,
        ("SPY",),
        as_of=FETCH_AS_OF,
        prior_sessions=3,
        known_at=FETCH_KNOWN_AT,
        feed="sip",
    )


def test_dump_and_load_roundtrip_preserves_the_table() -> None:
    table = build(
        [
            bar(SESSIONS[0], 4, 0, vwap="2", trades=1),
            bar(SESSIONS[1], 4, 0, vwap="4", trades=2),
            bar(SESSIONS[2], 4, 1, vwap="6", trades=3),
        ],
        [pre(day) for day in SESSIONS],
    )
    text = dump_baseline(table)
    payload = json.loads(text)

    assert set(payload) == {"samples", "provenance", "known_at", "feed"}
    restored = load_baseline(text)
    assert restored.samples == table.samples
    assert restored.provenance == table.provenance
    assert restored.known_at == table.known_at
    assert restored.feed == table.feed
    assert restored.identity == table.identity
    assert restored.latest_session == table.latest_session
    assert restored.window("SPY", "PRE", 120, 60) == table.window("SPY", "PRE", 120, 60)


def test_fetcher_builds_the_table_from_a_complete_raw_response() -> None:
    client = FakeClient({"SPY": [raw_bar(day, 4, 0) for day in FETCH_DAYS]})
    table = fetch(client)

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.symbol_or_symbols == ["SPY"]
    assert request.start == datetime.combine(FETCH_DAYS[0], time(4), tzinfo=ET).astimezone(
        UTC
    ).replace(tzinfo=None)
    assert request.end == (
        datetime.combine(FETCH_DAYS[-1], time(20), tzinfo=ET) - timedelta(microseconds=1)
    ).astimezone(UTC).replace(tzinfo=None)
    assert request.asof == "2026-11-09"
    assert request.feed is DataFeed.SIP
    assert request.adjustment is Adjustment.RAW
    assert request.sort is Sort.ASC
    assert request.timeframe.amount == 1
    assert request.timeframe.unit is TimeFrameUnit.Minute

    assert table.feed == "sip"
    assert table.known_at == FETCH_KNOWN_AT
    assert table.latest_session == FETCH_DAYS[-1]
    pre_value = table.cumulative("SPY", "PRE", 60)
    assert pre_value is not None
    assert pre_value.dollar_volume == D(20) and pre_value.sessions == 3
    # No RTH or POST bar in a complete response: no baseline for those segments.
    assert table.cumulative("SPY", "RTH", 60) is None
    assert not table.covers("SPY", "RTH") and not table.covers("SPY", "POST")
    assert "explicit complete coverage" in table.provenance


def test_fetcher_refuses_a_session_with_no_bars_for_any_symbol() -> None:
    response = {
        "SPY": [raw_bar(FETCH_DAYS[0], 4, 0), raw_bar(FETCH_DAYS[2], 4, 0)],
    }
    with pytest.raises(ValueError, match=r"missing requested session .* for every"):
        fetch(FakeClient(response))


def test_fetcher_refuses_a_bar_outside_the_requested_sessions() -> None:
    response = {"SPY": [raw_bar(day, 4, 0) for day in FETCH_DAYS]}
    response["SPY"].append(raw_bar(FETCH_DAYS[1], 3, 0))
    with pytest.raises(ValueError, match="outside the requested sessions"):
        fetch(FakeClient(response))


def test_fetcher_gives_no_baseline_for_a_complete_empty_response() -> None:
    table = fetch(FakeClient({}))

    assert table.samples == ()
    assert table.cumulative("SPY", "PRE", 60) is None


def fetch_two(response: dict[str, list[dict[str, Any]]]) -> BaselineTable:
    return fetch_baseline(
        FakeClient(response),
        ("SPY", "QQQ"),
        as_of=FETCH_AS_OF,
        prior_sessions=3,
        known_at=FETCH_KNOWN_AT,
        feed="sip",
    )


def test_fetcher_gives_no_baseline_to_a_symbol_absent_from_a_complete_response() -> None:
    table = fetch_two({"SPY": [raw_bar(day, 4, 0) for day in FETCH_DAYS]})

    assert table.covers("SPY", "PRE")
    assert all(not table.covers("QQQ", segment) for segment in ("PRE", "RTH", "POST"))
    assert (
        table.samples == fetch(FakeClient({"SPY": [raw_bar(d, 4, 0) for d in FETCH_DAYS]})).samples
    )


def test_fetcher_keeps_a_zero_for_a_traded_symbol_quiet_on_one_day() -> None:
    table = fetch_two(
        {
            "SPY": [raw_bar(day, 4, 0) for day in FETCH_DAYS],
            "QQQ": [raw_bar(FETCH_DAYS[0], 4, 0)],
        }
    )

    assert table.sessions("QQQ", "PRE") == tuple(day.isoformat() for day in FETCH_DAYS)
    value = table.cumulative("QQQ", "PRE", 60)
    assert value is not None and value.dollar_volume == D(0) and value.sessions == 3


def test_a_post_close_minute_alone_gives_no_post_baseline() -> None:
    response = {"SPY": [raw_bar(day, 4, 0) for day in FETCH_DAYS] + [raw_bar(FETCH_DAYS[0], 16, 0)]}

    assert not fetch(FakeClient(response)).covers("SPY", "POST")


def test_fetcher_still_refuses_bars_for_an_unrequested_symbol() -> None:
    response = {"QQQ": [raw_bar(day, 4, 0) for day in FETCH_DAYS]}
    with pytest.raises(ValueError, match="outside declared coverage"):
        fetch(FakeClient(response))
