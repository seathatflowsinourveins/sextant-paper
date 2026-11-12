"""Witnesses for the overnight baseline fetcher: boats bars over 20:00-04:00 ET.

Hand-built minute bars only: no vendor call, no network, no clock.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alpaca.data.enums import DataFeed

from sextant.data.baseline_bars import fetch_overnight_baseline

D = Decimal
NS = 1_000_000_000
ET = ZoneInfo("America/New_York")

AS_OF = date(2026, 11, 9)
SESSIONS = (date(2026, 11, 5), date(2026, 11, 6))


def ns(day: date, hour: int, minute: int = 0) -> int:
    moment = datetime.combine(day, time(hour, minute), tzinfo=ET)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * NS


KNOWN_AT = ns(date(2026, 11, 9), 12)


def iso(day: date, hour: int, minute: int = 0) -> str:
    return (
        datetime.combine(day, time(hour, minute), tzinfo=ET)
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


class FakeData:
    def __init__(self, rows: dict[str, list[dict[str, object]]]) -> None:
        self.rows = rows
        self.request: Any = None

    def get_stock_bars(self, request):
        self.request = request
        return self.rows


def overnight_rows() -> dict[str, list[dict[str, object]]]:
    rows: dict[str, list[dict[str, object]]] = {}
    for symbol in ("AAPL", "SPY"):
        bars: list[dict[str, object]] = []
        for day in SESSIONS:
            previous = day - timedelta(days=1)
            bars.extend(
                [
                    {"t": iso(previous, 20, 0), "v": 10, "vw": "2.5", "n": 2},
                    {"t": iso(previous, 20, 1), "v": 5, "vw": "2.0", "n": 1},
                    {"t": iso(day, 3, 59), "v": 7, "vw": "3.0", "n": 3},
                ]
            )
        rows[symbol] = bars
    return rows


def test_the_fetcher_requests_boats_over_the_prior_overnight_windows() -> None:
    client = FakeData(overnight_rows())
    table = fetch_overnight_baseline(
        client, ["AAPL", "SPY"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
    )
    request = client.request
    assert isinstance(request.feed, DataFeed)
    assert request.feed == DataFeed.BOATS
    assert request.start == datetime(2026, 11, 5, 1, 0)
    assert request.end == datetime(2026, 11, 6, 8, 59, 59, 999999)
    assert request.asof == AS_OF.isoformat()
    assert table.feed == "boats"
    assert table.provenance.count("v*vw, n") == 1


def test_the_table_holds_overnight_offsets_and_running_totals() -> None:
    table = fetch_overnight_baseline(
        FakeData(overnight_rows()), ["AAPL"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
    )
    samples = [s for s in table.samples if s.segment == "OVERNIGHT"]
    assert {s.offset_s for s in samples} == {0, 60, 120, 28740, 28800}
    totals = {s.offset_s: s.dollar_volume for s in samples}
    assert totals[0] == D("0")
    assert totals[60] == D("25")
    assert totals[120] == D("35")
    assert totals[28740] == D("35")
    assert totals[28800] == D("56")
    assert all(s.session in {"2026-11-05", "2026-11-06"} for s in samples)


def test_a_bar_outside_the_declared_window_is_refused() -> None:
    rows = overnight_rows()
    rows["AAPL"].append({"t": iso(date(2026, 11, 5), 12, 0), "v": 1, "vw": "1", "n": 1})
    with pytest.raises(ValueError, match="outside declared coverage"):
        fetch_overnight_baseline(
            FakeData(rows), ["AAPL"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
        )


def test_an_empty_response_gives_no_symbol_a_baseline() -> None:
    table = fetch_overnight_baseline(
        FakeData({}), ["AAPL"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
    )
    assert table.samples == ()
    assert not table.covers("AAPL", "OVERNIGHT")


def test_a_symbol_with_no_overnight_bars_in_a_complete_response_gets_no_baseline() -> None:
    rows = overnight_rows()
    del rows["SPY"]
    table = fetch_overnight_baseline(
        FakeData(rows), ["AAPL", "SPY"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
    )
    alone = fetch_overnight_baseline(
        FakeData(overnight_rows()), ["AAPL"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
    )
    assert not table.covers("SPY", "OVERNIGHT")
    assert table.cumulative("SPY", "OVERNIGHT", 60) is None
    assert table.samples == alone.samples


def test_a_symbol_quiet_in_one_overnight_session_keeps_a_zero_for_that_session() -> None:
    rows = overnight_rows()
    quiet = {iso(date(2026, 11, 5), 20, 0), iso(date(2026, 11, 5), 20, 1), iso(SESSIONS[1], 3, 59)}
    rows["AAPL"] = [bar for bar in rows["AAPL"] if str(bar["t"]) not in quiet]
    table = fetch_overnight_baseline(
        FakeData(rows), ["AAPL", "SPY"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
    )
    assert table.sessions("AAPL", "OVERNIGHT") == ("2026-11-05", "2026-11-06")
    last = [s for s in table.samples if s.symbol == "AAPL" and s.session == "2026-11-06"]
    assert {s.dollar_volume for s in last} == {D("0")}


def test_a_session_with_no_overnight_bars_for_any_symbol_is_refused() -> None:
    rows = overnight_rows()
    missing = {
        iso(date(2026, 11, 5), 20, 0),
        iso(date(2026, 11, 5), 20, 1),
        iso(date(2026, 11, 6), 3, 59),
    }
    for symbol in rows:
        rows[symbol] = [bar for bar in rows[symbol] if str(bar["t"]) not in missing]
    with pytest.raises(ValueError, match="missing requested session 2026-11-06 for every"):
        fetch_overnight_baseline(
            FakeData(rows), ["AAPL", "SPY"], as_of=AS_OF, known_at=KNOWN_AT, prior_sessions=2
        )


def test_the_overnight_fetcher_refuses_a_non_boats_feed() -> None:
    with pytest.raises(ValueError, match="boats feed only"):
        fetch_overnight_baseline(
            FakeData(overnight_rows()),
            ["AAPL"],
            as_of=AS_OF,
            known_at=KNOWN_AT,
            feed="sip",
            prior_sessions=2,
        )
