# pyright: reportAttributeAccessIssue=false, reportIncompatibleMethodOverride=false, reportOptionalMemberAccess=false
# ruff: noqa: RUF059
from dataclasses import replace
from decimal import Decimal

import pytest
import test_b_lifecycle as lifecycle_proofs
from nautilus_trader.config import RiskEngineConfig
from nautilus_trader.execution import StaticLatencyModel
from nautilus_trader.model import OrderSide, OrderStatus, Price, Quantity, QuoteTick
from test_b_lifecycle import GATE_ON, NS, PENNY, quote
from test_b_lifecycle_entry import QUOTES, Recording, at, run_entry
from test_b_lifecycle_protect import NATIVE, Owner, trails


class RaceObserver(Owner):
    def __init__(self, config):
        super().__init__(config)
        self.wire = []
        self.snapshots = []
        self.unsafe_commands = []

    def snapshot(self, source):
        book = self.books[PENNY]
        orders = self.cache.orders(instrument_id=PENNY, strategy_id=self.strategy_id)
        live = [order for order in trails(orders) if not order.is_closed]
        reserved = sum(
            (
                order.leaves_qty.as_decimal()
                for order in orders
                if not order.is_closed
                and order.side is OrderSide.SELL
                and not self._house(order.client_order_id)
            ),
            Decimal(0),
        )
        self.snapshots.append(
            dict(
                source=source,
                time=self.clock.timestamp_ns() - at("0"),
                held=self._held(book),
                reserved=reserved,
                needed=max(Decimal(0), self._held(book) - reserved),
                coverage=sum((order.leaves_qty.as_decimal() for order in live), Decimal(0)),
                trails=[
                    (
                        str(order.client_order_id),
                        str(order.quantity),
                        str(order.leaves_qty),
                        str(order.filled_qty),
                        order.status.name,
                    )
                    for order in live
                ],
            )
        )

    def modify_order(
        self,
        client_order_id,
        quantity=None,
        price=None,
        trigger_price=None,
        client_id=None,
        params=None,
    ):
        order = self.cache.order(client_order_id)
        self.wire.append(
            (
                self.clock.timestamp_ns() - at("0"),
                str(client_order_id),
                str(quantity),
                str(price),
                str(order.quantity),
                str(order.filled_qty),
                order.status.name,
            )
        )
        if order.filled_qty.as_decimal() > 0 or order.status is OrderStatus.TRIGGERED:
            self.unsafe_commands.append(self.wire[-1])
        super().modify_order(client_order_id, quantity, price, trigger_price, client_id, params)

    def cancel_order(self, client_order_id, client_id=None, params=None):
        order = self.cache.order(client_order_id)
        if order.filled_qty.as_decimal() > 0 or order.status is OrderStatus.TRIGGERED:
            self.unsafe_commands.append(("cancel", str(client_order_id)))
        super().cancel_order(client_order_id, client_id, params)

    def on_order_updated(self, event):
        super().on_order_updated(event)
        self.snapshot(f"updated:{event.client_order_id}:{event.quantity}")

    def on_order_filled(self, event):
        super().on_order_filled(event)
        self.snapshot(f"filled:{event.client_order_id}:{event.last_qty}")

    def on_quote(self, event):
        super().on_quote(event)
        if event.ts_event >= at("0.7"):
            self.snapshot("quote")


