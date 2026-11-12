# pyright: reportIncompatibleMethodOverride=false, reportAttributeAccessIssue=false
"""RED witnesses for protection-layer cells where B's Strategy differs from
handoffs/tasks/b-engine-strategy/PROTECT-STATE-TABLE.md, beyond the reviewers' own files.

Shape-neutral on purpose: the same file runs off main (4e25b0be) and off the owner's top
(lead/b-s1-4w 116aac10), so it calls only B's event handlers and watches the Strategy API
(``cancel_order``, ``modify_order``) both heads end in. Fixture values only; test-only.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from nautilus_trader.config import RiskEngineConfig
from nautilus_trader.model import ClientOrderId, OrderSide, OrderStatus, OrderType, Quantity
from test_b_lifecycle import GATE_ON, PENNY, quote
from test_b_lifecycle_entry import QUOTES, at, rows, run_entry
from test_b_lifecycle_protect import NATIVE, Owner
from test_ranking import NS, publisher
from test_ranking_consumer import consumer

from sextant.entry.parameters import EntryParameters
from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig

ENTRY = ClientOrderId("O-20261027-080600-001-BTEST-1")
OTHER = ClientOrderId("OWNER-OTHER")
FULL = EntryParameters.from_rows(rows(), fixture=True)


def entry_order(*, ts_init: int):
    return SimpleNamespace(
        client_order_id=ENTRY,
        instrument_id=PENNY,
        is_closed=False,
        is_open=True,
        is_pending_cancel=False,
        is_pending_update=False,
        is_inflight=False,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.ACCEPTED,
        filled_qty=Quantity.from_int(0),
        leaves_qty=Quantity.from_int(100),
        quantity=Quantity.from_int(100),
        venue_order_id=None,
        tags=["setup=FIRST_LEG"],
        ts_init=ts_init,
    )


class Probe(BLifecycle):
    """B over a stand-in cache and clock; the Strategy API is watched, never sent."""

    def __init__(self, config):
        super().__init__(config)
        self.orders, self.now, self.alerts, self.wire = {}, at("6"), [], []

    @property
    def clock(self):
        return SimpleNamespace(
            timestamp_ns=lambda: self.now,
            set_time_alert_ns=lambda name, due, callback=None, **_: self.alerts.append(
                (due, callback)
            ),
        )

    @property
    def cache(self):
        return SimpleNamespace(
            orders=lambda **_: list(self.orders.values()),
            order=lambda cid: self.orders.get(cid),
            positions_open=lambda **_: [],
        )

    def cancel_order(self, client_order_id, client_id=None, params=None):
        self.wire.append(("cancel", client_order_id, self.now))

    def modify_order(self, client_order_id, *args, **kwargs):
        self.wire.append(("modify", client_order_id, self.now))

    def _record(self, **row):
        return None

    def fire(self, until: int) -> None:
        for due, callback in sorted(self.alerts, key=lambda a: a[0]):
            if due <= until and callback is not None:
                self.now = due
                callback(SimpleNamespace(ts_event=due))
        self.alerts = [a for a in self.alerts if a[0] > until]


def probe(entry_params=None) -> Probe:
    return Probe(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=consumer(publisher(), heartbeat=2, generation=2, inputs=2),
            rank_gate=replace(GATE_ON, value=False),
            native_trail_fraction=NATIVE,
            entry_params=entry_params,
        )
    )


def cancels(engine: Probe) -> list:
    return [row for row in engine.wire if row[0] == "cancel" and row[1] == ENTRY]


# Cell "entry past its time limit x cancel rejected" (verdict s13h P2-1): the cancel is
# owed again later, on a timer or the next event, never lost.
def test_a_rejected_entry_cancel_is_sent_again_later():
    engine = probe()
    order = entry_order(ts_init=at("0"))
    engine.orders[ENTRY] = order
    book = engine.books[PENNY]
    book.entry_id, book.expiry_ns = ENTRY, at("5.5")
    engine._expire(PENNY)
    assert len(cancels(engine)) == 1, "fixture: the time limit sends one cancel"
    rejected = SimpleNamespace(
        instrument_id=PENNY, client_order_id=ENTRY, ts_event=engine.now, reason="fixture"
    )
    engine.on_order_cancel_rejected(rejected)
    engine.fire(at("30"))
    engine.now = at("30")
    engine.on_order_accepted(SimpleNamespace(instrument_id=PENNY, client_order_id=OTHER))
    assert len(cancels(engine)) == 2, (
        f"the rejected entry cancel was never sent again: {engine.wire}"
    )


# Cell "entry working x Strategy rebuilt from the cache" (verdict s13h P2-2): the rebuilt
# Strategy finds its own working entry in the cache and still ends it by its time limit
# (at most ts_init + entry_ttl_s, the limit _enter set it within).
@pytest.mark.xfail(
    strict=True, reason="the entry time limit waits for the engine's managed GTD part (lead 09:3x)"
)
def test_a_rebuilt_strategy_still_ends_a_cached_entry_past_its_time_limit():
    engine = probe(entry_params=FULL)
    engine.orders[ENTRY] = entry_order(ts_init=at("0"))
    engine.now = at("0") + FULL.ns("entry_ttl_s") + NS
    engine.on_order_accepted(SimpleNamespace(instrument_id=PENNY, client_order_id=OTHER))
    engine.fire(engine.now)
    assert len(cancels(engine)) == 1, "the rebuilt Strategy left its cached entry working"


# Cell "trail x modify rejected" (verdicts s13h P1-1 and the one-market-event bound): no
# second command inside the rejection's own callback, and the still-needed resize goes out
# again on the next market event.
def test_a_rejected_resize_is_retried_on_the_next_quote_never_inside_its_callback(tmp_path):
    class Throttled(Owner):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (at("1.0"), "OWNER-RESERVE-1", OrderSide.SELL, 10, "0.9000"),
            (at("1.1"), "OWNER-RESERVE-2", OrderSide.SELL, 10, "0.9000"),
        )

        def __init__(self, config):
            super().__init__(config)
            self.attempts, self.refusals = [], []

        def modify_order(self, client_order_id, *args, **kwargs):
            self.attempts.append(self.clock.timestamp_ns() - at("0"))
            if len(self.attempts) > 5:  # a loop: stop it, the assertion names it
                return None
            return super().modify_order(client_order_id, *args, **kwargs)

        def on_order_modify_rejected(self, event):
            self.refusals.append(self.clock.timestamp_ns() - at("0"))
            super().on_order_modify_rejected(event)

    _, _, strategy = run_entry(
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
    # ADAPTED (ADR-0007:17, one house trail of the full net quantity, re-armed by cancel plus one
    # resubmit): no resize is ever sent, so none is throttled, repeated inside a callback, or
    # retried. Original: a throttled resize retried once at the next quote.
    assert strategy.attempts == [] and strategy.refusals == []
