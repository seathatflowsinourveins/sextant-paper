"""Gate review of #531 (P1, static): a stream gap must not publish a READY rank before DEGRADED.

A real BacktestNode replays two qualifying names (PENNY and LARGE) into the RankingActor over
the node adapter (``NodeRankingHost``) with N1e's ``StreamFacts``; each print's facts record
reaches the Actor through ``on_data`` before the print, as the data client publishes it. At
362 s the gap notice arrives through ``on_data``. Every generation the Actor publishes from the
gap until the DEGRADED one is captured.
"""

from __future__ import annotations

from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from nautilus_trader.backtest import BacktestNode
from nautilus_trader.common import MessageBus, SocketState, SocketStateChanged
from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
    DataCatalogConfig,
)
from nautilus_trader.core import UUID4
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    AggressorSide,
    ClientId,
    CustomData,
    InstrumentId,
    NautilusDataType,
    OmsType,
    Price,
    Quantity,
    TradeId,
    TraderId,
    TradeTick,
)
from nautilus_trader.persistence import ParquetDataCatalog
from test_ranking import NS, SCOPE, START, baseline, registry
from test_ranking_actor import IDS, NAMES, SECONDS, Receiver, equity, priming

from sextant.alpaca.stream_facts import AlpacaStreamGap, AlpacaTradeFacts, StreamFacts
from sextant.ranking.consumer import RankingConsumer, RankingDataActorConfig
from sextant.ranking.generation import CoverageState
from sextant.ranking.node_actor import RankingActor, RankingActorConfig
from sextant.ranking.node_host import NodeRankingHost
from sextant.ranking.publisher import RankingPublisher, universe_digest

MOVERS = ("LARGE", "PENNY")
GAP_AT = START + 362 * NS  # inside input_max_age_s (2 s) of the last print at 361 s


def prints() -> list[TradeTick]:
    out = []
    for name in MOVERS:
        iid = InstrumentId.from_str(f"{name}.ALPACA")
        for second in SECONDS:  # PENNY's last print publishes alone, a second later
            at = START + (second + (name == "PENNY" and second == 360)) * NS
            out.append(
                TradeTick(
                    iid,
                    Price.from_str("0.50" if second < 310 else "0.55"),
                    Quantity.from_str("100"),
                    AggressorSide.BUY,
                    TradeId(f"{name}-{second}"),
                    at,
                    at,
                )
            )
    return sorted(out, key=lambda tick: tick.ts_event)


def run(tmp_path, *, gap: bool = True, sockets: tuple[tuple[int, Any], ...] = ()) -> dict[str, Any]:
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_instruments([equity(iid) for iid in IDS])
    for name in MOVERS:  # the catalog writes one instrument per chunk
        catalog.write_trade_ticks([t for t in prints() if t.instrument_id.symbol.value == name])
    config = BacktestRunConfig(
        venues=[
            BacktestVenueConfig(
                name="ALPACA",
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                starting_balances=["100000 USD"],
                fee_model=MakerTakerFeeModel(Decimal(0), Decimal(0)),
            )
        ],
        data=[
            BacktestDataConfig(
                data_type=NautilusDataType.TradeTick,
                catalog=DataCatalogConfig(path=str(tmp_path)),
                instrument_ids=[InstrumentId.from_str(f"{name}.ALPACA") for name in MOVERS],
            )
        ],
        engine=BacktestEngineConfig(bypass_logging=True, run_analysis=False),
        raise_exception=True,
        dispose_on_completion=False,
        start=START - NS,
        end=START + 400 * NS,
    )
    clocks: list[Any] = []
    published: list[Any] = []

    def build() -> NodeRankingHost:
        publisher = RankingPublisher(
            bus=MessageBus(TraderId.from_str("RANKING-001")),
            epoch="fixture-gap-order",
            feed="sip",
            segment=SCOPE,
            registry=registry(),
            universe=NAMES,
            universe_source="fixture selected symbols; NOT PIT universe",
            baseline=baseline(),
        )
        return NodeRankingHost(publisher, clock=clocks[0].timestamp_ns)

    facts = StreamFacts("sip")

    class Streamed(RankingActor):
        def on_start(self) -> None:
            clocks.append(self.clock)
            super().on_start()
            if gap:
                self.clock.set_time_alert_ns("fixture-gap", GAP_AT, self.deliver_gap)
            for at, state in sockets:  # what a LiveNode relays from a network client
                alert = partial(self.deliver_socket, state)
                self.clock.set_time_alert_ns(f"fixture-socket-{at}", at, alert)

        def deliver_socket(self, state, _event) -> None:
            now = self.clock.timestamp_ns()
            change = SocketStateChanged(
                TraderId.from_str("TRADER-001"),
                ClientId("ALPACA"),
                None,
                "fixture-endpoint",
                state,
                UUID4(),
                now,
                now,
            )
            self.on_socket_state(change)

        def deliver_gap(self, _event) -> None:
            notice = AlpacaStreamGap("sip", 2, GAP_AT, GAP_AT)
            self.on_data(CustomData(facts.gap_type, notice))

        def on_trade(self, trade: TradeTick) -> None:
            record = AlpacaTradeFacts(
                trade.instrument_id,
                trade.trade_id.value,
                ("@",),
                "C",
                trade.ts_event,
                trade.ts_init,
            )
            self.on_data(CustomData(facts.facts_type, record))
            super().on_trade(trade)

        def publish_data(self, data_type, data) -> None:
            published.append((self.clock.timestamp_ns(), data.data))
            super().publish_data(data_type, data)

    actor = Streamed(
        RankingActorConfig(
            instrument_ids=IDS,
            feed="sip",
            host_factory=build,
            priming=priming,
            page=lambda reason, detail: None,
            page_window_s=3600,
            trade_facts=facts,
        )
    )
    consumer = RankingConsumer(
        registry=registry(),
        feed="sip",
        segment=SCOPE,
        universe_hash=universe_digest(NAMES),
        baseline_hash=baseline().identity,
        entry_heartbeat_max_age_s=2,
        entry_generation_max_age_s=2,
        entry_input_max_age_s=2,
    )
    receiver = Receiver(RankingDataActorConfig(consumer=consumer))
    node = BacktestNode([config])
    try:
        node.build()
        for component in (actor, receiver):
            node.add_actor(config.id, component)
        node.run()
    finally:
        node.dispose()
    return dict(actor=actor, published=published, receiver=receiver)


