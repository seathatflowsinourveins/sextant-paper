# pyright: reportAttributeAccessIssue=false, reportIncompatibleMethodOverride=false, reportOptionalMemberAccess=false
from dataclasses import replace
from decimal import Decimal

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel, StaticLatencyModel
from nautilus_trader.model import (
    AccountType,
    BookAction,
    BookOrder,
    BookType,
    Currency,
    Money,
    OmsType,
    OrderBookDelta,
    OrderBookDeltas,
    OrderSide,
    Price,
    Quantity,
    RecordFlag,
    Venue,
)
from test_b_lifecycle import EQUITY, GATE_ON, NS, PENNY, quote, trade
from test_b_lifecycle_entry import at
from test_b_lifecycle_protect import NATIVE
from test_ranking import publisher
from test_ranking_consumer import consumer
from test_review_races import RaceObserver

from sextant.paper.b_lifecycle import BLifecycleConfig


def book_snapshot(seconds, bid, bid_size, ask, ask_size):
    timestamp = at(seconds)
    return OrderBookDeltas(
        PENNY,
        [
            OrderBookDelta.clear(PENNY, timestamp, timestamp, timestamp),
            OrderBookDelta(
                PENNY,
                BookAction.ADD,
                BookOrder(OrderSide.BUY, Price.from_str(bid), Quantity.from_int(bid_size), 1),
                0,
                timestamp,
                timestamp,
                timestamp,
            ),
            OrderBookDelta(
                PENNY,
                BookAction.ADD,
                BookOrder(OrderSide.SELL, Price.from_str(ask), Quantity.from_int(ask_size), 2),
                RecordFlag.F_LAST.value,
                timestamp,
                timestamp,
                timestamp,
            ),
        ],
    )


def test_added_shares_stay_covered_when_a_trail_remains_partly_filled():
    class Case(RaceObserver):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (at("1.0"), "OWNER-RESERVE", OrderSide.SELL, 30, "0.9000"),
            (at("1.3"), "OWNER-ADDED", OrderSide.BUY, 50, "0.6000"),
        )

    journal = []
    strategy = Case(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=consumer(publisher(), heartbeat=2, generation=2, inputs=2),
            rank_gate=replace(GATE_ON, value=False),
            native_trail_fraction=NATIVE,
            journal=journal.append,
        )
    )
    engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
    try:
        engine.add_venue(
            venue=Venue("ALPACA"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money(100000, Currency.from_str("USD"))],
            fee_model=MakerTakerFeeModel(Decimal(0), Decimal(0)),
            latency_model=StaticLatencyModel(update_latency_nanos=3 * NS),
            book_type=BookType.L2_MBP,
            liquidity_consumption=True,
            trade_execution=False,
        )
        engine.add_instrument(EQUITY)
        engine.add_strategy(strategy)
        engine.add_data(
            [
                book_snapshot("0.5", "0.5200", 100, "0.5250", 100),
                book_snapshot("0.8", "0.5200", 100, "0.5250", 200),
                book_snapshot("1.2", "0.5000", 10, "0.5050", 100),
            ]
        )
        engine.add_data(
            [
                quote(at("0.5"), "0.5200", "0.5250"),
                quote(at("0.8"), "0.5200", "0.5250"),
                quote(at("1.2"), "0.5000", "0.5050"),
                quote(at("1.5"), "0.5200", "0.5250"),
                quote(at("5"), "0.5200", "0.5250"),
                quote(at("7"), "0.5200", "0.5250"),
            ]
        )
        engine.add_data([trade(at("0.5"), "0.5200"), trade(at("1.2"), "0.5000")])
        engine.run(start=at("0"), end=at("7.1"))
        print("L2_WIRE", strategy.wire)
        print("L2_SNAPSHOTS", strategy.snapshots)
        print("L2_PAGES", [row for row in journal if row["kind"] == "page"])
        print(
            "L2_ORDER_EVENTS",
            [
                (str(order.client_order_id), [str(event) for event in order.events()])
                for order in engine.cache.orders()
            ],
        )
        assert any(
            snapshot["trails"] and any(Decimal(trail[3]) > 0 for trail in snapshot["trails"])
            for snapshot in strategy.snapshots
        )
        at_addition = [
            snapshot
            for snapshot in strategy.snapshots
            if snapshot["source"].startswith("filled:OWNER-ADDED:")
        ]
        assert at_addition
        # ADAPTED (ADR-0007:17, one house trail of the full net quantity, re-armed by cancel plus
        # one resubmit; ADR-0007:25, LEAD-2308, lead decision 09:3x): while the partly filled trail
        # completes, the added shares wait and are paged once. Original: cover >= needed at the
        # addition.
        pages = [row.get("reason") for row in journal if row["kind"] == "page"]
        assert "exit_triggered_left_to_complete" in pages
        assert not [s for s in strategy.snapshots if s["coverage"] > s["held"]]
        assert strategy.snapshots[-1]["coverage"] == strategy.snapshots[-1]["needed"]
    finally:
        engine.dispose()
