# pyright: reportAttributeAccessIssue=false, reportIncompatibleMethodOverride=false, reportOptionalMemberAccess=false
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model import ClientOrderId, OrderSide, OrderStatus, OrderType, Quantity
from test_b_lifecycle import EQUITY, GATE_ON, PENNY, trade
from test_b_lifecycle_entry import at
from test_b_lifecycle_protect import NATIVE
from test_ranking import publisher
from test_ranking_consumer import consumer

from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig

CID = ClientOrderId("O-20261027-080600-001-BTEST-2")


def cached_order(*, filled=0, quantity=100, pending=True, trail=True):
    return SimpleNamespace(
        client_order_id=CID,
        instrument_id=PENNY,
        is_closed=False,
        is_pending_cancel=False,
        is_pending_update=pending,
        is_inflight=pending,
        side=OrderSide.SELL if trail else OrderSide.BUY,
        order_type=OrderType.TRAILING_STOP_MARKET if trail else OrderType.LIMIT,
        status=(
            OrderStatus.PENDING_UPDATE
            if pending
            else OrderStatus.PARTIALLY_FILLED
            if filled
            else OrderStatus.ACCEPTED
        ),
        filled_qty=Quantity.from_int(filled),
        leaves_qty=Quantity.from_int(quantity - filled),
        quantity=Quantity.from_int(quantity),
        ts_init=at("0.7"),
        # ADAPTED (ADR-0007:17, plan P6): the engine's native contingency fields, which the
        # reservation count reads (an OCO pair counts once); none here.
        contingency_type=None,
        linked_order_ids=None,
        order_list_id=None,
    )


class EngineProbe(BLifecycle):
    def __init__(self, config):
        super().__init__(config)
        self.orders = {}
        self.now = at("6")
        self.held = Decimal(0)
        self.reserved = Decimal(0)
        self.arms = []
        self.commands = []
        self.rows = []
        self.trades = {}  # the last trade per instrument, as the engine's cache holds it
        self.requests = []  # the trail seed's data requests (instrument, limit)
        self.books[PENNY].instrument = EQUITY

    @property
    def clock(self):
        return SimpleNamespace(timestamp_ns=lambda: self.now)

    @property
    def cache(self):
        return SimpleNamespace(
            orders=lambda **filters: list(self.orders.values()),
            order=lambda client_order_id: self.orders.get(client_order_id),
            trade=lambda instrument_id: self.trades.get(instrument_id),
        )

    def on_trade(self, trade):  # the engine caches a trade before on_trade (dispatch.rs:117-128)
        self.trades[trade.instrument_id] = trade
        super().on_trade(trade)

    def request_trades(self, instrument_id, **kwargs):
        self.requests.append((instrument_id, kwargs.get("limit")))

    def _held(self, book):
        return self.held

    def _arm(self, book, now, shares):
        self.arms.append(shares)

    def _modify(self, client_order_id, **change):
        self.commands.append(("modify", client_order_id, change))

    def _cancel(self, client_order_id):
        self.commands.append(("cancel", client_order_id))

    def _record(self, **row):
        self.rows.append(row)


def probe():
    return EngineProbe(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=consumer(publisher(), heartbeat=2, generation=2, inputs=2),
            rank_gate=replace(GATE_ON, value=False),
            native_trail_fraction=NATIVE,
        )
    )


def owner_reservation(engine):
    reservation = cached_order(quantity=30, pending=False, trail=False)
    reservation.client_order_id = ClientOrderId("OWNER-RESERVE")
    reservation.side = OrderSide.SELL
    engine.orders[reservation.client_order_id] = reservation


@pytest.mark.parametrize("pending", [False, True])
def test_added_shares_have_cover_while_an_existing_exit_is_partly_filled(pending):
    engine = probe()
    order = cached_order(filled=10, pending=pending)
    engine.orders[CID] = order
    owner_reservation(engine)
    engine.held = Decimal(140)
    engine.on_order_filled(
        SimpleNamespace(instrument_id=PENNY, client_order_id=ClientOrderId("OWNER-ADDED"))
    )
    print(
        "PARTIAL_ADDITION",
        "pending",
        pending,
        "held",
        engine.held,
        "leaves",
        order.leaves_qty,
        "arms",
        engine.arms,
        "pages",
        engine.rows,
    )
    assert not engine.commands
    # ADAPTED (ADR-0007:17, one house trail of the full net quantity, re-armed by cancel plus one
    # resubmit; ADR-0007:25 and LEAD-2308, lead decision 09:3x): while the one trail completes, the
    # added shares wait, paged once, and are armed on its end. Original: the leaves plus a second
    # trail equal the unreserved holding.
    assert engine.arms == []
    assert any(r.get("reason") == "exit_triggered_left_to_complete" for r in engine.rows)