def ranks(generation) -> dict[str, int | None]:
    return {row.symbol: row.rank for row in generation.rows}


def test_no_ready_rank_is_published_between_the_gap_and_its_degraded_coverage(tmp_path) -> None:
    out = run(tmp_path)
    assert out["actor"].errors == {} and out["actor"].stream_gaps == 1
    before = [g for at, g in out["published"] if at < GAP_AT]
    assert before and set(MOVERS) <= {n for n, r in ranks(before[-1]).items() if r is not None}
    after = [g for at, g in out["published"] if at >= GAP_AT]
    first_degraded = next(
        i for i, g in enumerate(after) if g.coverage.state is CoverageState.DEGRADED
    )
    leaked = [
        (g.coverage.state.name, ranks(g))
        for g in after[:first_degraded]
        if g.coverage.state is CoverageState.READY and any(ranks(g).values())
    ]
    assert leaked == [], leaked


def leaked_after(out: dict[str, Any], at: int) -> list[tuple[str, dict[str, int | None]]]:
    after = [g for when, g in out["published"] if when >= at]
    degraded = (i for i, g in enumerate(after) if g.coverage.state is CoverageState.DEGRADED)
    first = next(degraded, len(after))
    return [
        (g.coverage.state.name, ranks(g))
        for g in after[:first]
        if g.coverage.state is CoverageState.READY and any(ranks(g).values())
    ]


@pytest.mark.parametrize(
    "events",
    [
        ((GAP_AT, SocketState.DISCONNECTED),),
        ((START - NS // 2, SocketState.CONNECTED), (GAP_AT, SocketState.CONNECTED)),
        ((GAP_AT, SocketState.CONNECTED),),  # the first connect seen, after prints (#537 P1)
    ],
    ids=["disconnect", "a-later-connect", "a-first-connect-after-prints"],
)
def test_a_socket_event_that_may_have_missed_prints_follows_the_gap_rule(tmp_path, events):
    out = run(tmp_path, gap=False, sockets=events)
    assert out["actor"].errors == {} and out["actor"].resets == 1
    assert leaked_after(out, GAP_AT) == []
    assert out["published"][-1][1].coverage.state is CoverageState.DEGRADED


def test_the_runs_first_connect_is_not_a_gap(tmp_path) -> None:
    out = run(tmp_path, gap=False, sockets=((START - NS // 2, SocketState.CONNECTED),))
    assert out["actor"].errors == {} and out["actor"].resets == 0
    last = out["published"][-1][1]
    assert last.coverage.state is CoverageState.READY
    assert {name for name, rank in ranks(last).items() if rank is not None} == set(MOVERS)
