"""LEAD-1544: cancellation races use native matching/events, never a broker.

Removing the pre-stop wait strands a late fill; accepting submitted protection as
coverage hides the gap; forgetting refusal state loses the no-ask receipt.
"""

from __future__ import annotations

import gc
import json
from decimal import Decimal

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.core import UUID4
from nautilus_trader.execution import MakerTakerFeeModel, StaticLatencyModel
from nautilus_trader.model import (
    AccountType,
    AggressorSide,
    Money,
    OmsType,
    OrderCancelRejected,
    OrderType,
    Price,
    Quantity,
    QuoteTick,
    TradeId,
    TradeTick,
)
from nautilus_trader.trading import Controller, ImportableControllerConfig

from sextant.paper import node_order
from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig

from .test_b_attended_entry import IID, opted
from .test_b_attended_entry import owner_thread_gc as owner_thread_gc
from .test_b_order_node import TIMING, AccountVenue, Stand, equity, writes
from .test_b_order_node import expected as expected

NS = 1_000_000_000
AT = 1_794_000_000 * NS
ACTIVE = {}


class StopController(Controller):
    def on_start(self):
        self.clock.set_time_alert_ns("bounded-stop", AT + NS, callback=self.begin)

    def begin(self, _event):
        strategy = ACTIVE["strategy"]
        order = strategy.cache.order(strategy.attended_order_id)
        ACTIVE["before"] = None if order is None else order.status.name
        if ACTIVE["control"]:
            return
        if ACTIVE.get("no_book"):
            strategy.books.pop(IID)
        if ACTIVE.get("reject_event"):
            self.clock.set_time_alert_ns(
                "late-cancel-reject", AT + 13 * NS // 10, callback=self.reject
            )

        def stop():
            own = strategy.cache.order(strategy.attended_order_id)
            trails = [
                o for o in strategy.cache.orders() if o.order_type is OrderType.TRAILING_STOP_MARKET
            ]
            ACTIVE["at_stop"] = {
                "entry": own.status.name,
                "trails": [o.status.name for o in trails],
            }
            self.stop_strategy(strategy.strategy_id)

        # The legacy stop supplies the gate's exact RED shape before the new API exists.
        if hasattr(strategy, "stop_attended"):
            strategy.stop_attended(stop, ACTIVE["bound"])
        else:
            stop()

    def reject(self, _event):
        # rc8 execution/engine/mod.rs:3951-3978 drops a cancel reject on an
        # already FILLED order (invalid transition). Exercise the handler with
        # the engine's event type; this is not end-to-end venue event delivery.
        strategy = ACTIVE["strategy"]
        order = strategy.cache.order(strategy.attended_order_id)
        assert order.status.name == "FILLED"
        assert str(strategy.state()) == "ComponentState.RUNNING"
        now = self.clock.timestamp_ns()
        strategy.on_order_cancel_rejected(
            OrderCancelRejected(
                strategy.trader_id,
                strategy.strategy_id,
                IID,
                order.client_order_id,
                "fixture: filled before cancellation",
                UUID4(),
                now,
                now,
                False,
                venue_order_id=order.venue_order_id,
                account_id=order.account_id,
            )
        )


class Observed(node_order.GatedBLifecycle):
    def on_start(self):
        self.gate = node_order.Gate("OLD-", open=True)
        BLifecycle.on_start(self)

    def _cancel(self, cid):
        if ACTIVE.get("cancel_error"):
            raise RuntimeError("fixture cancel sequence error")
        super()._cancel(cid)

    def _held(self, book):
        if ACTIVE.get("done_error") and self.attended_stopping:
            raise RuntimeError("fixture coverage read error")
        return super()._held(book)

    def on_order_filled(self, event):
        ACTIVE["fills"] += 1
        super().on_order_filled(event)

    def on_order_cancel_rejected(self, event):
        ACTIVE["cancel_rejects"] += 1
        super().on_order_cancel_rejected(event)


def tick(offset, ask="0.1240", size=100):
    at = AT + offset
    return QuoteTick(
        IID,
        Price.from_str("0.1230"),
        Price.from_str(ask),
        Quantity.from_int(size),
        Quantity.from_int(size),
        at,
        at,
    )