def test_quantity_answer_does_not_strand_the_added_shares_after_a_partial_fill():
    engine = probe()
    order = cached_order(filled=10, quantity=70, pending=False)
    engine.orders[CID] = order
    owner_reservation(engine)
    engine.held = Decimal(140)
    engine.on_order_updated(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    print(
        "PARTIAL_RESIZE_ANSWER",
        "held",
        engine.held,
        "reserved",
        30,
        "leaves",
        order.leaves_qty,
        "arms",
        engine.arms,
    )
    assert not engine.commands
    # ADAPTED (ADR-0007:17, one house trail of the full net quantity, re-armed by cancel plus one
    # resubmit; ADR-0007:25, LEAD-2308): no resize exists to answer, and a partly filled trail
    # completes before the rest is armed. Original: the leaves plus a second trail.
    assert engine.arms == []
    assert any(r.get("reason") == "exit_triggered_left_to_complete" for r in engine.rows)


@pytest.mark.xfail(
    strict=True, reason="the entry time limit waits for the engine's managed GTD part (lead 09:3x)"
)
def test_entry_ttl_survives_a_rejected_cancel_after_the_replace_answer():
    engine = probe()
    order = cached_order(trail=False)
    engine.orders[CID] = order
    book = engine.books[PENNY]
    book.entry_id, book.expiry_ns = CID, at("5.5")
    engine._expire(PENNY)
    assert book.expire_due and not engine.commands
    engine.now = at("11")
    order.is_pending_update = order.is_inflight = False
    order.status = OrderStatus.ACCEPTED
    engine.on_order_updated(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    assert len(engine.commands) == 1
    order.is_pending_cancel = True
    order.status = OrderStatus.PENDING_CANCEL
    order.is_pending_cancel = False
    order.status = OrderStatus.ACCEPTED
    engine.on_order_cancel_rejected(
        SimpleNamespace(instrument_id=PENNY, client_order_id=CID, ts_event=engine.now)
    )
    engine.now = at("12")
    engine.on_order_updated(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    print(
        "TTL_REJECTED_CANCEL",
        "commands",
        engine.commands,
        "expire_due",
        book.expire_due,
        "status",
        order.status,
    )
    assert len(engine.commands) == 2


@pytest.mark.xfail(
    strict=True, reason="the entry time limit waits for the engine's managed GTD part (lead 09:3x)"
)
@pytest.mark.parametrize("answer", ["updated", "modify_rejected", "filled"])
def test_entry_ttl_answer_and_terminal_controls(answer):
    engine = probe()
    order = cached_order(trail=False)
    engine.orders[CID] = order
    book = engine.books[PENNY]
    book.entry_id, book.expiry_ns = CID, at("5.5")
    engine._expire(PENNY)
    order.is_pending_update = order.is_inflight = False
    order.status = OrderStatus.FILLED if answer == "filled" else OrderStatus.ACCEPTED
    order.is_closed = answer == "filled"
    getattr(engine, f"on_order_{answer}")(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    print("TTL_CONTROL", answer, "commands", engine.commands, "expire_due", book.expire_due)
    assert len(engine.commands) == (0 if answer == "filled" else 1)
    assert not book.expire_due


@pytest.mark.xfail(
    strict=True, reason="the entry time limit waits for the engine's managed GTD part (lead 09:3x)"
)
def test_entry_expiry_remains_effective_after_a_strategy_restart():
    before = probe()
    order = cached_order(trail=False)
    before.orders[CID] = order
    book = before.books[PENNY]
    book.entry_id, book.expiry_ns = CID, at("5.5")
    before._expire(PENNY)
    assert book.expire_due
    after = probe()
    after.orders = before.orders
    after.now = at("11")
    order.is_pending_update = order.is_inflight = False
    order.status = OrderStatus.ACCEPTED
    after.on_order_updated(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    print(
        "TTL_RESTART",
        "entry_id",
        after.books[PENNY].entry_id,
        "expire_due",
        after.books[PENNY].expire_due,
        "commands",
        after.commands,
    )
    assert after.commands == [("cancel", CID)]


@pytest.mark.parametrize("refusal", ["denied", "rejected"])
def test_refused_trail_rearms_on_the_next_trade(refusal):
    engine = probe()
    order = cached_order(pending=False)
    order.is_closed = True
    order.status = OrderStatus.DENIED if refusal == "denied" else OrderStatus.REJECTED
    engine.orders[CID] = order
    engine.held = Decimal(100)
    getattr(engine, f"on_order_{refusal}")(
        SimpleNamespace(instrument_id=PENNY, client_order_id=CID, ts_event=engine.now)
    )
    # A denial with no trade cached waits for the next trade, never the next quote (the trail
    # price part: the RiskEngine prices this trail from the last trade only); a rejection
    # re-arms on the next price.
    book = engine.books[PENNY]
    assert (book.no_trade if refusal == "denied" else book.rearm) and not engine.arms
    engine.on_trade(trade(engine.now, "0.5200"))
    print("TRADE_REARM", refusal, engine.arms)
    assert engine.arms == [Decimal(100)]


@pytest.mark.xfail(
    strict=True, reason="the entry time limit waits for the engine's managed GTD part (lead 09:3x)"
)
def test_unanswered_price_modify_retains_expiry_without_an_illegal_cancel():
    engine = probe()
    order = cached_order(trail=False)
    engine.orders[CID] = order
    book = engine.books[PENNY]
    book.entry_id, book.expiry_ns = CID, at("5.5")
    engine._expire(PENNY)
    engine.now = at("60")
    engine.on_order_accepted(
        SimpleNamespace(instrument_id=PENNY, client_order_id=ClientOrderId("OWNER-OTHER"))
    )
    print("TTL_NO_ANSWER", "expire_due", book.expire_due, "commands", engine.commands)
    assert book.expire_due and not engine.commands
