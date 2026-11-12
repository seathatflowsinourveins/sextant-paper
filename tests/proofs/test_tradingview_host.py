"""Bar-fidelity host witnesses: bars, universe, baseline, anchors and rank parity."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.common import MessageBus
from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TraderId,
    TradeTick,
)
from requests import HTTPError

from sextant.ranking.baseline import BaselineSample, BaselineTable
from sextant.ranking.engine import (
    MoverRow,
    MoverState,
    ReferencePrices,
    Segment,
    advance_mover,
    rank_movers,
)
from sextant.ranking.generation import Coverage, CoverageState
from sextant.ranking.publisher import AssetState, RankingPublisher
from sextant.ranking.registry import Registry, RegistryRow
from sextant.ranking.window import TradeWindow
from sextant.tradingview.bars import (
    AlpacaBarSource,
    Bar,
    BarPoll,
    build_universe,
    parse_directory,
)
from sextant.tradingview.baseline import build_baseline
from sextant.tradingview.host import BarFidelityHost, load_symbol_map
from sextant.tradingview.producer import Clocks, reserve_stream

D = Decimal
NS = 1_000_000_000
NY = ZoneInfo("America/New_York")
DAY_START = int(datetime(2026, 11, 9, 4, 0, tzinfo=NY).timestamp()) * NS
SCOPE = Segment(
    "PRE", datetime(2026, 11, 9).date(), DAY_START, DAY_START + 19_800 * NS, "fixture XNYS boundary"
)


def registry() -> Registry:
    values = {
        "mover_min_move": (D("0.03"), "fraction"),
        "mover_min_trades_accel": (D("1.5"), "ratio"),
        "mover_min_dvol_ratio": (D("3"), "ratio"),
        "move_1m_window_s": (60, "seconds"),
        "move_5m_window_s": (300, "seconds"),
        "trades_accel_window_s": (60, "seconds"),
        # Fixture-only: one prior minute keeps the witness inside two polls.
        "trades_accel_baseline_min": (1, "minutes"),
        "publish_min_interval_s": (1, "seconds"),
        "generation_max_age_s": (2, "seconds"),
        "heartbeat_max_age_s": (2, "seconds"),
        "input_max_age_s": (2, "seconds"),
        "top_n": (2, "symbols"),
    }
    return Registry(
        tuple(
            RegistryRow(
                name, unit, value, "fixture only; ADR-0003", "UNCALIBRATED", SCOPE.session_date
            )
            for name, (value, unit) in values.items()
        )
    )


def baseline() -> BaselineTable:
    return BaselineTable(
        tuple(
            BaselineSample(name, "PRE", "2026-11-06", offset, D(dollars), D(0))
            for name in ("AAA", "BBB", "CCC")
            for offset, dollars in ((0, 0), (19_800, 3960))
        ),
        "fixture explicit complete curves; linear within minute",
        DAY_START - NS,
        "sip",
    )


def publisher() -> RankingPublisher:
    bus = MessageBus(TraderId.from_str("HOST-001"))
    pub = RankingPublisher(
        bus=bus,
        epoch="host-fixture-1",
        feed="sip",
        segment=SCOPE,
        registry=registry(),
        universe=("AAA", "BBB", "CCC"),
        universe_source="fixture selected symbols; NOT PIT universe",
        baseline=baseline(),
    )
    pub.set_coverage(Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0))
    for name in pub.universe:
        pub.set_symbol(
            name,
            references=ReferencePrices(D("10.00"), DAY_START - NS, "fixture official close"),
            asset=AssetState(DAY_START - NS, SCOPE.end_ns, False, "common", "fixture vendor"),
        )
    return pub


def bar(
    symbol: str, minute: int, close: str, volume: int, trades: int, *, open_: str = "10.00"
) -> Bar:
    return Bar(
        symbol=symbol,
        start_ns=DAY_START + minute * 60 * NS,
        open=D(open_),
        close=D(close),
        volume=volume,
        trades=trades,
        vw=None,
    )


def session_start(day: date, hour: int = 4) -> int:
    """The segment's own wall-clock start on a prior session's date."""

    return int(datetime.combine(day, time(hour, 0), tzinfo=NY).timestamp()) * NS


def session_bar(symbol: str, day: date, minute: int, close: str, volume: int, trades: int) -> Bar:
    start = session_start(day) + minute * 60 * NS
    return Bar(
        symbol=symbol,
        start_ns=start,
        open=D(close),
        close=D(close),
        volume=volume,
        trades=trades,
    )


def _bar_payload(close: float = 10.25) -> dict:
    return {
        "t": "2026-11-09T09:00:00Z",
        "o": 10.0,
        "h": 10.5,
        "l": 9.9,
        "c": close,
        "v": 1200,
        "n": 7,
        "vw": 10.1,
    }


class _Response:
    """Minimal requests response, so the real SDK request path can run offline."""

    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)

    def json(self) -> dict:
        assert self._payload is not None
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"synthetic {self.status_code}")


def duck_tick(symbol: str, price: Decimal, size: Decimal, at: int) -> TradeTick:
    """The e2e adapter's tick shape: no nautilus Quantity constraint."""

    tick = SimpleNamespace(
        instrument_id=InstrumentId.from_str(symbol + ".NASDAQ"),
        price=SimpleNamespace(as_decimal=lambda: price),
        size=SimpleNamespace(as_decimal=lambda: size),
        ts_event=at,
        ts_init=at,
        quote=None,
    )
    return cast(TradeTick, tick)