@pytest.mark.parametrize(
    "actions",
    [
        (("1.5", "BUY", 50),),
        (("1.5", "SELL", 20),),
        (("1.5", "BUY", 50), ("2.0", "SELL", 20)),
        (("1.5", "BUY", 50), ("2.0", "BUY", 10), ("2.5", "SELL", 10)),
        (("1.5", "SELL", 20), ("2.0", "BUY", 20)),
    ],
)
def test_quantity_races_settle_to_unreserved_holdings(tmp_path, actions):
    class Case(RaceObserver):
        pass

    Case.actions = (
        (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
        (at("1.0"), "OWNER-RESERVE", OrderSide.SELL, 30, "0.9000"),
        *(
            (
                at(seconds),
                f"OWNER-{index}",
                getattr(OrderSide, side),
                amount,
                "0.6000" if side == "BUY" else "0.4000",
            )
            for index, (seconds, side, amount) in enumerate(actions)
        ),
    )
    journal, orders, strategy = run_entry(
        tmp_path,
        cls=Case,
        gate=replace(GATE_ON, value=False),
        native_trail_fraction=NATIVE,
        quotes=[
            *QUOTES,
            *(quote(at(str(Decimal(step) / 10)), "0.5200", "0.5250") for step in range(8, 151)),
        ],
        latency=StaticLatencyModel(update_latency_nanos=3 * NS),
    )
    print("ACTIONS", actions)
    print("WIRE", strategy.wire)
    print("FINAL", strategy.snapshots[-1])
    print("PAGES", [row for row in journal if row["kind"] == "page"])
    assert not strategy.unsafe_commands
    assert strategy.snapshots[-1]["coverage"] == strategy.snapshots[-1]["needed"]


@pytest.mark.parametrize("owner_side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("liquidity_consumption", [False, True])
def test_partial_fill_during_quantity_replace_preserves_coverage_and_exit(
    tmp_path, monkeypatch, owner_side, liquidity_consumption
):
    venue_config = lifecycle_proofs.BacktestVenueConfig
    monkeypatch.setattr(
        lifecycle_proofs,
        "BacktestVenueConfig",
        lambda **options: venue_config(**options, liquidity_consumption=liquidity_consumption),
    )

    class Case(RaceObserver):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (at("1.0"), "OWNER-RESERVE", OrderSide.SELL, 30, "0.9000"),
            (
                at("1.3"),
                "OWNER-ADDED",
                owner_side,
                50 if owner_side is OrderSide.BUY else 20,
                "0.6000" if owner_side is OrderSide.BUY else "0.4000",
            ),
        )

    partial = QuoteTick(
        PENNY,
        Price.from_str("0.5000"),
        Price.from_str("0.5050"),
        Quantity.from_int(10),
        Quantity.from_int(100),
        at("1.2"),
        at("1.2"),
    )
    journal, orders, strategy = run_entry(
        tmp_path,
        cls=Case,
        gate=replace(GATE_ON, value=False),
        native_trail_fraction=NATIVE,
        quotes=[
            *QUOTES,
            quote(at("0.8"), "0.5200", "0.5250"),
            partial,
            quote(at("1.5"), "0.5200", "0.5250"),
            quote(at("5"), "0.5200", "0.5250"),
            quote(at("10"), "0.5200", "0.5250"),
        ],
        latency=StaticLatencyModel(update_latency_nanos=3 * NS),
    )
    print("OWNER_SIDE", owner_side)
    print("WIRE", strategy.wire)
    print("SNAPSHOTS", strategy.snapshots)
    print("PAGES", [row for row in journal if row["kind"] == "page"])
    assert not strategy.unsafe_commands
    assert strategy.snapshots[-1]["coverage"] == strategy.snapshots[-1]["needed"]
    at_owner_fill = [
        snapshot
        for snapshot in strategy.snapshots
        if snapshot["source"].startswith("filled:OWNER-ADDED:")
    ]
    assert all(snapshot["coverage"] >= snapshot["needed"] for snapshot in at_owner_fill)


@pytest.mark.xfail(
    strict=True, reason="the entry time limit waits for the engine's managed GTD part (lead 09:3x)"
)
def test_entry_ttl_really_cancels_after_update_answer(tmp_path):
    class Observed(Recording):
        def __init__(self, config):
            super().__init__(config)
            self.cancels = []
            self.answers = []

        def on_order_updated(self, event):
            self.answers.append(self.clock.timestamp_ns() - at("0"))
            super().on_order_updated(event)

        def cancel_order(self, client_order_id, client_id=None, params=None):
            self.cancels.append((self.clock.timestamp_ns() - at("0"), str(client_order_id)))
            super().cancel_order(client_order_id, client_id, params)

    journal, orders, strategy = run_entry(
        tmp_path,
        cls=Observed,
        quotes=[
            *QUOTES,
            quote(at("0.6"), "0.5150", "0.5200"),
            quote(at("0.9"), "0.5150", "0.5200"),
            quote(at("1"), "0.5050", "0.5103"),
            quote(at("1.1"), "0.5150", "0.5200"),
            quote(at("5.6"), "0.5150", "0.5200"),
            quote(at("12"), "0.5150", "0.5200"),
        ],
        latency=StaticLatencyModel(insert_latency_nanos=NS // 5, update_latency_nanos=10 * NS),
    )
    print("CANCELS", strategy.cancels)
    print("FINAL_ORDERS", [(str(order.client_order_id), order.status.name) for order in orders])
    print("EXPIRE_DUE", strategy.books[PENNY].expire_due)
    assert strategy.cancels
    assert strategy.cancels[0][0] == strategy.answers[-1]
    assert all(order.is_closed for order in orders)


def test_modify_throttle_rejection_does_not_retry_inside_the_same_callback(tmp_path):
    class Throttled(Owner):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (at("1.0"), "OWNER-RESERVE-1", OrderSide.SELL, 10, "0.9000"),
            (at("1.1"), "OWNER-RESERVE-2", OrderSide.SELL, 10, "0.9000"),
        )

        def __init__(self, config):
            super().__init__(config)
            self.attempts = []
            self.refusals = []
            self.stopped_loop = False

        def modify_order(
            self,
            client_order_id,
            quantity=None,
            price=None,
            trigger_price=None,
            client_id=None,
            params=None,
        ):
            self.attempts.append(
                (self.clock.timestamp_ns() - at("0"), str(client_order_id), str(quantity))
            )
            if len(self.attempts) > 5:
                self.stopped_loop = True
                return
            super().modify_order(client_order_id, quantity, price, trigger_price, client_id, params)

        def on_order_modify_rejected(self, event):
            self.refusals.append((self.clock.timestamp_ns() - at("0"), str(event.reason)))
            super().on_order_modify_rejected(event)

    journal, orders, strategy = run_entry(
        tmp_path,
        cls=Throttled,
        gate=replace(GATE_ON, value=False),
        native_trail_fraction=NATIVE,
        quotes=[
            *QUOTES,
            quote(at("0.8"), "0.5200", "0.5250"),
            quote(at("2.5"), "0.5200", "0.5250"),
        ],
        risk=RiskEngineConfig(max_order_modify_rate="1/00:00:01"),
    )
    print("THROTTLED_ATTEMPTS", strategy.attempts)
    print("THROTTLED_REFUSALS", strategy.refusals)
    print("THROTTLED_STOPPED_LOOP", strategy.stopped_loop)
    print(
        "THROTTLED_ORDERS",
        [
            (str(order.client_order_id), str(order.leaves_qty), order.status.name)
            for order in orders
        ],
    )
    # ADAPTED (ADR-0007:17, one house trail of the full net quantity, re-armed by cancel plus one
    # resubmit): no quantity modify is sent at all, so none is throttled and nothing can repeat
    # inside a rejection. Original: `assert strategy.refusals` (a throttled resize).
    assert strategy.attempts == []
    assert not strategy.stopped_loop


def test_stale_second_resize_cannot_restore_coverage_above_current_holdings(tmp_path):
    class Case(RaceObserver):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (at("1.0"), "OWNER-RESERVE", OrderSide.SELL, 30, "0.9000"),
            (at("1.5"), "OWNER-ADDED", OrderSide.BUY, 50, "0.6000"),
            (at("2.0"), "OWNER-SOLD", OrderSide.SELL, 100, "0.4000"),
        )

    journal, orders, strategy = run_entry(
        tmp_path,
        cls=Case,
        gate=replace(GATE_ON, value=False),
        native_trail_fraction=NATIVE,
        quotes=[
            *QUOTES,
            *(quote(at(str(Decimal(step) / 10)), "0.5200", "0.5250") for step in range(8, 101)),
        ],
        latency=StaticLatencyModel(update_latency_nanos=3 * NS),
    )
    late_resize_answers = [
        snapshot
        for snapshot in strategy.snapshots
        if snapshot["source"].startswith("updated:") and snapshot["time"] >= 4 * NS
    ]
    print("STALE_RESIZE_WIRE", strategy.wire)
    print("STALE_RESIZE_ANSWERS", late_resize_answers)
    print("STALE_RESIZE_FINAL", strategy.snapshots[-1])
    # ADAPTED (ADR-0007:17, one house trail of the full net quantity, re-armed by cancel plus one
    # resubmit): no resize is sent, so the precondition `assert late_resize_answers` cannot hold.
    # The safety check runs on every snapshot: cover is above the holding only at an owner sale's
    # own instant while B's cancel of every working trail is already in flight (the venue's reduce-
    # only sync shrinks it in the same event). Original: `:120` answers after 4 s only.
    assert strategy.snapshots
    over = [s for s in strategy.snapshots if s["coverage"] > s["held"]]
    assert not [s for s in over if any(t[4] != "PENDING_CANCEL" for t in s["trails"])], over
