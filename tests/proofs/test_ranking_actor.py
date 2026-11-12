"""N1 (LEAD-2325 2c): the RankingActor hosts the ranking in the account's engine node.

A real BacktestNode replays published fixture trade ticks from a catalog. The host is a fake
behind the Actor's protocol, carrying the real ``RankingPublisher`` on its own MessageBus as
``RankingHost`` does (8850b79 host.py:140-148); the real host drops in at N2. Fixture values
exercise mechanisms only.
"""

from __future__ import annotations

import ast
import threading
from decimal import Decimal as D
from pathlib import Path
from typing import Any, cast

from nautilus_trader.backtest import BacktestNode
from nautilus_trader.common import DataActor, MessageBus
from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
    DataActorConfig,
    DataCatalogConfig,
)
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    AggressorSide,
    Currency,
    Equity,
    InstrumentId,
    NautilusDataType,
    OmsType,
    Price,
    Quantity,
    Symbol,
    TradeId,
    TraderId,
    TradeTick,
)
from nautilus_trader.persistence import ParquetDataCatalog
from test_ranking import NS, SCOPE, START, baseline, registry

from sextant.ranking.consumer import RankingConsumer, RankingDataActor, RankingDataActorConfig
from sextant.ranking.engine import ReferencePrices
from sextant.ranking.generation import Coverage, CoverageState, generation_digest
from sextant.ranking.node_actor import Priming, RankingActor, RankingActorConfig
from sextant.ranking.publisher import AssetState, RankingPublisher, universe_digest

ROOT = Path(__file__).parents[2]
MODULE = ROOT / "src/sextant/ranking/node_actor.py"
NAMES = ("ADR", "LARGE", "PENNY")
IDS = tuple(InstrumentId.from_str(f"{name}.ALPACA") for name in NAMES)
SECONDS = (0, 60, 120, 180, 240, 300, 310, 320, 330, 340, 350, 360)


def equity(iid: InstrumentId) -> Equity:
    return Equity(
        instrument_id=iid,
        raw_symbol=Symbol(str(iid.symbol)),
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )


def ticks() -> list[TradeTick]:
    penny = InstrumentId.from_str("PENNY.ALPACA")
    return [
        TradeTick(
            penny,
            Price.from_str("0.50" if s < 310 else "0.55"),
            Quantity.from_str("100"),
            AggressorSide.BUY,
            TradeId(str(s)),
            START + s * NS,
            START + s * NS,
        )
        for s in SECONDS
    ]


def priming(reference: str = "0.50") -> Priming:
    return Priming(
        coverage=Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0),
        symbols={
            name: (
                ReferencePrices(D(reference), START - NS, "fixture official close"),
                AssetState(START - NS, SCOPE.end_ns, False, "common", "fixture vendor"),
            )
            for name in NAMES
        },
    )


class FakeHost:
    """The host protocol over the real publisher; it refuses calls off its own thread."""

    def __init__(self, log: list[str], fail_at: int | None = None) -> None:
        self.thread, self.log, self.fail_at, self.calls = threading.get_ident(), log, fail_at, 0
        log.append("construct")
        self.publisher = RankingPublisher(
            bus=MessageBus(TraderId.from_str("RANKING-001")),
            epoch="fixture-1",
            feed="sip",
            segment=SCOPE,
            registry=registry(),
            universe=NAMES,
            universe_source="fixture selected symbols; NOT PIT universe",
            baseline=baseline(),
        )
        self.references: dict[str, D] = {}

    def _thread(self) -> None:
        if threading.get_ident() != self.thread:
            raise RuntimeError("bound to its constructing thread")

    def set_coverage(self, coverage: Coverage) -> None:
        self._thread()
        self.log.append("coverage")
        self.publisher.set_coverage(coverage)

    def set_symbol(self, name: str, *, references: ReferencePrices, asset: AssetState) -> None:
        self._thread()
        self.log.append(f"symbol:{name}")
        assert references.prior_close is not None
        self.references[name] = references.prior_close
        self.publisher.set_symbol(name, references=references, asset=asset)

    def on_trade(self, tick: TradeTick, *, facts=None):
        self._thread()
        self.calls += 1
        self.log.append("trade")
        if self.calls == self.fail_at:
            raise RuntimeError("fixture host failure")
        self.publisher.on_trade(tick)
        return self.publisher.publish(now=tick.ts_init)

    def close(self) -> None:
        self.log.append("close")


class Logged(RankingActor):
    calls: list[str] = []  # noqa: RUF012 - each run binds its own list

    def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
        self.calls.append("subscribe")
        super().subscribe_trades(instrument_id, client_id, params)


class CounterConfig(DataActorConfig):
    def __init__(self, **_kwargs: object) -> None:
        super().__init__()


class TradeCounter(DataActor):
    """A second subscriber on the same data client: it must see every tick."""

    def __init__(self, config: CounterConfig) -> None:
        super().__init__(config)
        self.seen = 0

    def on_start(self) -> None:
        self.subscribe_trades(InstrumentId.from_str("PENNY.ALPACA"))

    def on_trade(self, trade) -> None:
        self.seen += 1


