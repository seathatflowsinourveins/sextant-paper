"""N2b-1 (LEAD-0033 2b): the node's thin adapter over RankingPublisher, and PR 427's witnesses.

``Node`` stands in for the RankingActor's node bus: every generation the adapter returns
reaches one ``RankingConsumer`` at once, and a heartbeat follows it through main's own
``HeartbeatEmitter``, the order DESIGN-N2B1 section 1 settles. Fixture values exercise
mechanisms only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal as D
from typing import Any

import pytest
from nautilus_trader.common import MessageBus
from nautilus_trader.model import (
    AggressorSide,
    CustomData,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TraderId,
    TradeTick,
)
from test_ranking import NS, SCOPE, START, registry

from sextant.ranking.baseline import BaselineSample, BaselineTable
from sextant.ranking.consumer import RankingConsumer
from sextant.ranking.engine import ReferencePrices
from sextant.ranking.generation import Coverage, CoverageState, RankingGeneration
from sextant.ranking.heartbeat import HeartbeatEmitter
from sextant.ranking.node_host import NodeRankingHost
from sextant.ranking.publisher import (
    AssetState,
    RankingPublisher,
    ranking_data_type,
    universe_digest,
)

NAMES = ("ADR", "LARGE", "PENNY")
REFS = ReferencePrices(D("0.50"), START - NS, "fixture official close")
GOOD = AssetState(START - NS, SCOPE.end_ns, False, "common", "fixture vendor")
AT = START + 360 * NS
REGULAR = (True, ("@",))  # a regular print's vendor facts


def ready(n: int) -> Coverage:
    return Coverage(CoverageState.READY, True, n, n, 0, 0, 0)


def curves(names: tuple[str, ...]) -> BaselineTable:
    return BaselineTable(
        tuple(
            BaselineSample(name, "PRE", "2026-10-26", offset, D(dollars), D(count))
            for name in names
            for offset, dollars, count in ((0, 0, 0), (600, 100, 100))
        ),
        "fixture explicit complete curves; linear within minute",
        START - NS,
        "sip",
    )


def trade(at: int, name: str, price: str) -> TradeTick:
    return TradeTick(
        InstrumentId.from_str(name + ".ALPACA"),
        Price.from_str(price),
        Quantity.from_str("100"),
        AggressorSide.BUY,
        TradeId(f"{name}-{at}"),
        at,
        at,
    )


class Node:
    def __init__(self, names=NAMES, assets: dict[str, AssetState | None] | None = None) -> None:
        self.names, self.now = names, START
        table = curves(names)
        self.publisher = RankingPublisher(
            bus=MessageBus(TraderId.from_str("RANKING-001")),
            epoch="fixture-node-1",
            feed="sip",
            segment=SCOPE,
            registry=registry(),
            universe=names,
            universe_source="fixture selected symbols; NOT PIT universe",
            baseline=table,
        )
        self.host = NodeRankingHost(self.publisher, clock=lambda: self.now)
        self.consumer = RankingConsumer(
            registry=registry(),
            feed="sip",
            segment=SCOPE,
            universe_hash=universe_digest(names),
            baseline_hash=table.identity,
            entry_heartbeat_max_age_s=2,
            entry_generation_max_age_s=2,
            entry_input_max_age_s=2,
        )
        self.emitter = HeartbeatEmitter(
            registry=registry(), clock=lambda: self.now, sink=self.consumer.accept_heartbeat
        )
        self.published: list[RankingGeneration] = []
        self.deliver(self.host.set_coverage(ready(len(names))))
        for name in sorted(names):
            asset = (assets or {}).get(name, GOOD)
            self.deliver(self.host.set_symbol(name, references=REFS, asset=asset))

    def deliver(self, generation: RankingGeneration | None) -> RankingGeneration | None:
        if generation is not None:  # the Actor's publish_data, then the heartbeat
            if self.published:  # review F3: every adapter generation advances the lineage
                last = self.published[-1].generation_id
                assert generation.generation_id.producer_epoch == last.producer_epoch
                assert generation.generation_id.sequence > last.sequence
            self.published.append(generation)
            data_type = ranking_data_type("sip")
            self.consumer.handle(CustomData(data_type, generation), now=self.now)
            self.emitter.emit(generation)
        return generation

    def tick(
        self,
        second: int,
        name: str,
        price: str = "0.50",
        *,
        now_s: int | None = None,
        facts: tuple[bool, tuple[str, ...]] | None = REGULAR,
    ):
        self.now = START + (second if now_s is None else now_s) * NS
        tick = trade(START + second * NS, name, price)
        return self.deliver(self.host.on_trade(tick, facts=facts))

    def warm(self, movers=("PENNY",), quiet=()) -> None:
        names = [name for name in self.names if name not in quiet]
        for second in (0, 60, 120, 180, 240, 300):
            for name in names:
                self.tick(second, name)
        for second in (310, 320, 330, 340, 350):
            for name in names:
                self.tick(second, name, "0.55" if name in movers else "0.50")
        # A mover needs a full prior window (360 s); the last trade publishes alone, a second
        # after the others, so every name's input is fresh in it.
        for name in names:
            if name not in movers:
                self.tick(359, name)
        *early, last = sorted(movers, key=lambda name: name == "PENNY")
        for name in early:
            self.tick(360, name, "0.55")
        self.tick(360 + bool(early), last, "0.55")

    def rank(self, name: str, at: int | None = None) -> int | None:
        return self.consumer.entry_rank(name, now=self.now if at is None else at)


def withdrawn(node: Node, at: int | None = None) -> None:
    at = node.now if at is None else at
    ranks = {name: node.rank(name, at) for name in node.names}
    assert all(rank is None for rank in ranks.values()), ranks
    assert node.consumer.ranked_names(now=at) == ()


# --- PR 427's host proofs that still apply ------------------------------------------------


def test_full_universe_fixture_reaches_ready_and_entry_rank_ranks() -> None:
    node = Node()
    node.warm()
    assert node.published[-1].coverage.state is CoverageState.READY
    assert node.rank("PENNY") == 1 and node.consumer.ranked_names(now=node.now) == ("PENNY",)


def test_a_stale_input_degrades_and_every_entry_rank_is_none() -> None:
    node = Node()
    node.warm()
    generation = node.tick(361, "PENNY", "0.56", now_s=370)  # received 9 s after it traded
    assert generation is not None and generation.coverage.state is CoverageState.DEGRADED
    withdrawn(node)


def asset_setter(asset: AssetState | None) -> Callable[[Node], Any]:
    return lambda node: node.host.set_symbol("PENNY", references=REFS, asset=asset)


def coverage_setter(coverage: Coverage) -> Callable[[Node], Any]:
    return lambda node: node.host.set_coverage(coverage)


VETOES: dict[str, Callable[[Node], Any]] = {
    f"coverage_{state.name.lower()}": coverage_setter(replace(ready(3), state=state))
    for state in CoverageState
    if state is not CoverageState.READY
}
VETOES["coverage_incomplete_with_good_counts"] = coverage_setter(replace(ready(3), complete=False))
VETOES["coverage_all_counts_unknown"] = coverage_setter(Coverage(CoverageState.READY, True))
for field in (
    "expected_symbols",
    "observed_symbols",
    "missing_symbols",
    "late_symbols",
    "invalid_symbols",
):
    VETOES[f"coverage_{field}_null"] = coverage_setter(replace(ready(3), **{field: None}))
    wrong = 2 if field in {"expected_symbols", "observed_symbols"} else 1
    VETOES[f"coverage_{field}_wrong"] = coverage_setter(replace(ready(3), **{field: wrong}))
VETOES["asset_none"] = asset_setter(None)
for veto_name, veto_asset in {
    "halted": replace(GOOD, halted=True),
    "halt_unknown": replace(GOOD, halted=None),
    "type_ineligible": replace(GOOD, security_type="etf"),
    "type_missing": replace(GOOD, security_type=""),
    "expired": replace(GOOD, expires_at=AT),
    "future": replace(GOOD, known_at=SCOPE.end_ns),
    "negative_known_at": replace(GOOD, known_at=-1),
    "negative_expiry": replace(GOOD, expires_at=-1),
    "no_source": replace(GOOD, source=""),
}.items():
    VETOES[f"asset_{veto_name}"] = asset_setter(veto_asset)
TIMES = ("before_warm", "after_warm", "between_read_and_entry")


def raise_once(node: Node) -> None:
    real, calls = node.publisher.publish, []

    def publish(*, now: int):
        calls.append(now)
        if len(calls) == 1:
            raise ValueError("fixture publication unavailable")
        return real(now=now)

    node.publisher.publish = publish  # type: ignore[method-assign]


@pytest.mark.parametrize("when", TIMES)
@pytest.mark.parametrize("veto", sorted(VETOES))
def test_veto_publication_error_is_fail_closed(veto: str, when: str) -> None:
    node = Node()
    if when != "before_warm":
        node.warm()
        if when == "between_read_and_entry":
            assert node.rank("PENNY") == 1
    raise_once(node)
    node.deliver(VETOES[veto](node))
    if when == "before_warm":
        node.warm()
    assert node.host.publish_errors == 1
    withdrawn(node, AT)


def test_raising_veto_leaves_no_readable_rank() -> None:  # review witness (b)
    node = Node()
    node.warm()
    bad = replace(GOOD, known_at=None, halted=True)  # type: ignore[arg-type]
    generation = node.deliver(node.host.set_symbol("PENNY", references=REFS, asset=bad))
    assert generation is not None and node.host.malformed_assets == 1
    assert node.publisher.assets["PENNY"] is None  # stored as unknown, never as raising
    withdrawn(node)


def test_reference_veto_renews_and_withdraws_at_once() -> None:
    node = Node()
    node.warm()
    beats = node.emitter._last_at
    unknown = ReferencePrices(None, START - NS, REFS.source)
    generation = node.deliver(node.host.set_symbol("PENNY", references=unknown, asset=GOOD))
    assert generation is not None and node.publisher.states["PENNY"].latest is None
    withdrawn(node)
    assert node.emitter._last_at == beats  # no new heartbeat for the withdrawing generation


def test_missing_reference_publication_regression() -> None:
    node = Node()
    node.warm()
    unknown = ReferencePrices(None, START - NS, REFS.source)
    node.now = AT + NS
    node.deliver(node.host.set_symbol("PENNY", references=unknown, asset=GOOD))
    node.tick(361, "PENNY", "0.56")
    latest = node.publisher.states["PENNY"].latest
    assert latest is not None and latest.score is None
    withdrawn(node)


# --- The three review witnesses -----------------------------------------------------------


@pytest.mark.parametrize("veto", sorted(VETOES))
def test_a_veto_set_after_warm_up_withdraws_the_rank_at_once(veto: str) -> None:  # (a)
    node = Node()
    node.warm()
    assert node.rank("PENNY", AT) == 1 and node.publisher.published_at == AT
    generation = node.deliver(VETOES[veto](node))  # inside the publish interval
    assert generation is not None and generation.known_at == AT
    withdrawn(node, AT)


def test_a_raising_publish_sends_one_veto_and_recovers() -> None:  # (b)
    node = Node()
    node.warm()
    raise_once(node)
    generation = node.tick(361, "PENNY", "0.56")
    assert generation is not None and generation.coverage.state is CoverageState.DEGRADED
    withdrawn(node)
    for second in (362, 363, 364):  # the reader re-anchors its sequence, then accepts
        node.tick(second, "PENNY", "0.56")
    assert node.published[-1].coverage.state is CoverageState.READY
    assert node.rank("PENNY") == 1


def test_when_the_veto_also_raises_the_actor_faults_and_the_rank_ages_out() -> None:  # (b)
    node = Node()
    node.warm()

    def broken(*, now: int):
        raise ValueError("fixture publication unavailable")

    node.publisher.publish = broken  # type: ignore[method-assign]
    node.now = AT + NS
    with pytest.raises(ValueError, match="fixture publication"):
        node.host.on_trade(trade(AT + NS, "PENNY", "0.56"), facts=REGULAR)
    assert node.host.publish_errors == 1
    withdrawn(node, AT + 3 * NS)  # past entry_generation_max_age_s


def test_one_ingress_error_does_not_leave_coverage_degraded_for_ever() -> None:  # (c)
    node = Node()
    node.warm()
    node.tick(340, "LARGE", now_s=361)  # LARGE last traded at 350: a regressed receipt
    assert node.host.ingress_errors == 1 and node.host.tainted == {"LARGE"}
    assert node.publisher.coverage.state is CoverageState.READY
    for second in (362, 363, 364):
        node.tick(second, "PENNY", "0.56")
    assert node.published[-1].coverage.state is CoverageState.READY
    assert node.rank("PENNY") == 1


# --- Ruling 2b's rules ----------------------------------------------------------------------


def test_a_raising_tick_taints_only_that_name_for_the_rest_of_the_segment() -> None:
    node = Node()
    node.warm(movers=("ADR", "PENNY"))
    assert node.rank("ADR") == 1 and node.rank("PENNY") == 2
    generation = node.tick(340, "PENNY", now_s=361)  # forced: the taint withdraws at once
    assert generation is not None
    ranks = {row.symbol: row.rank for row in generation.rows}
    assert ranks["PENNY"] is None and ranks["ADR"] == 1
    for second in (362, 363, 364):
        node.tick(second, "PENNY", "0.60")
        node.tick(second, "ADR", "0.56")
    state = node.publisher.states["PENNY"]
    assert state.segment_covered is False and state.latest is not None
    assert state.latest.score is None and node.rank("PENNY") is None
    assert node.rank("ADR") == 1


def test_a_trade_outside_the_universe_is_counted_and_publishes_nothing() -> None:
    node = Node()
    node.warm()
    count = len(node.published)
    assert node.tick(361, "OTHER", "9.99") is None and node.host.ignored_trades == 1
    assert len(node.published) == count and node.rank("PENNY", AT) == 1


def test_priming_publishes_once_and_ordinary_setters_follow_the_throttle() -> None:
    node = Node()
    assert len(node.published) == 1  # the first publish; the N priming setters are throttled
    node.warm()
    count = len(node.published)
    same = node.host.set_symbol("ADR", references=REFS, asset=GOOD)  # withdraws nothing
    assert same is None and node.host.set_coverage(ready(3)) is None
    assert len(node.published) == count


def test_the_callers_coverage_is_held_apart_and_never_written_back() -> None:
    node = Node()
    node.warm()
    node.tick(340, "LARGE", now_s=361)  # the publisher itself writes DEGRADED here
    supplied = node.host.supplied
    assert supplied == ready(3)
    node.deliver(node.host.set_coverage(replace(ready(3), state=CoverageState.DEGRADED)))
    node.tick(362, "PENNY", "0.56")
    assert node.published[-1].coverage.state is CoverageState.DEGRADED  # the caller's word
    node.deliver(node.host.set_coverage(ready(3)))
    for second in (363, 364, 365):
        node.tick(second, "PENNY", "0.56")
    assert node.rank("PENNY") == 1


def test_a_publisher_with_its_own_heartbeat_is_refused() -> None:
    node = Node()
    node.publisher.heartbeat = node.emitter
    with pytest.raises(ValueError, match="heartbeat"):
        NodeRankingHost(node.publisher, clock=lambda: START)


# --- A broad universe -----------------------------------------------------------------------


def test_one_halted_and_one_untraded_name_do_not_withhold_the_others() -> None:
    names = ("ADR", "FLAT", "HALT", "LARGE", "PENNY", "QUIET")
    node = Node(names, assets={"HALT": replace(GOOD, halted=True)})
    node.warm(movers=("ADR", "HALT", "PENNY"), quiet=("QUIET",))
    assert node.published[-1].coverage.state is CoverageState.READY
    assert node.rank("ADR") is not None and node.rank("PENNY") is not None
    assert node.rank("HALT") is None and node.rank("QUIET") is None
    assert set(node.consumer.ranked_names(now=node.now)) == {"ADR", "PENNY"}


# --- Behind the RankingActor on a real BacktestNode -----------------------------------------


def test_the_adapter_drops_into_the_ranking_actor_on_a_backtest_node(tmp_path) -> None:
    from test_ranking import baseline
    from test_ranking_actor import Logged, run

    clocks: list[Any] = []

    class Clocked(Logged):
        def on_start(self) -> None:
            clocks.append(self.clock)  # the factory runs inside on_start, on the node clock
            super().on_start()

    def build() -> NodeRankingHost:
        publisher = RankingPublisher(
            bus=MessageBus(TraderId.from_str("RANKING-001")),
            epoch="fixture-node-1",
            feed="sip",
            segment=SCOPE,
            registry=registry(),
            universe=NAMES,
            universe_source="fixture selected symbols; NOT PIT universe",
            baseline=baseline(),
        )
        return NodeRankingHost(publisher, clock=clocks[0].timestamp_ns)

    out = run(tmp_path, factory=build, actor_cls=Clocked, facts=lambda _tick: (True, ()))
    assert out["actor"].errors == {}
    last = out["receiver"].received[-1]  # only PENNY trades; ADR and LARGE never do
    assert last.coverage.state is CoverageState.READY
    assert out["consumer"].reader.generation is last
    (penny,) = [row for row in last.rows if row.symbol == "PENNY"]
    assert penny.rank == 1 and penny.focus_eligible is True


# --- Review round 1 F1-F3, checked on the adapter -----------------------------------------


def test_every_generation_the_adapter_returns_advances_its_lineage() -> None:  # F3
    node = Node()
    node.warm()
    raise_once(node)  # a failed publish and its veto, then recovery
    for second in (361, 362, 363):
        node.tick(second, "PENNY", "0.56")
    node.deliver(node.host.set_coverage(replace(ready(3), state=CoverageState.DEGRADED)))
    sequences = [g.generation_id.sequence for g in node.published]
    assert sequences == sorted(set(sequences)) and len(sequences) > 10


def test_a_setter_the_publisher_refuses_stores_nothing_and_raises_to_the_actor() -> None:  # F2
    node = Node()
    node.warm()
    with pytest.raises(ValueError, match="outside supplied universe"):
        node.host.set_symbol("OTHER", references=REFS, asset=GOOD)
    assert "OTHER" not in node.publisher.assets  # nothing half-applied for the Actor to trust


def test_the_taint_holds_after_a_late_print_at_the_segment_start() -> None:  # review G2
    node = Node()
    future = replace(REFS, known_at=START + NS)  # the first print raises: a future reference
    node.deliver(node.host.set_symbol("PENNY", references=future, asset=GOOD))
    node.tick(0, "PENNY")
    assert node.host.tainted == {"PENNY"}
    first = trade(START, "PENNY", "0.50")
    late = TradeTick(  # the segment's first print again, received a second later
        first.instrument_id,
        first.price,
        first.size,
        first.aggressor_side,
        first.trade_id,
        START,
        START + NS,
    )
    node.now = START + NS
    node.deliver(node.host.on_trade(late, facts=REGULAR))
    for second in (60, 120, 180, 240, 300):
        node.tick(second, "PENNY")
    for second in (310, 320, 330, 340, 350, 360):
        node.tick(second, "PENNY", "0.55")
    state = node.publisher.states["PENNY"]
    assert state.segment_covered is False and node.rank("PENNY") is None


# --- N1e: the print's facts reach the publisher through the one mask ---------------------


@pytest.mark.parametrize(
    "facts",
    [(True, ("Z",)), (True, ("6",)), (False, ("@",)), None],  # masked, closing, vendor, unknown
)
def test_a_print_the_mask_excludes_or_unknown_facts_never_move_the_rank(facts) -> None:
    node = Node()
    node.warm()
    before = node.publisher.states["PENNY"]
    node.tick(361, "PENNY", "0.90", facts=facts)
    assert node.publisher.states["PENNY"].latest == before.latest  # neither price nor volume
    assert node.publisher.last_event == AT


def test_a_regular_print_moves_the_rank_with_its_conditions() -> None:
    node = Node()
    node.warm()
    node.tick(361, "PENNY", "0.56", facts=(True, ("@", "I")))
    latest = node.publisher.states["PENNY"].latest
    assert latest is not None and latest.known_at == START + 361 * NS
