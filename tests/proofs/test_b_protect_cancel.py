# pyright: reportAttributeAccessIssue=false
"""S1-P2: the cancel is the only command with a record (ADR-0007:17, :27; exits/engine.py:303-
330). A rejected cancel only marks and goes again once the cancel bound has passed since its
send; after K+1 the order is exhausted until it ends; a completing trail is never cancelled;
failure handlers only mark; an absent row ends nothing. Fixture values only; the stand-in is
test_b_protect_single's.
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model import ClientOrderId, OrderStatus, Quantity
from test_b_lifecycle import PENNY
from test_b_protect_single import TRAIL, probe

from sextant.exits.engine import CANCEL_RETRIES, CANCEL_RETRY_NS

CID = ClientOrderId(TRAIL)


def rejected(engine, cid=CID):
    event = SimpleNamespace(instrument_id=PENNY, client_order_id=cid, ts_event=engine.now)
    engine.on_order_cancel_rejected(event)


def cancels(engine):
    return [row for row in engine.wire if row[0] == "cancel"]


def fire(engine, until):
    for due, callback in sorted(engine.alerts, key=lambda a: a[0]):
        if due <= until and callback is not None:
            engine.now = due
            callback(None)
    engine.alerts = [a for a in engine.alerts if a[0] > until]


def pages(engine):
    return [r.get("reason") for r in engine.rows if r.get("kind") == "page"]


def mismatched():
    engine = probe(held=150)
    engine.reconcile(engine.books[PENNY])
    assert cancels(engine) == [("cancel", TRAIL)]
    return engine


def test_a_rejected_cancel_goes_again_only_after_the_cancel_bound():
    engine = mismatched()
    sent, book = engine.now, engine.books[PENNY]
    rejected(engine)
    engine.reconcile(book)  # the next event, inside the bound: nothing
    assert len(cancels(engine)) == 1, "re-sent inside the rejection or before the bound"
    fire(engine, sent + CANCEL_RETRY_NS)
    assert len(cancels(engine)) == 2


def test_after_k_plus_one_the_order_is_exhausted_until_it_ends():
    engine = mismatched()
    book = engine.books[PENNY]
    for _ in range(CANCEL_RETRIES):
        rejected(engine)
        fire(engine, engine.now + CANCEL_RETRY_NS)
    rejected(engine)
    engine.reconcile(book)
    engine._due_cancels(book)
    fire(engine, engine.now + 10 * CANCEL_RETRY_NS)
    assert len(cancels(engine)) == CANCEL_RETRIES + 1
    assert pages(engine).count("cancel_retries_exhausted") == 1
    engine.end(TRAIL)
    assert CID not in book.exhausted and engine.wire[-1] == ("arm", 150)


def test_a_trail_that_starts_executing_is_never_cancelled_on_retry():
    engine = mismatched()
    rejected(engine)
    trail = engine.orders[CID]
    trail.status, trail.filled_qty = OrderStatus.PARTIALLY_FILLED, Quantity.from_int(10)
    fire(engine, engine.now + CANCEL_RETRY_NS)
    fire(engine, engine.now + CANCEL_RETRY_NS)
    assert len(cancels(engine)) == 1
    assert pages(engine).count("exit_triggered_left_to_complete") == 1


def test_a_cancel_refused_before_acceptance_goes_again_on_acceptance():
    engine = mismatched()
    trail = engine.orders[CID]
    trail.status = OrderStatus.SUBMITTED
    rejected(engine)
    engine._rearm(engine.books[PENNY])
    assert len(cancels(engine)) == 1 and engine.alerts == []
    trail.status = OrderStatus.ACCEPTED
    engine.on_order_accepted(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    assert len(cancels(engine)) == 2


def test_an_absent_row_keeps_its_retry_and_pages_once():
    engine = mismatched()
    book = engine.books[PENNY]
    rejected(engine)
    trail = engine.orders.pop(CID)
    for _ in range(2):
        engine.on_order_accepted(SimpleNamespace(instrument_id=PENNY, client_order_id=CID))
    fire(engine, engine.now + CANCEL_RETRY_NS)
    assert CID in book.recancel and len(cancels(engine)) == 1
    assert pages(engine).count("order_not_in_cache") == 1
    engine.orders[CID] = trail
    engine._due_cancels(book)
    assert len(cancels(engine)) == 2


def test_a_venue_rejected_trail_re_arms_on_the_next_quote_never_inside_the_refusal():
    engine = probe(trail=0)
    book = engine.books[PENNY]
    engine.reconcile(book)
    (cid,) = list(engine.orders)
    engine.orders[cid].status, engine.orders[cid].is_closed = OrderStatus.REJECTED, True
    engine.on_order_rejected(SimpleNamespace(instrument_id=PENNY, client_order_id=cid, ts_event=0))
    assert engine.wire == [("arm", 100)], "re-armed inside the refusal"
    engine._rearm(book)
    assert engine.wire == [("arm", 100), ("arm", 100)]


# Lead 09:1x: protection runs without a ranking consumer while the rank gate is off (the entry
# is then refused: its session segment comes from the consumer), and a (re)started Strategy
# reconciles every book at once (ADR-0007:17: "reconnects and starts").
def test_no_ranking_consumer_while_the_gate_is_off_and_recovery_at_start():
    from dataclasses import replace
    from decimal import Decimal

    import pytest
    from test_b_lifecycle import EQUITY, GATE_ON
    from test_b_lifecycle_protect import NATIVE
    from test_b_protect_single import Probe

    from sextant.paper.b_lifecycle import BLifecycleConfig

    class Started(Probe):
        @property
        def cache(self):
            cache = super().cache
            cache.instrument = lambda iid: EQUITY
            return cache

    def config(gate):
        return BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=None,
            rank_gate=gate,
            native_trail_fraction=NATIVE,
        )

    engine = Started(config(replace(GATE_ON, value=False)))
    engine.held = Decimal(100)
    engine.subscribe_trades = engine.subscribe_quotes = lambda *args, **kwargs: None
    engine.on_start()
    assert engine.wire == [("arm", 100)], "a started Strategy left its holding uncovered"
    assert "ranking_consumer" in engine.missing
    with pytest.raises(ValueError, match="RankingConsumer"):
        config(GATE_ON)
