"""P-R1 (MODELED): B's lifecycle Strategy on a real BacktestNode takes fixture ranking
generations through ``subscribe_data`` and the reviewed consumer and rank gate.

Fixture values exercise mechanisms only; they are not B's registry and confer no authority.
rc8's ``BacktestDataConfig`` refuses custom data (crates/backtest/src/config.rs:858-878, test
:1259-1274), so a test-only in-node actor publishes each recorded generation with
``publish_data`` at its ``ts_init`` (common stub :719, :647), the in-node publisher shape of
ADR-0013 S, and the reviewed ``HeartbeatEmitter`` delivers its heartbeat.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest import BacktestNode
from nautilus_trader.common import DataActor
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
    CustomData,
    Equity,
    InstrumentId,
    NautilusDataType,
    OmsType,
    Price,
    Quantity,
    QuoteTick,
    Symbol,
    TradeId,
    TradeTick,
)
from nautilus_trader.persistence import ParquetDataCatalog
from test_ranking import NS, START, publisher, registry
from test_ranking_consumer import consumer, warm

from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig
from sextant.ranking.generation import GenerationId
from sextant.ranking.heartbeat import HeartbeatEmitter
from sextant.ranking.publisher import ranking_data_type
from sextant.ranking.registry import RANK_GATE_ENABLED

ROOT = Path(__file__).parents[2]
MODULE = ROOT / "src/sextant/paper/b_lifecycle.py"
AT = START + 360 * NS
PENNY = InstrumentId.from_str("PENNY.ALPACA")
EQUITY = Equity(
    instrument_id=PENNY,
    raw_symbol=Symbol("PENNY"),
    currency=Currency.from_str("USD"),
    price_precision=4,
    price_increment=Price.from_str("0.0001"),
    lot_size=Quantity.from_str("1"),
    ts_event=0,
    ts_init=0,
)
GATE_ON = replace(RANK_GATE_ENABLED, value=True)  # a test input, never a registry change


class ReplayConfig(DataActorConfig):
    def __init__(self, *, items: list[tuple[int, Any]], entry: Any, **_kwargs: object) -> None:
        super().__init__()
        self.items, self.entry = items, entry


class GenerationReplay(DataActor):
    """Test-only in-node publisher of recorded generations and their heartbeats."""

    def __init__(self, config: ReplayConfig) -> None:
        super().__init__(config)
        self.items, self.entry = config.items, config.entry

    def on_start(self) -> None:
        emitter = HeartbeatEmitter(
            registry=registry(), clock=self.clock.timestamp_ns, sink=self.entry.accept_heartbeat
        )
        for index, (at, item) in enumerate(self.items):
            self.clock.set_time_alert_ns(
                f"generation-{index}", at, callback=lambda _e, i=item: self._send(i, emitter)
            )

    def _send(self, item: Any, emitter: HeartbeatEmitter) -> None:
        if item == "heartbeat":  # a heartbeat with no generation behind it
            now = self.clock.timestamp_ns()
            self.entry.accept_heartbeat(at=now, received_ns=now)
            return
        data_type = ranking_data_type("sip")
        self.publish_data(data_type, CustomData(data_type, item))
        emitter.emit(item)


def trade(at: int, price: str = "0.5500") -> TradeTick:
    return TradeTick(
        PENNY,
        Price.from_str(price),
        Quantity.from_str("100"),
        AggressorSide.BUY,
        TradeId(str(at)),
        at,
        at,
    )


def quote(at: int, bid: str, ask: str) -> QuoteTick:
    size = Quantity.from_str("100")
    return QuoteTick(PENNY, Price.from_str(bid), Price.from_str(ask), size, size, at, at)


def run_node(
    tmp_path: Path,
    items,
    trades,
    *,
    gate=GATE_ON,
    quotes=(),
    latency=None,
    extra=(),
    cls=BLifecycle,
    start=AT - NS,
    bars=(),
    risk=None,
    **kwargs: Any,
):
    pub = publisher()
    entry = consumer(pub, heartbeat=2, generation=kwargs.pop("generation_age", 1), inputs=2)
    tmp_path.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_instruments([EQUITY])
    catalog.write_trade_ticks([t if isinstance(t, TradeTick) else trade(t) for t in trades])
    if quotes:
        catalog.write_quote_ticks(list(quotes))
    if bars:
        catalog.write_bars(list(bars))
    data = [NautilusDataType.TradeTick] + [NautilusDataType.QuoteTick] * bool(quotes)
    config = BacktestRunConfig(
        venues=[
            BacktestVenueConfig(
                name="ALPACA",
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                starting_balances=["100000 USD"],
                fee_model=MakerTakerFeeModel(Decimal(0), Decimal(0)),
                latency_model=latency,
            )
        ],
        data=[
            BacktestDataConfig(
                data_type=kind, catalog=DataCatalogConfig(path=str(tmp_path)), instrument_id=PENNY
            )
            for kind in data
        ]
        + [
            BacktestDataConfig(
                data_type=NautilusDataType.Bar,
                catalog=DataCatalogConfig(path=str(tmp_path)),
                instrument_id=PENNY,
                bar_types=[str(first.bar_type)],
            )
            for first in bars[:1]
        ],
        engine=BacktestEngineConfig(bypass_logging=True, run_analysis=False, risk_engine=risk),
        raise_exception=True,
        dispose_on_completion=False,
        start=start,  # the clock starts before the first publication
    )
    journal: list[dict[str, Any]] = []
    strategy = cls(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=entry,
            rank_gate=gate,
            journal=journal.append,
            **kwargs,
        )
    )
    node = BacktestNode([config])
    try:
        node.build()
        node.add_actor(config.id, GenerationReplay(ReplayConfig(items=items(pub), entry=entry)))
        node.add_strategy(config.id, strategy)
        for other in extra:
            node.add_strategy(config.id, other)
        result = node.run()[0]
        orders = node.get_engine_cache(config.id).orders()
    finally:
        node.dispose()
    return journal, result, orders


def generation(pub):
    warm(pub)
    published = pub.publish(now=AT)
    assert published is not None
    return published


def ranks(journal):
    return [(row["status"], row["reason"], row["rank"]) for row in journal if row["kind"] == "rank"]


def test_a_fresh_generation_gives_a_candidate_and_a_stale_one_none(tmp_path) -> None:
    journal, result, orders = run_node(
        tmp_path, lambda pub: [(AT, generation(pub))], [AT + NS // 2, AT + 3 * NS // 2]
    )
    assert [row for row in journal if row["kind"] == "generation"] == [
        {"kind": "generation", "ts": AT, "accepted": True, "reason": "accepted"}
    ]
    assert ranks(journal) == [
        ("PASS", "fresh", 1),
        ("REFUSE", "entry_generation_stale", None),
    ]
    assert result.total_orders == 0 and orders == []


def test_a_missing_generation_gives_no_candidate_and_the_reason(tmp_path) -> None:
    journal, result, _ = run_node(tmp_path, lambda pub: [(AT, "heartbeat")], [AT + NS // 2])
    assert ranks(journal) == [("REFUSE", "generation_missing", None)]
    journal, result, _ = run_node(tmp_path / "none", lambda pub: [], [AT + NS // 2])
    assert ranks(journal) == [("REFUSE", "heartbeat_missing", None)]
    assert result.total_orders == 0


def test_a_bad_sequence_generation_gives_no_candidate_and_the_reason(tmp_path) -> None:
    def items(pub):
        first = generation(pub)
        gap = replace(first, generation_id=GenerationId("fixture-1", 3, 0))
        return [(AT, first), (AT + NS // 5, gap)]

    journal, result, _ = run_node(tmp_path, items, [AT + NS // 2])
    assert [row["accepted"] for row in journal if row["kind"] == "generation"] == [True, False]
    assert ranks(journal) == [("REFUSE", "generation sequence discontinuity", None)]
    assert result.total_orders == 0


def test_the_disabled_gate_gives_no_candidate(tmp_path) -> None:
    journal, result, _ = run_node(
        tmp_path,
        lambda pub: [(AT, generation(pub))],
        [AT + NS // 2],
        gate=RANK_GATE_ENABLED,
    )
    assert ranks(journal) == [("DISABLED", "rank_gate_disabled", None)]
    assert all(row["candidate"] is False for row in journal if row["kind"] == "rank")
    assert result.total_orders == 0


FORBIDDEN = {  # LEAD-2201 A8: never on B, in any part
    "submit_order_list",
    "cancel_orders",
    "cancel_all_orders",
    "close_position",
    "close_all_positions",
    "market_exit",
}
ENTRY_PATH = {"order_factory", "submit_order", "modify_order", "cancel_order"}


def test_the_strategy_has_no_forbidden_order_path() -> None:
    names = {
        node.attr
        for node in ast.walk(ast.parse(MODULE.read_text()))
        if isinstance(node, ast.Attribute)
    }
    assert names.isdisjoint(FORBIDDEN)  # S1-2 adds ENTRY_PATH: the entry's own commands


def test_an_unknown_config_key_is_refused_and_an_engine_field_passes() -> None:
    import pytest

    entry = consumer(publisher(), heartbeat=2, generation=1, inputs=2)
    own: dict[str, Any] = dict(
        namespace="BTEST", instrument_ids=(PENNY,), consumer=entry, rank_gate=GATE_ON
    )
    with pytest.raises(TypeError, match="rank_gates"):
        BLifecycleConfig(**own, rank_gates=GATE_ON)  # a misspelled key, never swallowed
    config = BLifecycleConfig(**own, log_events=False)  # an engine field reaches the base
    assert config.log_events is False and str(config.strategy_id) == "BLIFECYCLE-BTEST"
