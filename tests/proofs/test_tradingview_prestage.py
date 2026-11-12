"""TV producer witnesses: segment units, cadence, baselines and the once-only loop."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from sextant.ranking.baseline import segment_start
from sextant.ranking.engine import Segment
from sextant.ranking.registry import Registry, RegistryRow
from sextant.tradingview.bars import Bar
from sextant.tradingview.baseline import build_baseline
from sextant.tradingview.bridge import DisplayBounds
from sextant.tradingview.inputs import InputError, read_research_inputs
from sextant.tradingview.loop import _admission
from sextant.tradingview.prestage import build_segment_baseline, publish_tick
from sextant.tradingview.producer import reserve_stream

NY = ZoneInfo("America/New_York")
NS = 1_000_000_000
MINUTE_NS = 60 * NS
D = Decimal
DAY = date(2026, 11, 9)
SYMBOLS = ("AAA", "BBB", "CCC")
PRIOR = (
    date(2026, 11, 2),
    date(2026, 11, 3),
    date(2026, 11, 4),
    date(2026, 11, 5),
    date(2026, 11, 6),
)


def at(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(day, time(hour, minute), tzinfo=NY).timestamp()) * NS


def segment(name: str) -> Segment:
    start = {"PRE": (4, 0), "RTH": (9, 30), "POST": (16, 0)}[name]
    end = {"PRE": (9, 30), "RTH": (16, 0), "POST": (20, 0)}[name]
    return Segment(
        name,
        DAY,
        at(DAY, *start),
        at(DAY, *end),
        "fixture XNYS boundary",
    )


def registry() -> Registry:
    values = {
        "mover_min_move": (D("0.03"), "fraction"),
        "mover_min_trades_accel": (D("1.5"), "ratio"),
        "mover_min_dvol_ratio": (D("3"), "ratio"),
        "move_1m_window_s": (60, "seconds"),
        "move_5m_window_s": (300, "seconds"),
        "trades_accel_window_s": (60, "seconds"),
        "trades_accel_baseline_min": (5, "minutes"),
        "publish_min_interval_s": (1, "seconds"),
        "generation_max_age_s": (1_200, "seconds"),
        "heartbeat_max_age_s": (1_200, "seconds"),
        "input_max_age_s": (600, "seconds"),
        "top_n": (2, "symbols"),
    }
    return Registry(
        tuple(
            RegistryRow(name, unit, value, "fixture only; ADR-0003", "UNCALIBRATED", DAY)
            for name, (value, unit) in values.items()
        )
    )


class RecordedSource:
    """Recorded bars and official closes, shaped like AlpacaSource."""

    def __init__(self, bars: tuple[Bar, ...], closes: dict[str, Decimal] | None = None) -> None:
        self.bars = bars
        self.closes = dict(closes) if closes is not None else {name: D("10.00") for name in SYMBOLS}
        self.ranges: list[tuple[int, int]] = []

    def minute_bars(self, symbols: Any, *, start: datetime, end: datetime) -> tuple[Bar, ...]:
        first, last = int(start.timestamp()) * NS, int(end.timestamp()) * NS
        self.ranges.append((first, last))
        names = set(symbols)
        return tuple(
            item for item in self.bars if item.symbol in names and first <= item.start_ns < last
        )

    def daily_closes(self, symbols: Any, *, day: date) -> dict[str, Decimal]:
        return {name: self.closes[name] for name in symbols if name in self.closes}


def bar(
    segment_name: str, day: date, minute: int, symbol: str, close: str, volume: int, trades: int
) -> Bar:
    start = {"PRE": at(day, 4), "RTH": at(day, 9, 30), "POST": at(day, 16)}[segment_name]
    return Bar(
        symbol=symbol,
        start_ns=start + minute * MINUTE_NS,
        open=D(close),
        close=D(close),
        volume=volume,
        trades=trades,
    )


def prior_bars(segment_name: str, sessions: tuple[date, ...]) -> tuple[Bar, ...]:
    span = {"PRE": 330, "RTH": 390, "POST": 240}[segment_name]
    return tuple(
        bar(segment_name, day, minute, symbol, "10.00", 1_000, 5)
        for day in sessions
        for minute in range(span)
        for symbol in SYMBOLS
    )


def current_bars(segment_name: str) -> tuple[Bar, ...]:
    return tuple(
        bar(
            segment_name,
            DAY,
            minute,
            symbol,
            f"{D('10.10') + D('0.10') * minute:f}",
            5_000,
            5 if minute < 5 else 40,
        )
        for minute in range(7)
        for symbol in SYMBOLS
    )


def baseline_for(segment_name: str) -> tuple[Any, RecordedSource]:
    sessions = PRIOR
    source = RecordedSource(prior_bars(segment_name, sessions) + current_bars(segment_name))
    table = build_segment_baseline(
        source,
        segment=segment(segment_name),
        symbols=SYMBOLS,
        sessions=sessions,
        known_at=at(sessions[-1], 20),
        provenance="fixture five prior sessions",
    )
    return table, source


def bounds() -> DisplayBounds:
    return DisplayBounds(D("0.05"), D("2"), 600)


def publish(segment_name: str, tmp_path: Path, *, now_minute: int = 8) -> Any:
    section = segment(segment_name)
    baseline, source = baseline_for(segment_name)
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        receipt = publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=baseline,
            symbols=SYMBOLS,
            prior_closes={name: D("10.00") for name in SYMBOLS},
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch=f"tv-producer-fixture-{segment_name.lower()}",
            universe_source="fixture directory; NOT PIT",
            now_ns=section.start_ns + now_minute * MINUTE_NS,
            unit_dir=tmp_path / "unit",
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
        )
    return receipt, section


class TailSource(RecordedSource):
    """A source that also returns a bar the vendor has not completed yet."""

    def __init__(
        self,
        bars: tuple[Bar, ...],
        closes: dict[str, Decimal] | None = None,
        *,
        tail: tuple[Bar, ...] = (),
    ) -> None:
        super().__init__(bars, closes)
        self.tail = tuple(tail)

    def minute_bars(self, symbols: Any, *, start: datetime, end: datetime) -> tuple[Bar, ...]:
        return super().minute_bars(symbols, start=start, end=end) + tuple(
            item for item in self.tail if item.symbol in set(symbols)
        )


def publish_at(
    segment_name: str,
    tmp_path: Path,
    *,
    bars: tuple[Bar, ...],
    now_ns: int,
    tail: tuple[Bar, ...] = (),
) -> tuple[Any, TailSource]:
    section = segment(segment_name)
    source = TailSource(prior_bars(segment_name, PRIOR) + bars, tail=tail)
    baseline = build_segment_baseline(
        source,
        segment=section,
        symbols=SYMBOLS,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 20),
        provenance="fixture five prior sessions",
    )
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        receipt = publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=baseline,
            symbols=SYMBOLS,
            prior_closes={name: D("10.00") for name in SYMBOLS},
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch=f"tv-producer-fixture-{segment_name.lower()}-at",
            universe_source="fixture directory; NOT PIT",
            now_ns=now_ns,
            unit_dir=tmp_path / "unit",
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
        )
    return receipt, source


@pytest.mark.parametrize("segment_name", ["PRE", "RTH", "POST"])
def test_prestage_publishes_each_segment_unit_that_reads_back(
    tmp_path: Path, segment_name: str
) -> None:
    receipt, _ = publish(segment_name, tmp_path)
    assert receipt["segment"] == segment_name
    assert receipt["bars"] > 0
    now = datetime.fromtimestamp(receipt["publish_clock_ns"] / NS, UTC)
    inputs = read_research_inputs(tmp_path / "unit", now=now, max_age=1_200)
    assert inputs.memberships["NS movers"]
    admission = _admission(inputs)
    assert admission["watchlists"]["NS movers"]
    assert admission["display_only"] is True
    assert admission["paper_slot"] == 2
    assert (tmp_path / "j5.json").is_file()


def test_prestage_switches_the_display_generation_atomically(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A reader sees the whole old or whole new unit, never a mixed generation."""
    from sextant.tradingview import prestage

    publish("RTH", tmp_path, now_minute=8)
    unit = tmp_path / "unit"
    old_sha = json.loads((unit / "display-lists.json").read_text(encoding="utf-8"))["generation"][
        "sha256"
    ]
    now = datetime.fromtimestamp(at(DAY, 9, 41) / NS, UTC)
    observed: list[str] = []
    real_replace = os.replace

    def observe(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            observed.append(read_research_inputs(unit, now=now, max_age=1_200).generation["sha256"])
        except InputError:
            observed.append("MIXED")
        result = real_replace(src, dst, *args, **kwargs)
        if Path(dst) == unit:
            observed.append(read_research_inputs(unit, now=now, max_age=1_200).generation["sha256"])
        return result

    monkeypatch.setattr(prestage.os, "replace", observe)
    publish("RTH", tmp_path, now_minute=9)
    new_sha = json.loads((unit / "display-lists.json").read_text(encoding="utf-8"))["generation"][
        "sha256"
    ]
    assert old_sha != new_sha
    assert "MIXED" not in observed
    assert old_sha in observed and new_sha in observed


def test_prestage_interrupted_pointer_switch_keeps_the_previous_unit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An interruption before the switch leaves the previous complete unit readable."""
    from sextant.tradingview import prestage

    publish("RTH", tmp_path, now_minute=8)
    unit = tmp_path / "unit"
    resolved = unit.resolve()
    previous = {
        name: (unit / name).read_bytes()
        for name in ("top100.json", "focus.json", "display-lists.json")
    }
    real_replace = os.replace

    def interrupt(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(dst) == unit:
            raise OSError("simulated interruption before the pointer switch")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(prestage.os, "replace", interrupt)
    with pytest.raises(OSError):
        publish("RTH", tmp_path, now_minute=9)
    assert unit.resolve() == resolved
    assert all((unit / name).read_bytes() == data for name, data in previous.items())
    read_research_inputs(unit, now=datetime.fromtimestamp(at(DAY, 9, 41) / NS, UTC), max_age=1_200)


def test_prestage_uses_the_shipped_cadence_floors(tmp_path: Path) -> None:
    from sextant.tradingview.prestage import cadence_floor

    cadence = Path(__file__).resolve().parents[2] / "lanes/tradingview/j5-cadence.params.json"
    assert (
        cadence_floor(datetime(2026, 11, 9, 8, 0, tzinfo=NY), registry=cadence, state_root=tmp_path)
        == 150
    )
    assert (
        cadence_floor(
            datetime(2026, 11, 9, 12, 0, tzinfo=NY), registry=cadence, state_root=tmp_path
        )
        == 300
    )
    assert (
        cadence_floor(
            datetime(2026, 11, 9, 8, 0, tzinfo=NY),
            registry=cadence,
            state_root=tmp_path,
            weekly_usage_pct=Decimal(81),
        )
        == 900
    )
    assert (
        cadence_floor(
            datetime(2026, 11, 9, 20, 30, tzinfo=NY), registry=cadence, state_root=tmp_path
        )
        is None
    )
    assert (
        cadence_floor(
            datetime(2026, 11, 14, 12, 0, tzinfo=NY), registry=cadence, state_root=tmp_path
        )
        is None
    )


@pytest.mark.parametrize("segment_name", ["PRE", "POST"])
def test_prestage_streaming_baseline_matches_the_merged_builder(segment_name: str) -> None:
    sparse = tuple(
        (
            day,
            (
                bar(segment_name, day, 0, "AAA", "10.00", 100, 1),
                bar(segment_name, day, 2, "AAA", "10.00", 200, 2),
                bar(segment_name, day, 1, "BBB", "10.00", 50, 1),
            ),
        )
        for day in PRIOR
    )
    merged = build_baseline(
        sparse,
        symbols=("AAA", "BBB"),
        segment=segment(segment_name),
        known_at=at(PRIOR[-1], 20),
        provenance="fixture streaming parity",
    )
    source = RecordedSource(tuple(item for _, bars in sparse for item in bars))
    streamed = build_segment_baseline(
        source,
        segment=segment(segment_name),
        symbols=("AAA", "BBB"),
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 20),
        provenance="fixture streaming parity",
    )
    assert streamed.identity == merged.identity
    assert [item.tobytes() for item in streamed.dv] == [item.tobytes() for item in merged.dv]
    assert [item.tobytes() for item in streamed.counts] == [
        item.tobytes() for item in merged.counts
    ]


def test_prestage_prior_sessions_skip_a_moved_segment_start() -> None:
    from sextant.tradingview.prestage import baseline_sessions

    sessions = baseline_sessions(date(2026, 11, 30), segment("POST"))
    assert len(sessions) == 5
    assert date(2026, 11, 27) not in sessions  # 13:00 ET early close moved POST
    assert date(2026, 11, 25) in sessions
    assert all(day < date(2026, 11, 30) for day in sessions)


def test_prestage_skips_a_short_prior_rth_session() -> None:
    """A 13:00 early close cannot stand in for a 16:00 RTH baseline session."""
    from sextant.tradingview.prestage import baseline_sessions

    sessions = baseline_sessions(date(2026, 11, 30), segment("RTH"))
    assert len(sessions) == 5
    assert date(2026, 11, 27) not in sessions  # 13:00 ET early close
    assert date(2026, 11, 25) in sessions


def test_prestage_baseline_covers_an_early_close_target() -> None:
    """An early-close target is covered by the normal prior prefix, not rejected."""
    from sextant.tradingview.prestage import baseline_sessions

    early = date(2026, 11, 27)
    target = segment_start(at(early, 9, 30) + NS)[0]
    assert target is not None and target.name == "RTH"
    assert target.end_ns == at(early, 13)
    sessions = baseline_sessions(early, target)
    assert len(sessions) == 5
    assert all(day < early for day in sessions)
    normal_day = date(2026, 11, 30)
    normal = segment_start(at(normal_day, 9, 30) + NS)[0]
    assert normal is not None
    assert date(2026, 11, 27) not in baseline_sessions(normal_day, normal)


def test_prestage_rejects_registry_values_that_need_coercion(tmp_path: Path) -> None:
    """Booleans and floats are refused before any int/Decimal conversion."""
    from sextant.tradingview.prestage import PrestageError, load_registry

    source = Path(__file__).resolve().parents[2] / "lanes/tradingview/k2-ranking.params.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    for name, bad in (("top_n", True), ("top_n", 1.9), ("mover_min_move", 0.03)):
        document = json.loads(json.dumps(base))
        row = next(item for item in document["rows"] if item["name"] == name)
        row["value"] = bad
        path = tmp_path / f"registry-{name}-{bad}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(PrestageError):
            load_registry(path)


def test_prestage_refuses_a_missing_prior_close() -> None:
    from sextant.tradingview.prestage import collect_references

    source = RecordedSource((), closes={"AAA": D("10.00"), "BBB": D("10.00")})
    symbols, closes = collect_references(source, day=PRIOR[-1], symbols=SYMBOLS)
    assert symbols == ("AAA", "BBB")
    assert set(closes) == {"AAA", "BBB"}


def test_prestage_daily_closes_are_requested_and_parsed() -> None:
    from sextant.tradingview.bars import PacedStockBars
    from sextant.tradingview.prestage import AlpacaDailyBars

    calls: list[dict] = []
    waits: list[int] = []

    class FakePacer:
        def wait(self) -> None:
            waits.append(len(waits))

    client = PacedStockBars(
        pacer=FakePacer(),
        api_key="synthetic",
        secret_key="synthetic",  # noqa: S106
    )

    def request(method: str, url: str, **opts: object) -> Any:
        params = dict(opts["params"])  # type: ignore[arg-type]
        calls.append(params)
        return _Response(
            200,
            {
                "bars": {
                    "AAA": [
                        {
                            "t": "2026-11-06T05:00:00Z",
                            "o": 10.0,
                            "h": 10.6,
                            "l": 9.9,
                            "c": 10.5,
                            "v": 1000,
                            "n": 10,
                            "vw": 10.2,
                        }
                    ]
                }
            },
        )

    daily = AlpacaDailyBars(client, batch=2)
    with patch.object(client._session, "request", request):
        closes = daily.daily_closes(("AAA", "BBB"), day=PRIOR[-1])
    assert closes == {"AAA": D("10.5")}
    assert len(calls) == 1 and len(waits) == 1
    from alpaca.data.enums import Adjustment

    assert str(calls[0]["timeframe"]) == "1Day"
    assert calls[0]["adjustment"].value == Adjustment.SPLIT.value == "split"


class _Response:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)

    def json(self) -> dict:
        assert self._payload is not None
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"synthetic {self.status_code}")


def test_prestage_replays_every_completed_minute(tmp_path: Path, monkeypatch: Any) -> None:
    """The replay ingests each completed minute once; no unsourced prefix compression."""
    from sextant.tradingview.host import BarFidelityHost
    from sextant.tradingview.prestage import publish_tick

    section = segment("RTH")
    minutes = 25
    bars = tuple(
        bar("RTH", DAY, minute, symbol, f"{D('10.10') + D('0.10') * minute:f}", 5_000, 40)
        for minute in range(minutes)
        for symbol in SYMBOLS
    )
    source = RecordedSource(prior_bars("RTH", PRIOR) + bars)
    baseline = build_segment_baseline(
        source,
        segment=section,
        symbols=SYMBOLS,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 20),
        provenance="fixture five prior sessions",
    )
    calls: list[int] = []
    original = BarFidelityHost.ingest

    def spy(self: Any, poll: Any, *, market_ns: int, received_ns: int) -> int:
        calls.append(market_ns)
        return original(self, poll, market_ns=market_ns, received_ns=received_ns)

    monkeypatch.setattr(BarFidelityHost, "ingest", spy)
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=baseline,
            symbols=SYMBOLS,
            prior_closes={name: D("10.00") for name in SYMBOLS},
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch="tv-producer-fixture-compression",
            universe_source="fixture directory; NOT PIT",
            now_ns=section.start_ns + minutes * MINUTE_NS,
            unit_dir=tmp_path / "unit",
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
        )
    assert len(calls) == minutes


def test_prestage_drops_an_unfinished_minute(tmp_path: Path) -> None:
    """The in-progress minute is not replayed, and the request stops before it."""
    section = segment("RTH")
    unfinished = bar("RTH", DAY, 8, "AAA", "11.00", 100, 2)
    now_ns = section.start_ns + 8 * MINUTE_NS + 30 * NS
    receipt, source = publish_at(
        "RTH",
        tmp_path,
        bars=current_bars("RTH"),
        now_ns=now_ns,
        tail=(unfinished,),
    )
    assert receipt["bars"] == 7 * len(SYMBOLS)
    assert source.ranges[-1] == (section.start_ns, section.start_ns + 8 * MINUTE_NS)


def test_prestage_keeps_halt_eligibility_unknown(tmp_path: Path) -> None:
    """No status receipt means no name is eligible for focus, not a free pass."""
    publish("RTH", tmp_path)
    j5 = json.loads((tmp_path / "j5.json").read_text(encoding="utf-8"))
    assert all(row["focus_eligible"] is False for row in j5["ranking_generation"]["rows"])
    display = json.loads((tmp_path / "unit" / "display-lists.json").read_text(encoding="utf-8"))
    assert display["ns_focus"] == []
    assert display["counts"]["ns_focus"] == 0


def test_prestage_unit_cannot_outlive_the_j5_deadline(tmp_path: Path) -> None:
    """The projected unit keeps the J5 deadline, so a later read cannot revive it."""
    bars = tuple(
        bar("PRE", DAY, minute, symbol, "10.20", 5_000, 40)
        for minute in range(320, 329)
        for symbol in SYMBOLS
    )
    now_ns = at(DAY, 9, 29) + 30 * NS
    publish_at("PRE", tmp_path, bars=bars, now_ns=now_ns)
    now = datetime.fromtimestamp(now_ns / NS, UTC)
    inputs = read_research_inputs(tmp_path / "unit", now=now, max_age=1_200)
    assert inputs.expires_at == datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
    with pytest.raises(InputError):
        read_research_inputs(
            tmp_path / "unit",
            now=datetime.fromtimestamp(at(DAY, 9, 31) / NS, UTC),
            max_age=1_200,
        )


def test_segment_start_labels_the_fixture_boundaries() -> None:
    for name, hour, minute in (("PRE", 4, 0), ("RTH", 9, 30), ("POST", 16, 0)):
        found, session_date, _ = segment_start(at(DAY, hour, minute + 1))
        assert found is not None
        assert found.name == name
        assert session_date == DAY


def directory_texts(tmp_path: Path) -> tuple[Path, Path]:
    nasdaq = tmp_path / "nasdaqlisted.txt"
    nasdaq.write_text(
        "Symbol|Security Name|Test Issue|ETF\n"
        "AAA|Alpha Corporation|N|N\n"
        "BBB|Beta Corporation|N|N\n"
        "CCC|Gamma Corporation|N|N\n"
        "File Creation Time|2026-11-09 00:00:00.000\n",
        encoding="utf-8",
    )
    other = tmp_path / "otherlisted.txt"
    other.write_text(
        "ACT Symbol|Security Name|Test Issue|ETF\n",
        encoding="utf-8",
    )
    return nasdaq, other


class DayCloses(RecordedSource):
    """Recorded bars plus per-day official closes, so a reference day is observable."""

    def __init__(
        self, bars: tuple[Bar, ...], closes_by_day: dict[date, dict[str, Decimal]]
    ) -> None:
        super().__init__(bars)
        self.closes_by_day = closes_by_day
        self.days: list[date] = []

    def daily_closes(self, symbols: Any, *, day: date) -> dict[str, Decimal]:
        self.days.append(day)
        closes = self.closes_by_day.get(day, {})
        return {name: closes[name] for name in symbols if name in closes}


def _run_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    nasdaq, other = directory_texts(tmp_path)
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(
        json.dumps({name: f"NASDAQ:{name}" for name in SYMBOLS}), encoding="utf-8"
    )
    return nasdaq, other, symbol_map


def test_prestage_post_reference_is_the_same_session_close(tmp_path: Path) -> None:
    """POST moves are measured from D's official close, not D-1's (ADR-0003 K2)."""
    from sextant.tradingview.prestage import run

    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    closes = {
        PRIOR[-1]: {name: D("10.00") for name in SYMBOLS},
        DAY: {name: D("10.70") for name in SYMBOLS},
    }
    source = DayCloses(prior_bars("POST", PRIOR) + current_bars("POST"), closes)
    code = run(
        research_dir=tmp_path / "unit",
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=symbol_map,
        bounds=bounds(),
        state_root=tmp_path / "state",
        once=True,
        clock=lambda: at(DAY, 16, 8),
        sleep=lambda seconds: None,
        source_factory=lambda lease: source,
        page=lambda line: None,
    )
    assert code == 0
    assert source.days == [DAY]
    j5 = json.loads((tmp_path / "state" / "j5-unit.json").read_text(encoding="utf-8"))
    moves = {
        row["symbol"]: D(feature["value"])
        for row in j5["ranking_generation"]["rows"]
        for feature in row["features"]
        if feature["name"] == "move"
    }
    assert moves == {name: D("0") for name in SYMBOLS}


def test_prestage_run_once_refuses_outside_supported_segments(tmp_path: Path) -> None:
    from sextant.tradingview.prestage import run

    nasdaq, other = directory_texts(tmp_path)
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(
        json.dumps({name: f"NASDAQ:{name}" for name in SYMBOLS}), encoding="utf-8"
    )
    pages: list[str] = []
    code = run(
        research_dir=tmp_path / "unit",
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=symbol_map,
        bounds=bounds(),
        state_root=tmp_path / "state",
        once=True,
        clock=lambda: at(DAY, 2, 0),
        sleep=lambda seconds: None,
        page=pages.append,
    )
    assert code == 0
    assert not (tmp_path / "unit" / "top100.json").exists()
    assert any("overnight" in line.lower() or "closed" in line.lower() for line in pages)


def test_prestage_run_once_publishes_a_fresh_unit(tmp_path: Path) -> None:
    from sextant.tradingview.prestage import run

    nasdaq, other = directory_texts(tmp_path)
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(
        json.dumps({name: f"NASDAQ:{name}" for name in SYMBOLS}), encoding="utf-8"
    )
    source = RecordedSource(prior_bars("RTH", PRIOR) + current_bars("RTH"))
    code = run(
        research_dir=tmp_path / "unit",
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=symbol_map,
        bounds=bounds(),
        state_root=tmp_path / "state",
        once=True,
        clock=lambda: at(DAY, 9, 38),
        sleep=lambda seconds: None,
        source_factory=lambda lease: source,
        page=lambda line: None,
    )
    assert code == 0
    inputs = read_research_inputs(
        tmp_path / "unit", now=datetime(2026, 11, 9, 9, 38, tzinfo=NY), max_age=1_200
    )
    assert inputs.memberships["NS movers"]


def test_prestage_validation_failure_keeps_the_previous_unit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The three files are replaced only after the staged copy validates."""
    from sextant.tradingview import prestage

    publish("RTH", tmp_path, now_minute=8)
    unit = tmp_path / "unit"
    before = {name: (unit / name).read_bytes() for name in ("top100.json", "focus.json")}

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("fixture post-write refusal")

    monkeypatch.setattr(prestage, "read_research_inputs", refuse)
    with pytest.raises(ValueError, match="fixture post-write refusal"):
        publish("RTH", tmp_path, now_minute=9)
    assert {name: (unit / name).read_bytes() for name in before} == before


def test_prestage_omits_and_counts_unmapped_names(tmp_path: Path) -> None:
    """An unmapped or null name leaves the display universe, counted; an empty map refuses."""
    from sextant.tradingview.prestage import PrestageError, load_maps

    nasdaq, other = directory_texts(tmp_path)
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(json.dumps({"AAA": "NASDAQ:AAA", "BBB": None}), encoding="utf-8")
    universe, mapping, provenance = load_maps(nasdaq, other, symbol_map)
    assert universe == ("AAA",) and mapping == {"AAA": "NASDAQ:AAA"}
    assert "1 of 3 directory symbols mapped, 2 unmapped omitted" in provenance
    symbol_map.write_text(json.dumps({"BBB": None}), encoding="utf-8")
    with pytest.raises(PrestageError, match="symbol map maps none"):
        load_maps(nasdaq, other, symbol_map)


def test_prestage_skips_a_tick_without_completed_bars(tmp_path: Path) -> None:
    from sextant.tradingview.prestage import run

    nasdaq, other = directory_texts(tmp_path)
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(
        json.dumps({name: f"NASDAQ:{name}" for name in SYMBOLS}), encoding="utf-8"
    )
    unit = tmp_path / "unit"
    unit.mkdir()
    previous = unit / "top100.json"
    previous.write_text('{"schema": "previous"}', encoding="utf-8")
    source = RecordedSource(prior_bars("RTH", PRIOR))
    code = run(
        research_dir=unit,
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=symbol_map,
        bounds=bounds(),
        state_root=tmp_path / "state",
        once=True,
        clock=lambda: at(DAY, 9, 31),
        sleep=lambda seconds: None,
        source_factory=lambda lease: source,
        page=lambda line: None,
    )
    assert code == 1
    assert previous.read_text(encoding="utf-8") == '{"schema": "previous"}'


def test_prestage_reference_session_ignores_the_baseline_filter(tmp_path: Path) -> None:
    """A 13:00 early close is still the last completed session for the official close."""
    from sextant.tradingview.prestage import baseline_sessions, prior_sessions, run

    session_day = date(2026, 11, 30)
    section = Segment(
        "RTH",
        session_day,
        at(session_day, 9, 30),
        at(session_day, 16, 0),
        "fixture XNYS regular close 16:00 ET",
    )
    sessions = baseline_sessions(session_day, section)
    assert date(2026, 11, 27) not in sessions
    assert sessions[-1] == date(2026, 11, 25)
    reference_day = prior_sessions(session_day, 1)[-1]
    assert reference_day == date(2026, 11, 27)
    current = tuple(
        bar("RTH", session_day, minute, symbol, f"{D('10.10') + D('0.10') * minute:f}", 5_000, 40)
        for minute in range(7)
        for symbol in SYMBOLS
    )
    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    source = DayCloses(
        prior_bars("RTH", sessions) + current,
        {
            date(2026, 11, 25): {name: D("10.00") for name in SYMBOLS},
            date(2026, 11, 27): {name: D("10.00") for name in SYMBOLS},
        },
    )
    code = run(
        research_dir=tmp_path / "unit",
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=symbol_map,
        bounds=bounds(),
        state_root=tmp_path / "state",
        once=True,
        clock=lambda: at(session_day, 9, 38),
        sleep=lambda seconds: None,
        source_factory=lambda lease: source,
        page=lambda line: None,
    )
    assert code == 0
    assert source.days == [date(2026, 11, 27)]


def test_operator_path_builds_the_client_from_the_with_keys_environment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from sextant.tradingview.prestage import KEY_NAMES, operator_source

    monkeypatch.setenv(KEY_NAMES[0], "fixture-operator-key")
    monkeypatch.setenv(KEY_NAMES[1], "fixture-operator-secret")
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        source = operator_source(lease, requests_per_minute=60)
    client = source._bars.client
    assert client._api_key == os.environ[KEY_NAMES[0]]
    assert client._secret_key == os.environ[KEY_NAMES[1]]
    assert source._daily._client is client


def test_operator_path_refuses_missing_credentials(tmp_path: Path, monkeypatch: Any) -> None:
    from sextant.tradingview.prestage import KEY_NAMES, PrestageError, operator_source

    monkeypatch.delenv(KEY_NAMES[0], raising=False)
    monkeypatch.delenv(KEY_NAMES[1], raising=False)
    with (
        reserve_stream(
            tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
        ) as lease,
        pytest.raises(PrestageError) as refusal,
    ):
        operator_source(lease, requests_per_minute=60)
    message = str(refusal.value)
    assert KEY_NAMES[0] in message and KEY_NAMES[1] in message
    assert "with-keys" in message


def test_the_shipped_request_budget_is_the_ruled_200_per_minute() -> None:
    """The shipped row carries the ruled value, both authorities and no calibration claim."""
    from sextant.tradingview.prestage import DEFAULT_PRESTAGE_PARAMETERS, load_request_budget

    assert load_request_budget() == 200
    (row,) = json.loads(DEFAULT_PRESTAGE_PARAMETERS.read_text(encoding="utf-8"))
    assert (row["calibration"], row["scope"]) == ("UNCALIBRATED", "REVIEWED")
    source = row["source"]
    assert "docs/handbook/rank-live.md:124-125" in source
    assert "https://docs.alpaca.markets/us/docs/about-market-data-api" in source
    assert "200 / min" in source
    assert "lead's header read, 2026-11-10 23:17 EST" in source
    assert "shared" in source


def test_a_request_budget_row_without_a_value_is_refused(tmp_path: Path) -> None:
    """The earlier unavailable shape still refuses: a null value is never a default."""
    from sextant.tradingview.prestage import PrestageError, load_request_budget

    path = tmp_path / "prestage-parameters.json"
    row = {"name": "prestage_request_budget", "value": None, "scope": "UNAVAILABLE"}
    path.write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(PrestageError, match="request budget"):
        load_request_budget(path)


def test_prestage_request_budget_reads_a_reviewed_row(tmp_path: Path) -> None:
    from sextant.tradingview.prestage import load_request_budget

    path = tmp_path / "prestage-parameters.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "prestage_request_budget",
                    "unit": "requests/minute",
                    "value": 60,
                    "source": "fixture only; no authority",
                    "calibration": "UNCALIBRATED",
                    "last_change": "2026-11-10",
                    "scope": "REVIEWED",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert load_request_budget(path) == 60


def test_prestage_run_with_the_shipped_budget_still_needs_the_with_keys_pair(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The shipped budget passes; the key seam is the next refusal, before any request."""
    from sextant.tradingview.prestage import KEY_NAMES, PrestageError, run

    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    with pytest.raises(PrestageError, match="with-keys"):
        run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            state_root=tmp_path / "state",
            once=True,
            clock=lambda: at(DAY, 9, 38),
            sleep=lambda seconds: None,
            page=lambda line: None,
        )
    assert not (tmp_path / "unit").exists()
