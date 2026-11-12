"""ADR-0003 entry-heartbeat fixtures: schema-only ages and a post-publication hook.

Source: house composition over the pinned reader contract
(`src/sextant/ranking/reader.py:52-64`); pinned rc8 has no producer heartbeat for
custom ranking data. Every age value below is a synthetic fixture: ADR-0003, line 42, keeps
the three entry ages UNAVAILABLE until sourced, so this file proves mechanics,
never a market authority. No stream is started and no order surface is touched.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.common import MessageBus
from nautilus_trader.model import CustomData
from test_ranking import DAY, NS, SCOPE, START, baseline, publisher, registry
from test_ranking_consumer import consumer, published, tick, warm

from sextant.ranking.consumer import RankingConsumer, RawBusHost
from sextant.ranking.engine import ReferencePrices
from sextant.ranking.generation import Coverage, CoverageState, RankingGeneration
from sextant.ranking.heartbeat import HeartbeatEmitter
from sextant.ranking.publisher import AssetState, RankingPublisher, ranking_data_type
from sextant.ranking.registry import (
    ENTRY_ROWS,
    Registry,
    RegistryRow,
    entry_ages,
)

ROOT = Path(__file__).parents[2]
HEARTBEAT = ROOT / "src/sextant/ranking/heartbeat.py"
AT = START + 360 * NS
D = Decimal
SOURCE = "synthetic fixture only; ADR-0003 entry ages UNAVAILABLE until sourced"
ENTRY_NAMES = (
    "entry_heartbeat_max_age_s",
    "entry_generation_max_age_s",
    "entry_input_max_age_s",
)


class Sink:
    """Records what the emitter sends and can refuse to prove a retry is not burned."""

    def __init__(self, responses: list[bool] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._responses = list(responses or ())

    def __call__(self, *, at: int, received_ns: int) -> bool:
        self.calls.append((at, received_ns))
        return self._responses.pop(0) if self._responses else True


class RecordingBus:
    """A raw bus stand-in: records topics and can fail the full or top publication."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.events: list[str] = []
        self.calls = 0
        self.fail_at = fail_at

    def publish(self, topic: str, message: Any, external_pub: bool = True) -> None:
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("publication failed")
        self.events.append(topic)


def entry_rows(*, values: tuple[int, int, int] = (3, 3, 5)) -> tuple[RegistryRow, ...]:
    return tuple(
        RegistryRow(
            name=name,
            unit="seconds",
            value=value,
            source=SOURCE,
            calibration_state="UNCALIBRATED",
            last_change=DAY,
        )
        for name, value in zip(ENTRY_NAMES, values, strict=True)
    )


def build(bus: MessageBus, *, heartbeat: HeartbeatEmitter | None = None) -> RankingPublisher:
    pub = RankingPublisher(
        bus=bus,
        epoch="fixture-1",
        feed="sip",
        segment=SCOPE,
        registry=registry(),
        universe=("PENNY", "ADR", "LARGE"),
        universe_source="fixture universe",
        baseline=baseline(),
        heartbeat=heartbeat,
    )
    pub.set_coverage(Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0))
    for name in pub.universe:
        pub.set_symbol(
            name,
            references=ReferencePrices(D("0.50"), START - NS, "fixture official close"),
            asset=AssetState(START - NS, SCOPE.end_ns, False, "common", "fixture vendor"),
        )
    return pub


def deployment(pub: RankingPublisher, **ages: int) -> RankingConsumer:
    """Widen the display ages so only the synthetic entry age under test can bind."""
    wide = Registry(
        tuple(
            replace(row, value=10)
            if row.name in {"heartbeat_max_age_s", "generation_max_age_s", "input_max_age_s"}
            else row
            for row in registry().rows
        )
    )
    pub.registry = wide
    limits = {"heartbeat": 10, "generation": 10, "inputs": 10, **ages}
    return RankingConsumer(
        registry=wide,
        feed="sip",
        segment=pub.segment,
        universe_hash=pub.universe_hash,
        baseline_hash=pub.baseline.identity,
        entry_heartbeat_max_age_s=limits["heartbeat"],
        entry_generation_max_age_s=limits["generation"],
        entry_input_max_age_s=limits["inputs"],
    )


def test_the_emitter_never_manufactures_a_heartbeat_without_a_generation() -> None:
    pub = publisher()
    sink = Sink()
    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT, sink=sink)
    generation = published(pub, at=AT)
    degraded = replace(
        generation, coverage=replace(generation.coverage, state=CoverageState.DEGRADED)
    )
    incomplete = replace(generation, coverage=replace(generation.coverage, expected_symbols=4))
    assert not emitter.emit(cast(RankingGeneration, None))
    assert not emitter.emit(degraded)
    assert not emitter.emit(incomplete)
    assert sink.calls == []


