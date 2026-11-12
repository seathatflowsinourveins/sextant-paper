# pyright: reportAttributeAccessIssue=false
"""S1 fix round 1: the protection core's three review findings, each witnessed first.

1. A cancel rejection that arrives after its retry is already due still leaves a retry on the
   engine's clock (review s1p2-de481ca), so a quiet feed cannot leave the trail unretried.
2. The venue's client row is resolved before a command's bookkeeping (review s1p2b-eb33a6a).
3. Only the bool False turns the rank gate off without a ranking consumer (node builder, n3).
Fixture values only; the stand-in is test_b_protect_single's.
"""

from __future__ import annotations

import pytest
from test_b_lifecycle import PENNY
from test_b_protect_cancel import CID, cancels, fire, mismatched, rejected

from sextant.exits.engine import CANCEL_RETRY_NS


def test_an_overdue_cancel_rejection_retries_on_the_clock_in_a_quiet_feed():
    engine = mismatched()
    book = engine.books[PENNY]
    engine.now += CANCEL_RETRY_NS + 1  # the rejection arrives after its retry is due
    rejected(engine)
    assert len(cancels(engine)) == 1, "re-sent inside the rejection's callback"
    assert CID in book.recancel and engine.alerts, "no retry on the clock"
    fire(engine, engine.now + CANCEL_RETRY_NS)  # no quote, trade or order event
    assert len(cancels(engine)) == 2


# 2. The venue's client row is resolved before any command's bookkeeping (review s1p2b-eb33a6a):
# a row the engine's ClientId refuses (empty, non-ASCII, not a string) or an absent row sends
# nothing, gives one refusal naming client_id and leaves no record that blocks a retry, so a
# restored row recovers on the next price.
BAD_ROWS = ["", "IBé", 42, "MISSING"]


def set_row(engine, row):
    if row == "MISSING":
        del engine._config.client_id
    else:
        engine._config.client_id = row


def refusals(engine):
    return [r for r in engine.rows if r.get("kind") == "refuse" and r.get("reason") == "client_id"]


def sends_engine(trail=True):
    from decimal import Decimal

    from test_b_protect_client import Sends
    from test_b_protect_single import probe

    from sextant.paper.b_lifecycle import BLifecycleConfig

    base = probe(held=150, trail=100 if trail else 0)
    config = base._config
    engine = Sends(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=config.consumer,
            rank_gate=config.rank_gate,
            native_trail_fraction=config.native_trail_fraction,
        )
    )
    engine.calls, engine.held, engine.orders = [], Decimal(150), dict(base.orders)
    return engine


def attempt(call):
    """The exception a command let escape, recorded so the recovery is checked first."""
    try:
        call()
    except (AttributeError, TypeError, ValueError) as error:
        return type(error).__name__


@pytest.mark.parametrize("row", BAD_ROWS)
def test_a_bad_client_row_refuses_the_cancel_and_a_restored_row_recovers(row):
    engine = sends_engine()
    book = engine.books[PENNY]
    set_row(engine, row)
    escaped = attempt(lambda: engine.reconcile(book))  # the quantity changed: a cancel is owed
    recorded = (set(book.cancelling), dict(book.cancel_tries))
    engine._config.client_id = None
    engine._rearm(book)  # the next quote or trade
    assert engine.calls == [("cancel", None)], "a restored row never recovered"
    assert escaped is None and recorded == (set(), {}) and len(refusals(engine)) == 1


@pytest.mark.parametrize("row", BAD_ROWS)
def test_a_bad_client_row_refuses_the_trail_and_a_restored_row_arms_on_the_next_price(row):
    engine = sends_engine(trail=False)
    book = engine.books[PENNY]
    set_row(engine, row)
    escaped = attempt(lambda: engine.reconcile(book))  # held and uncovered: a trail is owed
    protects = [r for r in engine.rows if r.get("kind") == "protect"]
    engine._config.client_id = None
    engine._rearm(book)  # a quiet feed: only the next quote
    assert engine.calls == [("submit", None)], "a restored row never armed"
    assert escaped is None and protects == [] and len(refusals(engine)) == 1


@pytest.mark.parametrize("row", BAD_ROWS)
def test_a_bad_client_row_refuses_the_modify_by_name(row):
    from decimal import Decimal

    from nautilus_trader.model import ClientId

    engine = sends_engine()
    set_row(engine, row)
    escaped = attempt(lambda: engine._modify(CID, price=Decimal(1)))
    assert escaped is None and engine.calls == [] and len(refusals(engine)) == 1
    engine._config.client_id = "IB"
    engine._modify(CID, price=Decimal(1))
    assert engine.calls == [("modify", ClientId("IB"))]


def test_a_bad_client_row_refuses_the_entry_and_a_restored_row_enters(tmp_path):
    from test_b_lifecycle import trade
    from test_b_lifecycle_entry import Recording, at, house, run_entry

    class Restores(Recording):
        def __init__(self, config):
            super().__init__(config)
            self.escaped: list[str] = []

        def on_trade(self, trade):
            if trade.ts_event > at("0.5"):
                self._config.client_id = None  # the row restored after the completing trade
            try:
                super().on_trade(trade)
            except ValueError as error:
                self.escaped.append(str(error))

    journal, orders, strategy = run_entry(
        tmp_path, cls=Restores, client_id="IBé", extra_trades=[trade(at("0.7"), "0.5100")]
    )
    assert [o.ts_init for o in house(orders)] == [at("0.7")], "a restored row never entered"
    assert strategy.escaped == []
    assert [r["reason"] for r in journal if r.get("reason") == "client_id"] == ["client_id"]


# 3. Only the bool False is the rank gate's off (ranking/registry.py:50-61): with no ranking
# consumer, a gate row of 0, Decimal(0) or None is refused, never read as off.
@pytest.mark.parametrize("value", [0, "Decimal0", None])
def test_only_the_bool_false_gate_runs_without_a_ranking_consumer(value):
    from dataclasses import replace
    from decimal import Decimal

    from test_b_lifecycle import GATE_ON
    from test_b_lifecycle_protect import NATIVE

    from sextant.paper.b_lifecycle import BLifecycleConfig

    def config(gate):
        return BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=None,
            rank_gate=replace(GATE_ON, value=gate),
            native_trail_fraction=NATIVE,
        )

    config(False)  # the off row: protection only
    with pytest.raises(ValueError, match="RankingConsumer"):
        config(Decimal(0) if value == "Decimal0" else value)