def direct_ranks(bars: list[Bar]) -> dict[int, list[tuple[str, int]]]:
    """The e2e bar-fidelity replay: zero-size anchor, one close tick per bar."""
    references = ReferencePrices(D("10.00"), DAY_START - NS, "fixture official close")
    states: dict[str, MoverState] = {}
    by_minute: dict[int, list[Bar]] = {}
    for item in bars:
        by_minute.setdefault((item.start_ns - DAY_START) // (60 * NS), []).append(item)
    out: dict[int, list[tuple[str, int]]] = {}
    for minute in sorted(by_minute):
        rows: list[MoverRow] = []
        for item in by_minute[minute]:
            if item.symbol not in states:
                anchor_price = item.open if item.start_ns == SCOPE.start_ns else D("10.00")
                state, _ = advance_mover(
                    _empty_state(),
                    duck_tick(item.symbol, anchor_price, D(0), SCOPE.start_ns),
                    references,
                    baseline(),
                    segment=SCOPE,
                    feed="sip",
                    registry=registry(),
                )
                states[item.symbol] = state
            tick = duck_tick(item.symbol, item.close, D(item.volume), item.close_ns)
            states[item.symbol], row = advance_mover(
                states[item.symbol],
                tick,
                references,
                baseline(),
                segment=SCOPE,
                feed="sip",
                registry=registry(),
            )
            if row is not None:
                rows.append(row)
        ranked = rank_movers(row for row in rows if row is not None)
        out[minute] = [(row.symbol, row.rank) for row in ranked if row.rank is not None]
    return out


def _empty_state() -> MoverState:
    return MoverState()


def test_counted_twins_match_independent_appends() -> None:
    for count in (1, 4, 50):
        volume = 10 + count
        sizes = [1] * (count - 1) + [volume - (count - 1)]
        partition = tuple(
            TradeTick(
                InstrumentId.from_str("AAA.NASDAQ"),
                Price.from_str("1.2500"),
                Quantity.from_int(size),
                AggressorSide.BUY,
                TradeId(str(index)),
                DAY_START,
                DAY_START,
            )
            for index, size in enumerate(sizes)
        )
        real = TradeTick(
            InstrumentId.from_str("AAA.NASDAQ"),
            Price.from_str("1.2500"),
            Quantity.from_int(volume),
            AggressorSide.BUY,
            TradeId("real"),
            DAY_START,
            DAY_START,
        )
        independent = TradeWindow()
        for item in partition:
            independent = independent.append(item, 400)
        seeded = TradeWindow().append_count(real, count - 1, 400).append(real, 400)
        assert len(seeded.buckets) == len(independent.buckets) == 1
        assert seeded.buckets[0].dollar_volume == independent.buckets[0].dollar_volume
        assert seeded.totals(DAY_START - 60 * NS, DAY_START) == (
            D("1.2500") * volume,
            count,
        )
        assert seeded.totals(DAY_START - 60 * NS, DAY_START) == independent.totals(
            DAY_START - 60 * NS, DAY_START
        )
        seeded_price = seeded.price_at(DAY_START)
        independent_price = independent.price_at(DAY_START)
        assert seeded_price is not None and independent_price is not None
        assert seeded_price.price.as_decimal() == independent_price.price.as_decimal()
        with pytest.raises(ValueError, match="out-of-order"):
            seeded.append(
                TradeTick(
                    InstrumentId.from_str("AAA.NASDAQ"),
                    Price.from_str("1.0000"),
                    Quantity.from_int(1),
                    AggressorSide.BUY,
                    TradeId("early"),
                    DAY_START - NS,
                    DAY_START - NS,
                ),
                400,
            )


def test_directory_parser_keeps_operating_companies_only() -> None:
    nasdaq = "\n".join(
        (
            "File Creation Time: 1109202604:00",
            "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
            "Round Lot Size|ETF|NextShares",
            "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
            "ZTEST|Z Test Corp - Common Stock|Q|Y|N|100|N|N",
            "SPY|SPDR S&P 500 ETF Trust|P|N|N|100|Y|N",
            "ABC$|ABC Corp - Depositary Shares|Q|N|N|100|N|N",
            "WARRANTX|WarrantX Corp - Warrant|Q|N|N|100|N|N",
            "BRK.B|Berkshire Hathaway Inc. - Class B Common Stock|Q|N|N|100|N|N",
        )
    )
    other = "\n".join(
        (
            "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
            "Test Issue|NASDAQ Symbol",
            "IBM|International Business Machines Corporation|N|IBM|N|100|N|IBM",
            "UNT|Unit Corp - Units|N|UNT|N|100|N|UNT",
            "File Creation Time: 1109202604:00",
        )
    )
    assert [item.symbol for item in parse_directory(nasdaq)] == [
        "AAPL",
        "ZTEST",
        "SPY",
        "ABC$",
        "WARRANTX",
        "BRK.B",
    ]
    assert build_universe(nasdaq, other) == ("AAPL", "BRK.B", "IBM")
    assert build_universe(nasdaq, other, active={"AAPL", "IBM"}) == ("AAPL", "IBM")


def test_alpaca_bars_batch_and_decimal_fields() -> None:
    from sextant.tradingview.bars import PacedStockBars

    calls: list[dict] = []
    waits: list[int] = []

    class Pacer:
        def wait(self) -> None:
            waits.append(len(waits))

    client = PacedStockBars(
        pacer=Pacer(),
        api_key="synthetic",
        secret_key="synthetic",  # noqa: S106
    )

    def request(method: str, url: str, **opts: object) -> _Response:
        params = cast(dict[str, str], opts["params"])
        calls.append(params)
        return _Response(
            200, {"bars": {name: [_bar_payload()] for name in params["symbols"].split(",")}}
        )

    source = AlpacaBarSource(client, batch=2)
    with patch.object(client._session, "request", request):
        bars = source.minute_bars(
            ("AAA", "BBB", "CCC"),
            start=datetime(2026, 11, 9, 4, 0, tzinfo=NY),
            end=datetime(2026, 11, 9, 4, 5, tzinfo=NY),
        )
    assert len(calls) == 2
    assert len(waits) == 2  # one paced wire request per batch, never an unpaced call
    assert [item.symbol for item in bars] == ["AAA", "BBB", "CCC"]
    first = bars[0]
    assert first.start_ns == int(datetime(2026, 11, 9, 9, 0, tzinfo=UTC).timestamp()) * NS
    assert first.close == D("10.25")
    assert first.volume == 1200
    assert first.trades == 7
    assert first.vw == D("10.1")
    assert (
        source.minute_bars(
            (),
            start=datetime(2026, 11, 9, 4, 0, tzinfo=NY),
            end=datetime(2026, 11, 9, 4, 5, tzinfo=NY),
        )
        == ()
    )
    with pytest.raises(ValueError):
        AlpacaBarSource(client, batch=0)


def test_alpaca_transport_paces_every_page_and_retry() -> None:
    from sextant.tradingview.bars import PacedStockBars

    calls: list[dict] = []
    waits: list[int] = []

    class Pacer:
        def wait(self) -> None:
            waits.append(len(waits))

    client = PacedStockBars(
        pacer=Pacer(),
        api_key="synthetic",
        secret_key="synthetic",  # noqa: S106
    )
    client._retry_wait = 0
    pages = iter(
        (
            _Response(429),
            _Response(200, {"bars": {"AAA": [_bar_payload()]}, "next_page_token": "page-2"}),
            _Response(200, {"bars": {"AAA": [_bar_payload(close=10.5)]}}),
        )
    )

    def request(method: str, url: str, **opts: object) -> _Response:
        calls.append(cast(dict, opts["params"]))
        return next(pages)

    source = AlpacaBarSource(client)
    with patch.object(client._session, "request", request):
        bars = source.minute_bars(
            ("AAA",),
            start=datetime(2026, 11, 9, 9, 0, tzinfo=NY),
            end=datetime(2026, 11, 9, 9, 5, tzinfo=NY),
        )
    assert [item.close for item in bars] == [D("10.25"), D("10.5")]
    assert len(calls) == 3  # one 429 retry plus two page_token pages
    assert len(waits) == len(calls)  # every wire request waited on the shared pacer


def test_five_session_baseline_carries_missing_minutes_and_medians() -> None:
    from datetime import date

    sessions = tuple(
        (
            day,
            (
                session_bar("AAA", day, 0, "10.00", volume, 1),
                session_bar("AAA", day, 2, "10.00", 100, 1),
            ),
        )
        for day, volume in (
            (date(2026, 11, 2), 100),
            (date(2026, 11, 3), 300),
            (date(2026, 11, 4), 200),
        )
    )
    table = build_baseline(
        sessions,
        symbols=("AAA", "BBB"),
        segment=SCOPE,
        known_at=DAY_START - 20 * 3600 * NS,
        provenance="fixture five-session curves",
    )
    assert table.feed == "sip"
    assert table.latest_session == date(2026, 11, 4)
    assert table.known_at == DAY_START - 20 * 3600 * NS

    def dollars(symbol: str, offset: str) -> Decimal:
        value = table.cumulative(symbol, "PRE", D(offset))
        assert value is not None
        return value.dollar_volume

    # Cumulative through minute 0 at offset 60 s, then minute 2 at offset 180 s.
    assert dollars("AAA", "60") == D(2000)
    assert dollars("AAA", "120") == D(2000)  # carried forward
    assert dollars("AAA", "180") == D(3000)
    assert dollars("AAA", "150") == D(2500)  # linear inside the gap
    assert dollars("BBB", "60") == D(0)
    assert table.cumulative("AAA", "PRE", D(-1)) is None
    assert table.cumulative("AAA", "PRE", D(19_800)) is not None
    assert table.cumulative("AAA", "PRE", D(19_801)) is None
    assert table.cumulative("ZZZ", "PRE", D(60)) is None
    assert table.identity


def test_baseline_anchors_each_session_to_its_own_start() -> None:
    sessions = tuple(
        (day, (session_bar("AAA", day, 0, "10.00", 100, 1),))
        for day in (
            date(2026, 11, 2),
            date(2026, 11, 3),
            date(2026, 11, 4),
            date(2026, 11, 5),
            date(2026, 11, 6),
        )
    )
    table = build_baseline(
        sessions,
        symbols=("AAA",),
        segment=SCOPE,
        known_at=DAY_START - 24 * 3600 * NS,
        provenance="fixture five prior sessions",
    )
    value = table.cumulative("AAA", "PRE", D(60))
    assert value is not None
    assert value.dollar_volume == D(1000)  # each 04:00 ET bar: close 10 x volume 100
    assert value.trade_count == D(1)
    carried = table.cumulative("AAA", "PRE", D(180))
    assert carried is not None and carried.dollar_volume == D(1000)


def test_host_ranks_match_direct_bar_fidelity_replay(tmp_path: Path) -> None:
    polls = []
    for minute, row in enumerate((("11.00", "12.00", "10.50"), ("11.50", "11.00", "10.60"))):
        polls.append(
            BarPoll(
                bars=(
                    bar("AAA", minute, row[0], 10, 3),
                    bar("BBB", minute, row[1], 20, 4),
                    bar("CCC", minute, row[2], 10, 2),
                ),
                requested=("AAA", "BBB", "CCC"),
            )
        )
    expected = direct_ranks([item for poll in polls for item in poll.bars])
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        # Minute 0 only opens the segment prefix: the prior trade window is still empty.
        close_ns = DAY_START + 60 * NS
        assert host.ingest(polls[0], market_ns=close_ns, received_ns=close_ns) == 3
        assert host.heartbeat(at=close_ns, received_ns=close_ns)
        assert expected[0] == []
        close_ns = DAY_START + 120 * NS
        assert host.ingest(polls[1], market_ns=close_ns, received_ns=close_ns) == 3
        assert host.heartbeat(at=close_ns, received_ns=close_ns)
        unit = host.publish(
            tmp_path / "unit.json",
            now=close_ns,
            mapping_clocks=Clocks(close_ns, close_ns, "fixture receipt"),
        )
        assert unit["status"] == "fresh", unit
        generation = host.producer.reader.generation
        assert generation is not None
        published = sorted(
            (row.symbol, row.rank) for row in generation.rows if row.rank is not None
        )
        assert published == sorted(expected[1])
        assert unit["watchlists"] == {"NS Ranked": ["NASDAQ:BBB", "NASDAQ:AAA"]}
        assert unit["paper_slot"] == 2 and unit["display_only"] is True


def test_host_refuses_lookahead_and_skips_stale_bars(tmp_path: Path) -> None:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = DAY_START + 60 * NS
        first = BarPoll(bars=(bar("AAA", 0, "11.00", 10, 3),), requested=("AAA",))
        host.ingest(first, market_ns=close_ns, received_ns=close_ns)
        state = host.publisher.states["AAA"]
        with pytest.raises(ValueError, match="market clock"):
            host.ingest(
                BarPoll(bars=(bar("AAA", 1, "11.50", 10, 3),), requested=("AAA",)),
                market_ns=close_ns,
                received_ns=close_ns,
            )
        assert host.ingest(first, market_ns=close_ns, received_ns=close_ns) == 0
        assert host.publisher.states["AAA"] == state
        with pytest.raises(ValueError, match="outside the universe"):
            host.ingest(
                BarPoll(bars=(bar("ZZZ", 0, "11.00", 10, 3),), requested=("ZZZ",)),
                market_ns=close_ns,
                received_ns=close_ns,
            )


def test_bar_closing_at_the_segment_end_is_skipped(tmp_path: Path) -> None:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        boundary = bar("AAA", 329, "11.00", 10, 3)
        assert boundary.close_ns == SCOPE.end_ns
        poll = BarPoll(bars=(boundary,), requested=("AAA", "BBB", "CCC"))
        assert host.ingest(poll, market_ns=SCOPE.end_ns, received_ns=SCOPE.end_ns) == 0
        assert host.producer.error is None
        assert host.publisher.states["AAA"].latest is None


def prime_two_minutes(host: BarFidelityHost) -> int:
    """Poll minutes 0 and 1 and heartbeat; return the second close clock."""
    for minute, row in (
        (0, ("11.00", "12.00", "10.50")),
        (1, ("11.50", "11.00", "10.60")),
    ):
        close_ns = DAY_START + (minute + 1) * 60 * NS
        host.ingest(
            BarPoll(
                bars=(
                    bar("AAA", minute, row[0], 10, 3),
                    bar("BBB", minute, row[1], 20, 4),
                    bar("CCC", minute, row[2], 10, 2),
                ),
                requested=("AAA", "BBB", "CCC"),
            ),
            market_ns=close_ns,
            received_ns=close_ns,
        )
        assert host.heartbeat(at=close_ns, received_ns=close_ns)
    return DAY_START + 120 * NS


def test_publish_uses_the_heartbeat_receipt_clock(tmp_path: Path) -> None:
    """A receive clock after the market clock still publishes at the receipt."""
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = prime_two_minutes(host)
        ingested, unit = host.poll(
            BarPoll(bars=(), requested=("AAA", "BBB", "CCC")),
            market_ns=close_ns,
            received_ns=close_ns + 1,
            path=tmp_path / "unit.json",
        )
        assert ingested == 0
        assert unit["status"] == "fresh", unit


def test_history_gap_stays_degraded_until_backfilled(tmp_path: Path) -> None:
    """A failed minute is never skipped silently: the prefix waits for its bar."""
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = prime_two_minutes(host)
        assert host.publisher.states["CCC"].segment_dollar_volume == D("211")
        minute_two = close_ns + 60 * NS
        assert (
            host.ingest(
                BarPoll(bars=(), requested=("AAA", "BBB", "CCC"), failed=("CCC",)),
                market_ns=minute_two,
                received_ns=minute_two,
            )
            == 0
        )
        minute_three = minute_two + 60 * NS
        # CCC's minute-2 bar is still missing: its later bar must not advance
        # the prefix past the hole.
        assert (
            host.ingest(
                BarPoll(bars=(bar("CCC", 3, "10.80", 10, 2),), requested=("AAA", "BBB", "CCC")),
                market_ns=minute_three,
                received_ns=minute_three,
            )
            == 0
        )
        assert host.publisher.states["CCC"].segment_dollar_volume == D("211")
        assert host.heartbeat(at=minute_three, received_ns=minute_three)
        unit = host.publish(
            tmp_path / "unit.json",
            now=minute_three,
            mapping_clocks=Clocks(minute_three, minute_three, "fixture receipt"),
        )
        assert unit["status"] == "unavailable" and unit["watchlists"] is None
        # Verified backfill: the missed bar and the later bar arrive together.
        assert (
            host.ingest(
                BarPoll(
                    bars=(
                        bar("CCC", 2, "10.70", 100, 2),
                        bar("AAA", 3, "11.60", 10, 3),
                        bar("BBB", 3, "11.10", 20, 4),
                        bar("CCC", 3, "10.80", 10, 2),
                    ),
                    requested=("AAA", "BBB", "CCC"),
                ),
                market_ns=minute_three,
                received_ns=minute_three,
            )
            == 4
        )
        assert host.publisher.states["CCC"].segment_dollar_volume == D("1389")
        # The fixture registry throttles publications to one per second, so the
        # recovered unit is published on the next second.
        later = minute_three + NS
        assert host.heartbeat(at=later, received_ns=later)
        unit = host.publish(
            tmp_path / "unit.json",
            now=later,
            mapping_clocks=Clocks(later, later, "fixture receipt"),
        )
        assert unit["status"] == "fresh", unit


def test_revised_bar_invalidates_the_epoch(tmp_path: Path) -> None:
    """An identical duplicate is skipped; a changed bar needs a new epoch."""
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = prime_two_minutes(host)
        duplicate = BarPoll(bars=(bar("AAA", 1, "11.50", 10, 3),), requested=("AAA", "BBB", "CCC"))
        assert host.ingest(duplicate, market_ns=close_ns, received_ns=close_ns) == 0
        assert host.producer.error is None
        revised = BarPoll(bars=(bar("AAA", 1, "11.75", 20, 5),), requested=("AAA", "BBB", "CCC"))
        with pytest.raises(ValueError, match="revised"):
            host.ingest(revised, market_ns=close_ns, received_ns=close_ns)
        assert host.producer.error is not None
        unit = host.publish(
            tmp_path / "unit.json",
            now=close_ns,
            mapping_clocks=Clocks(close_ns, close_ns, "fixture receipt"),
        )
        assert unit["status"] == "unavailable" and unit["watchlists"] is None


def test_partial_poll_never_clears_the_list(tmp_path: Path) -> None:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = prime_two_minutes(host)
        unit = host.publish(
            tmp_path / "unit.json",
            now=close_ns,
            mapping_clocks=Clocks(close_ns, close_ns, "fixture receipt"),
        )
        assert unit["status"] == "fresh", unit

        host.ingest(
            BarPoll(bars=(), requested=("AAA", "BBB", "CCC"), failed=("CCC",)),
            market_ns=close_ns + NS,
            received_ns=close_ns + NS,
        )
        host.heartbeat(at=close_ns + NS, received_ns=close_ns + NS)
        degraded = host.publish(
            tmp_path / "unit.json",
            now=close_ns + NS,
            mapping_clocks=Clocks(close_ns + NS, close_ns + NS, "fixture receipt"),
        )
        assert degraded["status"] == "unavailable"
        assert degraded["watchlists"] is None
        written = json.loads((tmp_path / "unit.json").read_text(encoding="utf-8"))
        assert written["status"] == "unavailable"
        assert written["watchlists"] is None


def test_missing_mapping_never_clears_the_list(tmp_path: Path) -> None:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = prime_two_minutes(host)
        unit = host.publish(
            tmp_path / "unit.json",
            now=close_ns,
            mapping_clocks=Clocks(close_ns, close_ns, "fixture receipt"),
        )
        assert unit["status"] == "fresh", unit

        host.symbol_map.pop("AAA")
        host.heartbeat(at=close_ns + NS, received_ns=close_ns + NS)
        unmapped = host.publish(
            tmp_path / "unit.json",
            now=close_ns + NS,
            mapping_clocks=Clocks(close_ns + NS, close_ns + NS, "fixture receipt"),
        )
        assert unmapped["status"] == "unavailable"
        assert unmapped["watchlists"] is None
        assert "mapping" in unmapped["reason"]


def test_anchor_price_uses_open_only_at_the_segment_start(tmp_path: Path) -> None:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = DAY_START + 5 * 60 * NS
        host.ingest(
            BarPoll(
                bars=(
                    bar("AAA", 0, "11.00", 10, 3, open_="9.90"),
                    bar("BBB", 4, "11.00", 10, 3, open_="9.90"),
                ),
                requested=("AAA", "BBB", "CCC"),
            ),
            market_ns=close_ns,
            received_ns=close_ns,
        )

        def anchored(symbol: str) -> Decimal:
            tick = host.publisher.states[symbol].history.price_at(DAY_START)
            assert tick is not None
            return tick.price.as_decimal()

        assert anchored("AAA") == D("9.90")
        assert anchored("BBB") == D("10.00")
        assert host.publisher.states["AAA"].segment_dollar_volume == D("11.00") * 10
        assert host.publisher.states["BBB"].segment_dollar_volume == D("11.00") * 10


def test_symbol_map_is_explicit_and_unique(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text('{"AAA": "NASDAQ:AAA", "BBB": null}', encoding="utf-8")
    assert load_symbol_map(path) == {"AAA": "NASDAQ:AAA", "BBB": None}
    path.write_text('{"AAA": "NASDAQ:AAA", "BBB": "NASDAQ:AAA"}', encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_symbol_map(path)
    path.write_text('{"AAA": "SMART:AAA"}', encoding="utf-8")
    with pytest.raises(ValueError, match="exchange"):
        load_symbol_map(path)
    path.write_text('{"AAA": "not a venue"}', encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_symbol_map(path)


POST_START = DAY_START + 12 * 3600 * NS
POST_SCOPE = Segment(
    "POST",
    date(2026, 11, 9),
    POST_START,
    POST_START + 4 * 3600 * NS,
    "fixture XNYS regular close 16:00 ET",
)


def post_session_bar(
    symbol: str, day: date, minute: int, close: str, volume: int, trades: int
) -> Bar:
    start = int(datetime.combine(day, time(16, 0), tzinfo=NY).timestamp()) * NS + minute * 60 * NS
    return Bar(
        symbol=symbol,
        start_ns=start,
        open=D(close),
        close=D(close),
        volume=volume,
        trades=trades,
    )


def post_bar(symbol: str, minute: int, close: str, volume: int, trades: int, *, open_: str) -> Bar:
    return Bar(
        symbol=symbol,
        start_ns=POST_START + minute * 60 * NS,
        open=D(open_),
        close=D(close),
        volume=volume,
        trades=trades,
    )


def post_baseline() -> BaselineTable:
    return BaselineTable(
        tuple(
            BaselineSample(name, "POST", "2026-11-06", offset, D(dollars), D(0))
            for name in ("AAA", "BBB", "CCC")
            for offset, dollars in ((0, 0), (14_400, 3960))
        ),
        "fixture explicit complete POST curves; linear within minute",
        POST_START - NS,
        "sip",
    )


def post_publisher() -> RankingPublisher:
    pub = RankingPublisher(
        bus=MessageBus(TraderId.from_str("HOST-POST-001")),
        epoch="host-fixture-post-1",
        feed="sip",
        segment=POST_SCOPE,
        registry=registry(),
        universe=("AAA", "BBB", "CCC"),
        universe_source="fixture selected symbols; NOT PIT universe",
        baseline=post_baseline(),
    )
    pub.set_coverage(Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0))
    for name in pub.universe:
        pub.set_symbol(
            name,
            references=ReferencePrices(D("10.00"), POST_START - NS, "fixture official close"),
            asset=AssetState(POST_START - NS, POST_SCOPE.end_ns, False, "common", "fixture vendor"),
        )
    return pub


def test_post_baseline_is_built_from_supplied_sessions() -> None:
    sessions = tuple(
        (day, (post_session_bar("AAA", day, 0, "10.00", 100, 1),))
        for day in (
            date(2026, 11, 2),
            date(2026, 11, 3),
            date(2026, 11, 4),
            date(2026, 11, 5),
            date(2026, 11, 6),
        )
    )
    table = build_baseline(
        sessions,
        symbols=("AAA",),
        segment=POST_SCOPE,
        known_at=POST_START - 20 * 3600 * NS,
        provenance="fixture five prior POST sessions",
    )
    assert table.segment == "POST"
    assert table.end_s == 14_400
    assert table.points == 241
    value = table.cumulative("AAA", "POST", D(60))
    assert value is not None
    assert value.dollar_volume == D(1000)


def test_post_anchor_marks_the_segment_covered(tmp_path: Path) -> None:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        host = BarFidelityHost(
            post_publisher(),
            lease=lease,
            sip_authority="fixture SIP authority",
            symbol_map={name: f"NASDAQ:{name}" for name in ("AAA", "BBB", "CCC")},
        )
        close_ns = POST_START + 60 * NS
        ingested = host.ingest(
            BarPoll(
                bars=(post_bar("AAA", 0, "10.25", 100, 5, open_="10.00"),),
                requested=("AAA", "BBB", "CCC"),
            ),
            market_ns=close_ns,
            received_ns=close_ns,
        )
        assert ingested == 1
        state = host.publisher.states["AAA"]
        assert state.segment_covered is True
        assert state.segment_dollar_volume == D("10.25") * 100
        assert host.publisher.coverage.state is CoverageState.READY