def test_a_stale_receipt_is_never_sent() -> None:
    pub = publisher()
    sink = Sink()
    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT + 3 * NS, sink=sink)
    assert not emitter.emit(published(pub, at=AT))
    assert sink.calls == []


def test_the_emitter_sends_exactly_what_the_reader_accepts() -> None:
    pub = publisher()
    entry = consumer(pub)
    calls: list[tuple[int, int]] = []

    def sink(*, at: int, received_ns: int) -> bool:
        calls.append((at, received_ns))
        return entry.accept_heartbeat(at=at, received_ns=received_ns)

    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT, sink=sink)
    generation = published(pub, at=AT)
    assert emitter.emit(generation)
    assert calls == [(AT, AT)]
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=AT)
    assert entry.entry_rank("PENNY", now=AT) == 1
    assert entry.reason == "fresh"


def test_a_refused_sink_does_not_burn_the_heartbeat() -> None:
    pub = publisher()
    sink = Sink(responses=[False, True])
    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT, sink=sink)
    generation = published(pub, at=AT)
    assert not emitter.emit(generation)
    assert emitter.emit(generation)
    assert sink.calls == [(AT, AT), (AT, AT)]


def test_the_emitter_never_sends_a_duplicate_or_regressed_heartbeat() -> None:
    pub = publisher()
    now = [AT]
    sink = Sink()
    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: now[0], sink=sink)
    first = published(pub, at=AT)
    assert emitter.emit(first)
    pub.on_trade(tick(AT + 2 * NS))
    second = pub.publish(now=AT + 2 * NS)
    assert second is not None
    now[0] = AT + 2 * NS
    assert emitter.emit(second)
    assert not emitter.emit(first)
    assert len(sink.calls) == 2


