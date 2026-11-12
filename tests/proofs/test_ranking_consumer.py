"""ADR-0003 fixtures for the entry-ranking consumer and its two hosts, not a trial."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import DataActor
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model import (
    AggressorSide,
    CustomData,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)
from test_ranking import NS, START, publisher, registry

from sextant.ranking.consumer import (
    RankingConsumer,
    RankingDataActor,
    RankingDataActorConfig,
    RawBusHost,
)
from sextant.ranking.generation import GenerationId, generation_digest
from sextant.ranking.publisher import ranking_data_type

ROOT = Path(__file__).parents[2]
CONSUMER = ROOT / "src/sextant/ranking/consumer.py"
AGES = (
    "entry_heartbeat_max_age_s",
    "entry_generation_max_age_s",
    "entry_input_max_age_s",
)


def tick(at: int, price: str = "0.50", name: str = "PENNY") -> TradeTick:
    # 100 shares per print reaches the fixture dvol gate; ts_init == ts_event keeps the
    # published generation's clocks equal to the engine dispatch clock in the actor proof.
    return TradeTick(
        InstrumentId.from_str(name + ".NASDAQ"),
        Price.from_str(price),
        Quantity.from_str("100"),
        AggressorSide.BUY,
        TradeId(str(at)),
        at,
        at,
    )


def warm(pub) -> None:
    for second in (0, 60, 120, 180, 240, 300):
        pub.on_trade(tick(START + second * NS))
    for second in (310, 320, 330, 340, 350, 360):
        pub.on_trade(tick(START + second * NS, "0.55"))


def consumer(
    pub,
    *,
    heartbeat: int = 2,
    generation: int = 2,
    inputs: int = 2,
    alarm: Any = None,
) -> RankingConsumer:
    return RankingConsumer(
        registry=registry(),
        feed="sip",
        segment=pub.segment,
        universe_hash=pub.universe_hash,
        baseline_hash=pub.baseline.identity,
        entry_heartbeat_max_age_s=heartbeat,
        entry_generation_max_age_s=generation,
        entry_input_max_age_s=inputs,
        alarm=alarm,
    )


def recorder():
    calls: list[tuple[str, str | None]] = []
    return calls, lambda reason, symbol=None: calls.append((reason, symbol))


def published(pub, *, at: int):
    warm(pub)
    generation = pub.publish(now=at)
    assert generation is not None
    return generation


def test_raw_bus_host_delivers_a_full_generation_to_the_entry_reader() -> None:
    pub = publisher()
    at = START + 360 * NS
    entry = consumer(pub)
    host = RawBusHost(entry, clock=lambda: at)
    host.subscribe(pub.bus)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    generation = published(pub, at=at)
    assert entry.reader.generation_key == generation_digest(generation)
    assert entry.entry_rank("PENNY", now=at) == 1
    assert entry.ranked_names(now=at) == ("PENNY",)


def test_native_data_actor_reaches_the_same_reader_in_a_backtest_node() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub)
    actor = RankingDataActor(RankingDataActorConfig(consumer=entry))
    assert isinstance(actor, DataActor)
    assert not hasattr(actor, "submit_order")
    engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True))
    engine.add_actor(actor)
    try:
        engine.add_data([CustomData(ranking_data_type("sip"), generation)])
        engine.run()
        assert entry.reader.generation_key == generation_digest(generation)
        assert entry.accept_heartbeat(at=at, received_ns=at)
        assert entry.entry_rank("PENNY", now=at) == 1
        assert entry.ranked_names(now=at) == ("PENNY",)
    finally:
        engine.dispose()


def test_no_generation_yields_no_names_and_a_reason() -> None:
    entry = consumer(publisher())
    at = START + 360 * NS
    assert entry.accept_heartbeat(at=at, received_ns=at)
    assert entry.ranked_names(now=at) == ()
    assert entry.reason == "generation_missing"


def test_missing_heartbeat_yields_no_names() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.ranked_names(now=at) == ()
    assert entry.reason == "heartbeat_missing"
    assert entry.accept_heartbeat(at=at, received_ns=at)
    assert entry.ranked_names(now=at) == ("PENNY",)


def test_entry_heartbeat_age_binds_before_the_reader_age() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub, heartbeat=1)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + 2 * NS
    assert entry.reader.fresh(later)
    assert entry.ranked_names(now=later) == ()
    assert entry.reason == "entry_heartbeat_stale"


def test_entry_generation_age_binds_before_the_reader_age() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub, generation=1)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + NS + NS // 2
    assert entry.reader.fresh(later)
    assert entry.ranked_names(now=later) == ()
    assert entry.reason == "entry_generation_stale"


def test_entry_input_age_binds_before_the_reader_age() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub, inputs=1)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + NS + NS // 2
    assert entry.reader.fresh(later)
    assert entry.ranked_names(now=later) == ()
    assert entry.reason == "entry_input_stale"


def test_a_stale_candidate_input_refuses_a_rank_and_a_name() -> None:
    # Reviewer's witness: PENNY's own features stop at 360 s while another symbol keeps the
    # generation's latest event current, so only the per-candidate entry age can refuse it.
    pub = publisher()
    warm(pub)
    now = START + 361 * NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    generation = pub.publish(now=now)
    assert generation is not None
    calls, alarm = recorder()
    entry = consumer(pub, inputs=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    assert entry.reader.fresh(now)
    assert entry.entry_rank("PENNY", now=now) is None
    assert entry.reason == "symbol_input_stale"
    assert entry.ranked_names(now=now) == ()
    assert entry.reason == "symbol_input_stale"
    assert calls == [("symbol_input_stale", "PENNY")]


def test_a_stale_entry_generation_calls_the_alarm() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    calls, alarm = recorder()
    entry = consumer(pub, generation=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + NS + NS // 2
    assert entry.reader.fresh(later)
    assert entry.ranked_names(now=later) == ()
    assert entry.reason == "entry_generation_stale"
    assert calls == [("entry_generation_stale", None)]


def test_a_stale_entry_heartbeat_calls_the_alarm() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    calls, alarm = recorder()
    entry = consumer(pub, heartbeat=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + 2 * NS
    assert entry.reader.fresh(later)
    assert entry.ranked_names(now=later) == ()
    assert entry.reason == "entry_heartbeat_stale"
    assert calls == [("entry_heartbeat_stale", None)]


def test_a_stale_entry_input_calls_the_alarm() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    calls, alarm = recorder()
    entry = consumer(pub, inputs=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + NS + NS // 2
    assert entry.reader.fresh(later)
    assert entry.ranked_names(now=later) == ()
    assert entry.reason == "entry_input_stale"
    assert calls == [("entry_input_stale", None)]


def test_a_missing_heartbeat_calls_the_alarm() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    calls, alarm = recorder()
    entry = consumer(pub, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.ranked_names(now=at) == ()
    assert entry.reason == "heartbeat_missing"
    assert calls == [("heartbeat_missing", None)]


def test_a_persistent_entry_refusal_alarms_once_until_the_generation_changes() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    calls, alarm = recorder()
    entry = consumer(pub, generation=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    later = at + NS + NS // 2
    assert entry.reader.fresh(later)
    for _ in range(3):
        assert entry.ranked_names(now=later) == ()
    assert calls == [("entry_generation_stale", None)]
    second = replace(generation, generation_id=GenerationId("fixture-1", 1, 0))
    assert entry.handle(CustomData(ranking_data_type("sip"), second), now=later)
    assert entry.accept_heartbeat(at=later, received_ns=later)
    assert entry.ranked_names(now=later) == ()
    assert calls == [("entry_generation_stale", None), ("entry_generation_stale", None)]


def test_an_alarm_exception_keeps_the_veto_and_records_the_failure() -> None:
    pub = publisher()
    warm(pub)
    now = START + 361 * NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    generation = pub.publish(now=now)
    assert generation is not None

    def refusing_alarm(reason: str, symbol: str | None = None) -> None:
        raise RuntimeError("alarm transport down")

    entry = consumer(pub, inputs=1, alarm=refusing_alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    assert entry.entry_rank("PENNY", now=now) is None
    assert entry.reason == "symbol_input_stale"
    assert entry.ranked_names(now=now) == ()
    assert entry.reason == "symbol_input_stale"
    assert entry.alarm_errors == 1
    assert entry.last_alarm_error is not None and "RuntimeError" in entry.last_alarm_error


def test_accepting_a_heartbeat_clears_only_the_heartbeat_refusal() -> None:
    pub = publisher()
    warm(pub)
    now = START + 361 * NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    generation = pub.publish(now=now)
    assert generation is not None
    calls, alarm = recorder()
    entry = consumer(pub, inputs=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    assert entry.entry_rank("PENNY", now=now) is None
    assert calls == [("symbol_input_stale", "PENNY")]
    assert entry.accept_heartbeat(at=now, received_ns=now)
    entry.ranked_names(now=now)
    assert calls == [("symbol_input_stale", "PENNY")]


def test_simultaneous_refusals_are_remembered_independently() -> None:
    pub = publisher()
    for name in ("PENNY", "LARGE"):
        for second in (0, 60, 120, 180, 240, 300):
            pub.on_trade(tick(START + second * NS, name=name))
        for second in (310, 320, 330, 340, 350, 360):
            pub.on_trade(tick(START + second * NS, "0.55", name))
    now = START + 361 * NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    generation = pub.publish(now=now)
    assert generation is not None
    calls, alarm = recorder()
    entry = consumer(pub, inputs=1, alarm=alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    for _ in range(3):
        assert entry.ranked_names(now=now) == ()
    assert sorted(calls) == [("symbol_input_stale", "LARGE"), ("symbol_input_stale", "PENNY")]


def test_an_unprintable_alarm_exception_is_still_contained() -> None:
    pub = publisher()
    warm(pub)
    now = START + 361 * NS + NS // 2
    pub.on_trade(tick(now, name="ADR"))
    generation = pub.publish(now=now)
    assert generation is not None

    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    def refusing_alarm(reason: str, symbol: str | None = None) -> None:
        raise Unprintable()

    entry = consumer(pub, inputs=1, alarm=refusing_alarm)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=now)
    assert entry.accept_heartbeat(at=now, received_ns=now)
    assert entry.entry_rank("PENNY", now=now) is None
    assert entry.reason == "symbol_input_stale"
    assert entry.alarm_errors == 1
    assert isinstance(entry.last_alarm_error, str)
    assert entry.ranked_names(now=now) == ()


def test_a_mismatched_generation_topic_clears_names() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.ranked_names(now=at) == ("PENNY",)
    assert not entry.handle(CustomData(ranking_data_type("boats"), generation), now=at)
    assert entry.reason == "wrong generation topic"
    assert entry.ranked_names(now=at) == ()


def test_a_sequence_discontinuity_clears_names() -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    entry = consumer(pub)
    assert entry.accept_heartbeat(at=at, received_ns=at)
    assert entry.handle(CustomData(ranking_data_type("sip"), generation), now=at)
    assert entry.ranked_names(now=at) == ("PENNY",)
    gap = replace(generation, generation_id=GenerationId("fixture-1", 3, 0))
    assert not entry.handle(CustomData(ranking_data_type("sip"), gap), now=at)
    assert entry.reason == "generation sequence discontinuity"
    assert entry.ranked_names(now=at) == ()


def ages(pub) -> dict[str, Any]:
    return {
        "registry": registry(),
        "feed": "sip",
        "segment": pub.segment,
        "universe_hash": pub.universe_hash,
        "baseline_hash": pub.baseline.identity,
        "entry_heartbeat_max_age_s": 2,
        "entry_generation_max_age_s": 2,
        "entry_input_max_age_s": 2,
    }


@pytest.mark.parametrize("missing", AGES)
def test_every_entry_age_is_required(missing: str) -> None:
    kwargs = ages(publisher())
    kwargs.pop(missing)
    with pytest.raises(TypeError):
        RankingConsumer(**kwargs)


@pytest.mark.parametrize("name", AGES)
@pytest.mark.parametrize("bad", (0, -1, True, 1.0))
def test_every_entry_age_rejects_non_positive_or_non_int(name: str, bad: object) -> None:
    kwargs = ages(publisher())
    kwargs[name] = bad
    with pytest.raises(ValueError):
        RankingConsumer(**kwargs)


def test_the_consumer_cannot_reach_order_broker_or_account_surfaces() -> None:
    source = CONSUMER.read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").lower())
            imported.update(alias.name.lower() for alias in node.names)
    forbidden = ("order", "execution", "broker", "account", "strategy", "writer")
    assert not [name for name in imported if any(word in name for word in forbidden)]
    assert "k2-ranking" not in source and "lanes/tradingview" not in source
    for surface in (RankingConsumer, RawBusHost, RankingDataActor):
        assert not hasattr(surface, "submit_order")
        assert not hasattr(surface, "cancel_order")


def test_the_actor_config_refuses_an_unknown_key_and_passes_an_engine_field() -> None:
    entry = consumer(publisher())
    with pytest.raises(TypeError, match="consumers"):
        RankingDataActorConfig(consumer=entry, consumers=entry)  # a misspelling, never swallowed
    config = RankingDataActorConfig(consumer=entry, log_events=False)
    assert config.log_events is False and config.consumer is entry