def race(
    *,
    control=False,
    partial=False,
    cancel_ns=NS // 2,
    bound=5 * NS,
    insert_ns=NS // 50,
    no_cross=False,
    no_book=False,
    cancel_error=False,
    done_error=False,
    reject_event=False,
):
    ACTIVE.clear()
    ACTIVE.update(
        control=control,
        bound=bound,
        fills=0,
        cancel_rejects=0,
        no_book=no_book,
        cancel_error=cancel_error,
        done_error=done_error,
        reject_event=reject_event,
    )
    engine = BacktestEngine(
        BacktestEngineConfig(
            bypass_logging=True,
            run_analysis=False,
            controller=ImportableControllerConfig(
                controller_path=f"{__name__}:StopController",
                config_path="nautilus_trader.config:DataActorConfig",
                config={},
            ),
        )
    )
    try:
        engine.add_venue(
            venue=IID.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money.from_str("100 USD")],
            fee_model=MakerTakerFeeModel(Decimal(0), Decimal(0)),
            latency_model=StaticLatencyModel(
                insert_latency_nanos=insert_ns, cancel_latency_nanos=cancel_ns
            ),
            liquidity_consumption=partial,
        )
        engine.add_instrument(equity(IID))
        strategy = Observed(
            BLifecycleConfig(
                namespace="BTEST",
                instrument_ids=(IID,),
                consumer=None,
                rank_gate=node_order.RANK_GATE_ENABLED,
                native_trail_fraction=Decimal("0.025"),
            )
        )
        strategy.attended = node_order.AttendedEntry(
            IID, Quantity.from_int(2 if partial else 1), 5 * NS
        )
        ACTIVE["strategy"] = strategy
        book = strategy.books[IID]
        engine.add_strategy(strategy)
        at = AT + NS // 10
        data = [
            TradeTick(
                IID,
                Price.from_str("0.1234"),
                Quantity.from_int(100),
                AggressorSide.BUY,
                TradeId("seed"),
                at,
                at,
            ),
            tick(NS // 5, "0.1234"),
            tick(NS // 4),
            tick(NS // 2),
            tick(3 * NS // 4),
        ]
        if not no_cross:
            data.append(tick(NS + NS // 10, "0.1234", 1 if partial else 100))
        data.extend([tick(NS + NS // 5), tick(3 * NS // 2), tick(2 * NS), tick(7 * NS)])
        engine.add_data(data)
        engine.run()
        orders = engine.cache.orders()
        trails = [o for o in orders if o.order_type is OrderType.TRAILING_STOP_MARKET]
        cid = strategy.attended_order_id
        assert cid is not None
        entry = engine.cache.order(cid)
        assert entry is not None
        snapshot = dict(
            state=str(strategy.state()),
            held=str(BLifecycle._held(strategy, book)),
            fills=ACTIVE["fills"],
            trails=len(trails),
            trail_status=[o.status.name for o in trails],
            trail_qty=[str(o.quantity) for o in trails],
            entry=entry.status.name,
            before=ACTIVE["before"],
            rejects=ACTIVE["cancel_rejects"],
            outcome=getattr(strategy, "attended_outcome", {}),
            at_stop=ACTIVE.get("at_stop"),
        )
        print("RACE", snapshot)
        return snapshot
    finally:
        engine.dispose()
        ACTIVE.clear()
        gc.collect()


def test_gate_witness_late_fill_is_protected_before_stop():
    got = race()
    assert got["before"] == "ACCEPTED"
    assert got["at_stop"] == {"entry": "FILLED", "trails": ["ACCEPTED"]}
    assert (got["held"], got["fills"], got["trails"], got["trail_status"]) == (
        "1",
        1,
        1,
        ["ACCEPTED"],
    ), got


def test_control_without_early_stop_protects_the_same_fill():
    got = race(control=True)
    assert (got["held"], got["fills"], got["trails"], got["trail_status"]) == (
        "1",
        1,
        1,
        ["ACCEPTED"],
    )


def test_partial_fill_during_wait_protects_filled_share_and_cancels_remainder():
    got = race(partial=True)
    assert got["at_stop"] == {"entry": "CANCELED", "trails": ["ACCEPTED"]}
    assert (got["held"], got["fills"], got["entry"], got["trail_qty"], got["trail_status"]) == (
        "1",
        1,
        "CANCELED",
        ["1"],
        ["ACCEPTED"],
    ), got


def test_cancel_reject_after_fill_still_waits_for_accepted_trail():
    got = race(insert_ns=NS // 2, reject_event=True)
    assert got["rejects"] == 1, got
    assert got["at_stop"] == {"entry": "FILLED", "trails": ["ACCEPTED"]}
    assert got["fills"] == 1 and got["trail_status"] == ["ACCEPTED"], got


def test_expiring_bound_names_unresolved_entry_with_counts(capsys):
    got = race(cancel_ns=10 * NS, bound=NS // 5, no_cross=True)
    assert got["outcome"] == {
        "entry_unresolved_at_stop": {"entry_pending": 1, "unprotected_shares": 0}
    }
    assert capsys.readouterr().err.count("ERROR entry_unresolved_at_stop") == 1


def test_expiring_bound_does_not_count_a_submitted_trail_as_accepted(capsys):
    got = race(insert_ns=NS // 4, bound=NS // 5)
    assert got["outcome"] == {
        "entry_unresolved_at_stop": {"entry_pending": 0, "unprotected_shares": 1}
    }, got
    assert capsys.readouterr().err.count("ERROR entry_unresolved_at_stop") == 1


@pytest.mark.parametrize("kind", ["none", "stale", "crossed"])
def test_no_usable_ask_has_named_outcome_without_consuming_attempt(tmp_path, monkeypatch, kind):
    stand = Stand(AccountVenue())
    verify = node_order.GatedBLifecycle._verify
    seen = {}

    async def admitted(strategy):
        await verify(strategy)
        seen["strategy"] = strategy
        if kind != "none":
            now = strategy.clock.timestamp_ns()
            age = 6 * NS if kind == "stale" else 0
            strategy.on_quote(
                QuoteTick(
                    IID,
                    Price.from_str("0.1300" if kind == "crossed" else "0.1230"),
                    Price.from_str("0.1234"),
                    Quantity.from_int(100),
                    Quantity.from_int(100),
                    now - age,
                    now - age,
                )
            )

    monkeypatch.setattr(node_order.GatedBLifecycle, "_verify", admitted)
    path, code = node_order.run(
        opted(tmp_path),
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    body = json.loads(path.read_text())
    print("NOASK", kind, body.get("test_entry_not_sent"), code, len(writes(stand.venue)))
    assert code == 0 and not seen["strategy"].attended_attempted
    assert not writes(stand.venue)
    assert body["test_entry_not_sent"] == {
        "last_reason": "no_quote" if kind == "none" else kind,
        "attempts": 0,
        "submits": 0,
    }


def test_missing_book_finishes_stop_with_named_failure(capsys):
    got = race(no_book=True, no_cross=True)
    assert got["state"] == "ComponentState.STOPPED"
    assert got["outcome"] == {"entry_unresolved_at_stop": {"book_missing": 1}}
    assert capsys.readouterr().err.count("ERROR entry_unresolved_at_stop") == 1


@pytest.mark.parametrize("fault", ["cancel_error", "done_error"])
def test_exception_in_stop_sequence_still_stops_with_named_failure(capsys, fault):
    got = race(no_cross=True, **{fault: True})
    assert got["state"] == "ComponentState.STOPPED"
    assert got["outcome"] == {"entry_unresolved_at_stop": {"sequence_errors": 1}}
    assert capsys.readouterr().err.count("ERROR entry_unresolved_at_stop") == 1


def test_run_failure_and_unresolved_entry_both_survive_in_receipt(tmp_path, monkeypatch):
    stand = Stand(AccountVenue())

    async def fail(node, strategy, args):
        strategy.gate.open = True
        strategy.attended_outcome = {"entry_unresolved_at_stop": {"sequence_errors": 1}}
        raise RuntimeError("fixture run failure")

    monkeypatch.setattr(node_order, "run_attended", fail)
    path, code = node_order.run(
        opted(tmp_path),
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    body = json.loads(path.read_text())
    assert code == 2
    assert body["reason"] == "RuntimeError: fixture run failure"
    assert body["entry_unresolved_at_stop"] == {"sequence_errors": 1}


def test_unattended_path_starts_timer_then_runs_native_node(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from .test_b_order_node import argv

    calls = []

    class Node:
        def handle(self):
            return SimpleNamespace(stop=lambda: calls.append("stop"))

        def run(self):
            calls.append("node.run")

        def dispose(self):
            calls.append("dispose")

    class Timer:
        def __init__(self, _duration, _stop):
            pass

        def start(self):
            calls.append("timer.start")

        def cancel(self):
            calls.append("timer.cancel")

    strategy = SimpleNamespace(
        missing=[], gate=node_order.Gate("OLD-", open=True), journal_errors=0, attended_outcome={}
    )
    monkeypatch.setattr(node_order, "build", lambda *_a, **_k: (Node(), strategy))
    monkeypatch.setattr(node_order.threading, "Timer", Timer)
    path, code = node_order.run(
        node_order.parse(argv(tmp_path)), consumer=None, leases=node_order.take_leases
    )
    assert calls == ["timer.start", "node.run", "timer.cancel", "dispose"]
    assert code == 0 and json.loads(path.read_text())["phase"] == "stopped"