@pytest.mark.parametrize(
    "received",
    [float(AT + NS // 2), Decimal(AT + NS // 2)],
    ids=["float", "decimal"],
)
def test_p3_a_float_or_decimal_receipt_clock_at_publication_scale_is_never_sent(
    received: float | Decimal,
) -> None:
    # Without the receipt-clock type check both values sit inside every numeric bound
    # (0 <= at <= received, receipt age 0.5 s < heartbeat age 2 s) and reach the sink.
    pub = publisher()
    sink = Sink()
    emitter = HeartbeatEmitter(
        registry=registry(),
        clock=cast(Any, lambda: received),
        sink=sink,
    )
    assert not emitter.emit(published(pub, at=AT))
    assert sink.calls == []


@pytest.mark.parametrize(
    "received",
    [True, None, 1.5, -1, float(AT), Decimal(AT), "1"],
    ids=["bool", "none", "float-small", "negative", "float-ns", "decimal-ns", "str"],
)
def test_any_non_integer_receipt_clock_is_refused_before_the_sink(received: Any) -> None:
    pub = publisher()
    sink = Sink()
    emitter = HeartbeatEmitter(registry=registry(), clock=cast(Any, lambda: received), sink=sink)
    assert not emitter.emit(published(pub, at=AT))
    assert sink.calls == []


def test_the_entry_schema_is_separate_from_the_display_registry() -> None:
    display = registry()
    before = display.params_hash
    assert len(display.rows) == 12
    assert set(ENTRY_ROWS).isdisjoint({row.name for row in display.rows})
    assert entry_ages(entry_rows()) == {
        "entry_heartbeat_max_age_s": 3,
        "entry_generation_max_age_s": 3,
        "entry_input_max_age_s": 5,
    }
    assert display.params_hash == before
    extra = RegistryRow(
        name="entry_heartbeat_max_age_s",
        unit="seconds",
        value=3,
        source=SOURCE,
        calibration_state="UNCALIBRATED",
        last_change=DAY,
    )
    with pytest.raises(ValueError):
        Registry((*display.rows, extra))


@pytest.mark.parametrize(
    "bad",
    [
        entry_rows()[:-1],
        (*entry_rows(), entry_rows()[0]),
        (replace(entry_rows()[0], name="entry_other"), *entry_rows()[1:]),
        (replace(entry_rows()[0], name="heartbeat_max_age_s"), *entry_rows()[1:]),
        (replace(entry_rows()[0], value=True), *entry_rows()[1:]),
        (replace(entry_rows()[0], value=3.0), *entry_rows()[1:]),
        (replace(entry_rows()[0], value=0), *entry_rows()[1:]),
        (replace(entry_rows()[0], unit="minutes"), *entry_rows()[1:]),
        (replace(entry_rows()[0], source=""), *entry_rows()[1:]),
        (replace(entry_rows()[0], calibration_state="CALIBRATED"), *entry_rows()[1:]),
        (replace(entry_rows()[0], last_change=None), *entry_rows()[1:]),
        (replace(entry_rows()[0], last_change="2026-11-10"), *entry_rows()[1:]),
        (
            replace(entry_rows()[0], last_change=datetime(2026, 11, 10, 12, 0)),
            *entry_rows()[1:],
        ),
    ],
    ids=[
        "missing",
        "duplicate",
        "unknown",
        "display-name",
        "bool",
        "float",
        "zero",
        "unit",
        "source",
        "calibration",
        "date-none",
        "date-iso",
        "date-datetime",
    ],
)
def test_entry_ages_refuse_anything_but_three_positive_integer_rows(
    bad: tuple[RegistryRow, ...],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        entry_ages(bad)


@pytest.mark.parametrize("source", [123, None, b"fixture"])
def test_entry_ages_refuse_a_non_string_source(source: Any) -> None:
    rows = entry_rows()
    with pytest.raises(ValueError, match="registry source and last change required"):
        entry_ages((replace(rows[0], source=source), rows[1], rows[2]))


def test_entry_ages_are_required_keyword_only_positive_integers() -> None:
    params = inspect.signature(RankingConsumer).parameters
    for name in ENTRY_NAMES:
        parameter = params[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
    for bad in (True, 0, -1, 3.0, None):
        with pytest.raises((TypeError, ValueError)):
            RankingConsumer(
                registry=registry(),
                feed="sip",
                segment=SCOPE,
                universe_hash=publisher().universe_hash,
                baseline_hash=baseline().identity,
                entry_heartbeat_max_age_s=cast(Any, bad),
                entry_generation_max_age_s=3,
                entry_input_max_age_s=3,
            )


def test_no_entry_age_values_are_shipped() -> None:
    assert not (ROOT / "lanes/research/entry-ranking.params.json").exists()


def test_the_entry_age_authority_stays_unavailable() -> None:
    adr = (ROOT / "docs/adr/0003-ranking-and-feeds.md").read_text()
    assert "UNAVAILABLE until sourced" in adr
    manifest = (ROOT / "docs/manifest.md").read_text()
    rows = [line for line in manifest.splitlines() if "entry_heartbeat_max_age_s" in line]
    assert len(rows) == 1
    assert "UNAVAILABLE until sourced" in rows[0]
    assert "sourced by" not in rows[0].lower()


def test_the_heartbeat_module_starts_no_stream_or_thread() -> None:
    tree = ast.parse(HEARTBEAT.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert not imported & {"socket", "ssl", "threading", "asyncio", "websockets"}


@pytest.mark.parametrize(
    ("age", "offset", "expected"),
    [
        ("heartbeat", 3 * NS, 1),
        ("heartbeat", 3 * NS + 1, None),
        ("generation", 3 * NS, 1),
        ("generation", 3 * NS + 1, None),
        ("inputs", 5 * NS, 1),
        ("inputs", 5 * NS + 1, None),
    ],
    ids=[
        "heartbeat-at-3",
        "heartbeat-at-3-plus-1",
        "generation-at-3",
        "generation-at-3-plus-1",
        "input-at-5",
        "input-at-5-plus-1",
    ],
)
def test_synthetic_entry_ages_bind_at_their_integer_second_boundaries(
    age: str, offset: int, expected: int | None
) -> None:
    pub = publisher()
    limit = 5 if age == "inputs" else 3
    entry = deployment(pub, **{age: limit})
    generation = published(pub, at=AT)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=AT)
    now = AT + offset
    if age == "inputs":
        pub.on_trade(tick(now, name="ADR"))
        later = pub.publish(now=now)
        assert later is not None
        assert entry.handle(CustomData(ranking_data_type("sip"), later), now=now)
        assert entry.accept_heartbeat(at=now, received_ns=now)
    else:
        assert entry.accept_heartbeat(at=AT, received_ns=AT)
    assert entry.entry_rank("PENNY", now=now) == expected


def test_the_entry_input_age_binds_per_symbol_not_per_generation() -> None:
    pub = publisher()
    entry = consumer(pub, inputs=1)
    generation = published(pub, at=AT)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=AT)
    now = AT + NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    later = pub.publish(now=now)
    assert later is not None
    assert entry.handle(CustomData(ranking_data_type("sip"), later), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    assert entry.entry_rank("PENNY", now=now) is None
    assert entry.reason == "symbol_input_stale"


@pytest.mark.parametrize("feature", ["price", "move", "dvol_ratio", "trades_accel"])
def test_each_required_feature_uses_its_own_event_clock(feature: str) -> None:
    """GPT 0c331ed3's per-feature witness: age one feature, not the whole candidate."""
    pub = publisher()
    warm(pub)
    now = AT + NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    generation = pub.publish(now=now)
    assert generation is not None
    rows = tuple(
        replace(
            row,
            features=tuple(
                replace(f, ts_event=AT if f.name == feature else now, known_at=now)
                for f in row.features
            ),
        )
        if row.symbol == "PENNY"
        else row
        for row in generation.rows
    )
    generation = replace(generation, rows=rows)
    entry = consumer(pub, inputs=1)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    assert entry.reader.fresh(now)
    assert entry.entry_rank("PENNY", now=now) is None
    assert entry.reason == "symbol_input_stale"
    assert entry.ranked_names(now=now) == ()
    assert entry.reason == "symbol_input_stale"


def test_the_heartbeat_hook_runs_after_both_publications() -> None:
    bus = RecordingBus()
    calls: list[tuple[int, int]] = []

    def sink(*, at: int, received_ns: int) -> bool:
        calls.append((at, received_ns))
        bus.events.append("heartbeat")
        return True

    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT, sink=sink)
    pub = build(cast(MessageBus, bus), heartbeat=emitter)
    warm(pub)
    generation = pub.publish(now=AT)
    assert generation is not None
    full_topic = ranking_data_type("sip").topic
    top_topic = ranking_data_type("sip", "top").topic
    assert bus.events == [full_topic, top_topic, "heartbeat"]
    assert calls == [(AT, AT)]
    assert pub.published_at == AT
    assert pub.sequence == 1


def test_a_raising_sink_never_fails_a_committed_publication() -> None:
    """Follow-up #371: a raising sink is counted as not sent, never raised."""
    bus = RecordingBus()
    calls: list[tuple[int, int]] = []

    def sink(*, at: int, received_ns: int) -> bool:
        calls.append((at, received_ns))
        raise RuntimeError("heartbeat sink failed")

    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT, sink=sink)
    pub = build(cast(MessageBus, bus), heartbeat=emitter)
    warm(pub)
    generation = pub.publish(now=AT)
    full_topic = ranking_data_type("sip").topic
    top_topic = ranking_data_type("sip", "top").topic
    assert generation is not None
    assert bus.events == [full_topic, top_topic]
    assert calls == [(AT, AT)]
    assert pub.published_at == AT
    assert pub.sequence == 1
    assert emitter.sink_failures == 1


@pytest.mark.parametrize(("fail_at", "events"), [(1, []), (2, [ranking_data_type("sip").topic])])
def test_a_full_or_top_publication_failure_never_emits_a_heartbeat(
    fail_at: int, events: list[str]
) -> None:
    bus = RecordingBus(fail_at=fail_at)
    sink = Sink()
    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT, sink=sink)
    pub = build(cast(MessageBus, bus), heartbeat=emitter)
    warm(pub)
    with pytest.raises(RuntimeError, match="publication failed"):
        pub.publish(now=AT)
    assert bus.events == events
    assert "heartbeat" not in bus.events
    assert sink.calls == []
    assert pub.published_at is None


def test_a_delayed_receipt_is_sent_with_the_generation_clock() -> None:
    pub = publisher()
    entry = consumer(pub)
    sink = Sink()
    emitter = HeartbeatEmitter(registry=registry(), clock=lambda: AT + NS // 2, sink=sink)
    pub.heartbeat = emitter
    generation = published(pub, at=AT)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=AT)
    assert entry.accept_heartbeat(at=AT, received_ns=AT + NS // 2)
    assert entry.entry_rank("PENNY", now=AT + NS // 2) == 1
    assert sink.calls == [(AT, AT + NS // 2)]


@pytest.mark.parametrize("mode", ["quiet", "halted"])
def test_quiet_and_halted_markets_still_emit_a_fresh_heartbeat(mode: str) -> None:
    pub = publisher()
    now = [AT]
    calls: list[tuple[int, int]] = []
    alarms: list[tuple[str, str | None]] = []
    entry = consumer(pub, alarm=lambda reason, symbol: alarms.append((reason, symbol)))

    def sink(*, at: int, received_ns: int) -> bool:
        calls.append((at, received_ns))
        return entry.accept_heartbeat(at=at, received_ns=received_ns)

    pub.heartbeat = HeartbeatEmitter(registry=registry(), clock=lambda: now[0], sink=sink)
    RawBusHost(entry, clock=lambda: now[0]).subscribe(pub.bus)
    published(pub, at=AT)
    assert len(calls) == 1
    if mode == "quiet":
        pub.on_trade(tick(now[0], "0.50"))
    else:
        for name in ("PENNY", "ADR", "LARGE"):
            asset = pub.assets[name]
            assert asset is not None
            pub.set_symbol(
                name,
                references=pub.references[name],
                asset=replace(asset, halted=True),
            )
        pub.on_trade(tick(now[0], "0.56"))
    now[0] = AT + NS
    second = pub.publish(now=now[0])
    assert second is not None
    assert second.coverage.state is CoverageState.READY
    assert not any(row.focus_eligible for row in second.rows)
    assert entry.ranked_names(now=now[0]) == ()
    assert entry.reason == "fresh"
    assert len(calls) == 2
    assert alarms == []