class Receiver(RankingDataActor):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.received: list[Any] = []

    def on_data(self, data) -> None:
        self.received.append(data.data)
        super().on_data(data)


def run(
    tmp_path: Path,
    *,
    fail_at=None,
    prime=priming,
    factory=None,
    statuses=(),
    host_cls=None,
    actor_cls=None,
    window: int = 3600,
    extra=(),
    facts=None,
    snapshot_window=None,
    heartbeat=None,
    end=None,
    trades=None,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_instruments([*(equity(iid) for iid in IDS), *extra])
    tape = ticks() if trades is None else trades
    trade_ids = sorted({tick.instrument_id for tick in tape}, key=str)
    for iid in trade_ids:  # one instrument per chunk
        catalog.write_trade_ticks([tick for tick in tape if tick.instrument_id == iid])
    status_ids = sorted({status.instrument_id for status in statuses}, key=str)
    for iid in status_ids:  # the catalog writes one instrument per chunk
        catalog.write_instrument_statuses([s for s in statuses if s.instrument_id == iid])
    config = BacktestRunConfig(
        venues=[
            BacktestVenueConfig(
                name="ALPACA",
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                starting_balances=["100000 USD"],
                fee_model=MakerTakerFeeModel(D(0), D(0)),
            )
        ],
        data=[
            BacktestDataConfig(
                data_type=NautilusDataType.TradeTick,
                catalog=DataCatalogConfig(path=str(tmp_path)),
                instrument_id=iid,
            )
            for iid in trade_ids
        ]
        + [
            BacktestDataConfig(
                data_type=NautilusDataType.InstrumentStatus,
                catalog=DataCatalogConfig(path=str(tmp_path)),
                instrument_ids=status_ids,
            )
            for _ in statuses[:1]
        ],
        engine=BacktestEngineConfig(bypass_logging=True, run_analysis=False),
        raise_exception=True,
        dispose_on_completion=False,
        start=START - NS,
        end=end,
    )
    log: list[str] = []
    pages: list[tuple[str, str]] = []
    hosts: list[FakeHost] = []

    def build() -> FakeHost:
        hosts.append((host_cls or FakeHost)(log, fail_at))
        return hosts[-1]

    actor_config = RankingActorConfig(
        instrument_ids=IDS,
        feed="sip",
        host_factory=cast(Any, factory or build),
        priming=prime,
        page=lambda reason, detail: pages.append((reason, detail)),
        page_window_s=window,
        trade_facts=facts,
        status_snapshot_window_s=snapshot_window,
        heartbeat_registry=heartbeat,
    )
    bound = type("Bound", (actor_cls or Logged,), {"calls": log})
    actor = bound(actor_config)
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
    counter = TradeCounter(CounterConfig())
    before = list(log)
    node = BacktestNode([config])
    try:
        node.build()
        for component in (actor, receiver, counter):
            node.add_actor(config.id, component)
        node.run()
    finally:
        node.dispose()
    return dict(
        log=log,
        before=before,
        pages=pages,
        hosts=hosts,
        actor=actor,
        receiver=receiver,
        consumer=consumer,
        counter=counter,
    )


def test_the_host_is_built_on_start_and_primed_before_the_first_subscription(tmp_path) -> None:
    out = run(tmp_path)
    assert out["before"] == []  # nothing is constructed or primed with the config
    log = out["log"]
    first = log.index("subscribe")
    assert log[:first] == ["construct", "coverage", *(f"symbol:{name}" for name in NAMES)]
    assert "trade" not in log[:first] and log[-1] == "close"


def test_each_generation_reaches_a_subscriber_through_the_node_bus(tmp_path) -> None:
    out = run(tmp_path)
    received, consumer = out["receiver"].received, out["consumer"]
    assert received and all(type(g).__name__ == "RankingGeneration" for g in received)
    assert consumer.reader.generation_key == generation_digest(received[-1])
    assert out["actor"].published == len(received)


def test_a_host_exception_is_counted_paged_and_never_stops_the_feed(tmp_path) -> None:
    out = run(tmp_path, fail_at=3)
    actor, host = out["actor"], out["hosts"][0]
    assert actor.errors == {"on_trade": 1} and [r for r, _ in out["pages"]] == ["on_trade"]
    assert host.calls == len(SECONDS) and out["counter"].seen == len(SECONDS)
    assert out["receiver"].received  # later ticks still publish


def test_a_priming_failure_pages_and_subscribes_nothing(tmp_path) -> None:
    def broken() -> Priming:
        raise ValueError("fixture priming source unavailable")

    out = run(tmp_path, prime=broken)
    assert "subscribe" not in out["log"] and out["hosts"][0].calls == 0
    assert [r for r, _ in out["pages"]] == ["priming"] and out["counter"].seen == len(SECONDS)

    def no_host():
        raise OSError("fixture host refused")

    out = run(tmp_path / "factory", factory=no_host)
    assert "subscribe" not in out["log"] and [r for r, _ in out["pages"]] == ["host_factory"]


def test_the_priming_source_is_read_at_start_never_snapshotted(tmp_path) -> None:
    source = {"reference": "0.50"}
    out = run(tmp_path, prime=lambda: priming(source["reference"]))
    assert out["hosts"][0].references["PENNY"] == D("0.50")
    late = {"reference": "0.50"}

    def prime() -> Priming:
        return priming(late["reference"])

    late["reference"] = "0.40"  # changed after the config was built, before start
    out = run(tmp_path / "late", prime=prime)
    assert out["hosts"][0].references["PENNY"] == D("0.40")
    assert source  # the first run's source is untouched


def test_the_actor_opens_no_socket() -> None:
    tree = ast.parse(MODULE.read_text())
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert imports <= {
        "__future__",
        "collections",
        "dataclasses",
        "typing",
        "nautilus_trader",
        "sextant",
    }
    calls = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    # N1d observes the node's socket state (actors.md:273-345) but never drives a socket.
    assert calls.isdisjoint({"reconnect_socket", "connect", "run_forever"})


def test_an_unknown_config_key_is_refused_by_name() -> None:
    import pytest

    with pytest.raises(TypeError, match="feeed"):
        RankingActorConfig(
            instrument_ids=IDS,
            feed="sip",
            host_factory=cast(Any, lambda: FakeHost([])),
            priming=priming,
            page_window_s=3600,
            trade_facts=None,
            feeed="boats",
        )
    config = RankingActorConfig(  # the engine's own base fields still pass through
        instrument_ids=IDS,
        feed="sip",
        host_factory=cast(Any, lambda: FakeHost([])),
        priming=priming,
        page_window_s=3600,
        trade_facts=None,
        log_events=False,
    )
    assert config.log_events is False


# --- Review round 1 (Codex xhigh at ba990fd): the exact-head witnesses as proofs ----------


def test_a_failing_subscription_faults_and_the_node_keeps_running(tmp_path) -> None:  # F4
    class Unavailable(Logged):
        def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
            raise OSError("fixture subscription registration unavailable")

    out = run(tmp_path, actor_cls=Unavailable)
    assert out["actor"].errors == {"subscribe": 1} and out["pages"] == [("subscribe", "OSError")]
    assert out["counter"].seen == len(SECONDS)  # the shared node and its data client ran on


def test_a_repeated_trade_generation_is_never_published_twice(tmp_path) -> None:  # F3
    class Repeating(FakeHost):
        def __init__(self, log, fail_at=None) -> None:
            super().__init__(log, fail_at)
            self.previous = None

        def on_trade(self, tick, *, facts=None):
            generation = super().on_trade(tick)
            if self.calls == 3:
                return self.previous  # an earlier generation handed back
            self.previous = generation
            return generation

    out = run(tmp_path, host_cls=Repeating)
    sequences = [g.generation_id.sequence for g in out["receiver"].received]
    assert sequences == sorted(set(sequences))  # strictly increasing
    assert out["actor"].errors.get("lineage") == 1
    assert ("lineage", "StaleGeneration") in out["pages"]


def test_the_pager_key_set_is_bounded_by_stage(tmp_path) -> None:  # F6
    class DistinctErrors(FakeHost):
        def on_trade(self, tick, *, facts=None):
            self.calls += 1
            raise type(f"ProviderFailure{self.calls}", (RuntimeError,), {})("fixture")

    out = run(tmp_path, host_cls=DistinctErrors)
    assert out["actor"].errors == {"on_trade": len(SECONDS)}
    assert [stage for stage, _ in out["pages"]] == ["on_trade"]  # one page, not one per type
    assert set(out["actor"]._paged) <= {"on_trade"}


def test_the_engine_base_fields_reach_the_config_through_new_not_init() -> None:
    """Counter-witness (gate review of #486): the pinned pattern passes ``actor_id`` and the log
    settings to ``DataActorConfig.__new__`` and calls ``super().__init__()`` with none
    (docs/concepts/actors.md:15-21); the base ``__init__`` is ``object.__init__`` and refuses
    them, so forwarding ``**kwargs`` would break every config."""
    import pytest
    from nautilus_trader.model import ActorId

    config = RankingActorConfig(
        instrument_ids=IDS,
        feed="sip",
        host_factory=cast(Any, lambda: FakeHost([])),
        priming=priming,
        page_window_s=3600,
        trade_facts=None,
        actor_id=ActorId("RANKING-PROOF"),
        log_events=False,
        log_commands=False,
    )
    assert config.actor_id == ActorId("RANKING-PROOF")
    assert config.log_events is False and config.log_commands is False

    class Forwarding(DataActorConfig):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

    with pytest.raises(TypeError, match=r"object\.__init__"):
        Forwarding(log_events=False)
