# pyright: reportIncompatibleMethodOverride=false, reportAttributeAccessIssue=false
"""S1-P1: one house trail of the full unreserved quantity, re-armed by cancel plus one resubmit
(ADR-0007:17; the owner's 2026-11-09 policy, ADR-0007:50). Fixture values only: B over a
stand-in cache and clock, the Strategy API watched.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from nautilus_trader.model import (
    ClientOrderId,
    ContingencyType,
    OrderSide,
    OrderStatus,
    OrderType,
    Quantity,
)
from test_b_lifecycle import GATE_ON, PENNY
from test_b_lifecycle_entry import at
from test_b_lifecycle_protect import NATIVE
from test_ranking import publisher
from test_ranking_consumer import consumer

from sextant.exits.engine import CANCEL_RETRY_NS
from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig


def order(cid, *, side=OrderSide.SELL, kind=OrderType.TRAILING_STOP_MARKET, quantity=100, **extra):
    fields = dict(
        client_order_id=ClientOrderId(cid),
        instrument_id=PENNY,
        side=side,
        order_type=kind,
        status=OrderStatus.ACCEPTED,
        is_closed=False,
        is_inflight=False,
        is_pending_update=False,
        is_pending_cancel=False,
        quantity=Quantity.from_int(quantity),
        filled_qty=Quantity.from_int(0),
        leaves_qty=Quantity.from_int(quantity),
        contingency_type=ContingencyType.NO_CONTINGENCY,
        linked_order_ids=None,
        order_list_id=None,
        ts_init=at("1"),
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


class Probe(BLifecycle):
    def __init__(self, config):
        super().__init__(config)
        self.orders, self.now, self.held, self.alerts, self.wire = {}, at("10"), Decimal(0), [], []
        self.rows = []

    @property
    def clock(self):
        return SimpleNamespace(
            timestamp_ns=lambda: self.now,
            set_time_alert_ns=lambda name, due, callback=None: self.alerts.append((due, callback)),
        )

    @property
    def cache(self):
        return SimpleNamespace(
            orders=lambda **_: list(self.orders.values()), order=lambda cid: self.orders.get(cid)
        )

    def _held(self, book):
        return self.held

    def _arm(self, book, now, shares) -> ClientOrderId | None:
        cid = f"O-20261027-080600-001-BTEST-{len(self.orders) + 10}"
        self.orders[ClientOrderId(cid)] = order(
            cid, quantity=int(shares), status=OrderStatus.SUBMITTED
        )
        self.wire.append(("arm", int(shares)))
        return ClientOrderId(cid)

    def cancel_order(self, client_order_id, client_id=None, params=None):
        self.wire.append(("cancel", str(client_order_id)))

    def modify_order(self, *args, **kwargs):
        self.wire.append(("modify",))

    def _record(self, **row):
        self.rows.append(row)

    def end(self, cid):  # the cancel's terminal event
        self.orders[ClientOrderId(cid)].is_closed = True
        self.on_order_canceled(
            SimpleNamespace(instrument_id=PENNY, client_order_id=ClientOrderId(cid))
        )


def probe(held=100, trail=100) -> Probe:
    engine = Probe(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=consumer(publisher(), heartbeat=2, generation=2, inputs=2),
            rank_gate=replace(GATE_ON, value=False),
            native_trail_fraction=NATIVE,
        )
    )
    engine.held = Decimal(held)
    if trail:
        engine.orders[ClientOrderId(TRAIL)] = order(TRAIL, quantity=trail)
    return engine


TRAIL = "O-20261027-080600-001-BTEST-2"


def test_no_trail_arms_one_of_the_full_target():
    engine = probe(trail=0)
    engine.reconcile(engine.books[PENNY])
    assert engine.wire == [("arm", 100)]


# An owner add: the one trail is cancelled, and one trail of the full target is armed only on
# the cancel's terminal event; never a second trail beside it, never a modify.
def test_a_changed_quantity_re_arms_by_cancel_plus_one_resubmit():
    engine = probe(held=150)
    book = engine.books[PENNY]
    engine.reconcile(book)
    engine.reconcile(book)  # the cancel is in flight: nothing more
    assert engine.wire == [("cancel", TRAIL)]
    engine.end(TRAIL)
    assert engine.wire == [("cancel", TRAIL), ("arm", 150)]


def test_an_owner_reservation_re_arms_the_net_quantity():
    engine = probe()
    engine.orders[ClientOrderId("OWNER-1")] = order("OWNER-1", kind=OrderType.LIMIT, quantity=30)
    engine.reconcile(engine.books[PENNY])
    engine.end(TRAIL)
    assert engine.wire == [("cancel", TRAIL), ("arm", 70)]


# Plan P6: an owner OCO pair reserves once, at its larger leg (Alpaca orders page :165).
def test_an_owner_oco_pair_is_counted_once():
    engine = probe(trail=0)
    oco: dict[str, Any] = dict(contingency_type=ContingencyType.OCO, order_list_id="OL-1")
    take = order(
        "OWNER-TP", kind=OrderType.LIMIT, quantity=30, linked_order_ids=["OWNER-SL"], **oco
    )
    stop = order(
        "OWNER-SL", kind=OrderType.STOP_MARKET, quantity=30, linked_order_ids=["OWNER-TP"], **oco
    )
    engine.orders[take.client_order_id], engine.orders[stop.client_order_id] = take, stop
    engine.reconcile(engine.books[PENNY])
    assert engine.wire == [("arm", 70)], "the pair was counted twice"


# A trail that is completing is never cancelled (LEAD-2201 B3 a); one page; the remainder is
# armed once it ends.
def test_a_triggered_or_partly_filled_trail_completes_then_the_rest_is_armed():
    engine = probe(held=90)
    trail = engine.orders[ClientOrderId(TRAIL)]
    trail.status, trail.filled_qty = OrderStatus.PARTIALLY_FILLED, Quantity.from_int(10)
    book = engine.books[PENNY]
    engine.reconcile(book)
    engine.reconcile(book)
    assert engine.wire == []
    assert [r["reason"] for r in engine.rows if r.get("kind") == "page"] == [
        "exit_triggered_left_to_complete"
    ]
    trail.status, trail.is_closed = OrderStatus.CANCELED, True
    engine.held = Decimal(60)
    engine.reconcile(book)
    assert engine.wire == [("arm", 60)]


# The accepted gap is bounded: a re-arm the venue has not accepted within the cancel bound
# pages; an accepted one does not.
def test_a_re_arm_not_accepted_within_the_cancel_bound_pages():
    engine = probe(held=150)
    engine.reconcile(engine.books[PENNY])
    engine.end(TRAIL)
    ((due, check),) = engine.alerts
    assert due == engine.now + CANCEL_RETRY_NS
    check(None)
    pages = [r["reason"] for r in engine.rows if r.get("kind") == "page"]
    assert pages == ["rearm_not_accepted"]
    accepted = probe(held=150)
    accepted.reconcile(accepted.books[PENNY])
    accepted.end(TRAIL)
    for o in accepted.orders.values():
        if not o.is_closed:
            o.status = OrderStatus.ACCEPTED
    accepted.alerts[0][1](None)
    assert not [r for r in accepted.rows if r.get("reason") == "rearm_not_accepted"]


# B's own high-water mark and width are not reset by a re-arm: the new trail starts where B is.
def test_a_re_arm_keeps_bs_high_water_mark_and_width():
    engine = probe(held=150)
    book = engine.books[PENNY]
    book.high, book.width = Decimal("0.6100"), Decimal("0.0300")
    engine.reconcile(book)
    engine.end(TRAIL)
    assert engine.wire[-1] == ("arm", 150)
    assert book.high == Decimal("0.6100") and book.width == Decimal("0.0300")
