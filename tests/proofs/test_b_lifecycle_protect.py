"""P-R3 (MODELED): B's first protection and its quantity rule on a real BacktestNode.

Orders named ``OWNER-*`` are submitted through B's own Strategy with a non-house client order
id: the replay shape of a claimed owner order, which carries B's strategy id (LEAD-2201 A8).
Replay cannot show the venue's regular-hours-only trigger: SimulatedExchange triggers the
trail in every session (LEAD-2201 G8).
"""

from __future__ import annotations

import ast
from decimal import Decimal as D
from typing import Any

from nautilus_trader.model import (
    ClientOrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.trading import Strategy
from test_b_lifecycle import MODULE, NS, START, quote
from test_b_lifecycle_entry import QUOTES, Recording, at, run_entry

NATIVE = D("0.025")  # lanes/paper/b-parameters.json native_trail_fraction, passed explicitly
OPEN = START + 19_800 * NS  # 09:30 ET on the fixture day


class Owner(Recording):
    """B's Strategy plus owner orders placed under its strategy id at fixed times."""

    actions: tuple[tuple[int, str, OrderSide, int, str], ...] = ()
    reads = 0

    def on_start(self) -> None:
        super().on_start()
        for at_ns, name, side, size, price in self.actions:
            self.clock.set_time_alert_ns(
                f"owner-{name}",
                at_ns,
                callback=lambda _e, a=(name, side, size, price): self._owner(*a),
            )

    def _owner(self, name: str, side: OrderSide, size: int, price: str) -> None:
        order = self.order_factory.limit(
            self.books_iid(),
            side,
            Quantity.from_int(size),
            Price.from_str(price),
            client_order_id=ClientOrderId(name),
        )
        Strategy.submit_order(self, order)  # not B's command: never recorded in ``sent``

    def books_iid(self):
        return next(iter(self.books))

    def on_order_filled(self, event) -> None:
        super().on_order_filled(event)
        book = self.books[event.instrument_id]
        for _ in range(self.reads):  # reconciliation reads inside the fill's own handler
            self.reconcile(book)


def run(tmp_path, quotes=QUOTES, actions=(), reads=0, **kwargs: Any):
    class Case(Owner):
        pass

    Case.actions, Case.reads = tuple(actions), reads
    kwargs.setdefault("native_trail_fraction", NATIVE)
    return run_entry(tmp_path, quotes=quotes, cls=Case, **kwargs)


def trails(orders):
    return [o for o in orders if o.order_type is OrderType.TRAILING_STOP_MARKET]


def held(orders) -> D:
    bought = sum((o.filled_qty.as_decimal() for o in orders if o.side is OrderSide.BUY), D(0))
    sold = sum((o.filled_qty.as_decimal() for o in orders if o.side is OrderSide.SELL), D(0))
    return bought - sold


def no_owner_target(strategy) -> bool:
    return all(not str(cid).startswith("OWNER") for _, cid, _ in strategy.sent)


FILL_REST = [*QUOTES, quote(at("1"), "0.5050", "0.5100")]  # the entry remainder fills at +1


def test_the_first_fill_arms_one_venue_held_trail_and_added_fills_get_their_own(tmp_path):
    _, orders, strategy = run(tmp_path, quotes=FILL_REST)
    house = trails(orders)
    # ADAPTED (ADR-0007:17, cancel plus one resubmit): the remainder's fill re-arms one trail of the
    # full holding. Original: one trail per fill, their quantities summing to the holding, and no
    # cancel.
    (working,) = [o for o in house if o.is_open]
    assert working.quantity.as_decimal() == held(orders)
    for order in house:
        assert order.side is OrderSide.SELL and order.time_in_force is TimeInForce.GTC
        assert order.is_reduce_only and order.emulation_trigger is TriggerType.NO_TRIGGER
        assert order.trailing_offset_type is TrailingOffsetType.BASIS_POINTS
        assert order.trailing_offset == D("250.00")
    (entry,) = [o for o in orders if o.side is OrderSide.BUY]
    fills = [e for e in entry.events() if type(e).__name__ == "OrderFilled"]
    assert house[0].ts_init == fills[0].ts_event  # armed in the first fill's own handler
    assert [o.quantity.as_decimal() for o in house] == [
        fills[0].last_qty.as_decimal(),
        held(orders),
    ]
    assert [kind for kind, _, _ in strategy.sent].count("cancel") == 1


def test_an_owner_reservation_reduces_coverage_and_a_full_one_pages(tmp_path):
    later = [*QUOTES, quote(at("0.8"), "0.5050", "0.5100")]
    reserve = [(at("0.7"), "OWNER-1", OrderSide.SELL, 30, "0.9000")]
    _, orders, strategy = run(tmp_path, quotes=later, actions=reserve)
    house = [o for o in trails(orders) if o.is_open]
    assert sum(o.leaves_qty.as_decimal() for o in house) == held(orders) - 30
    assert no_owner_target(strategy)
    full = [(at("0.7"), "OWNER-1", OrderSide.SELL, 111, "0.9000")]  # every share held
    journal, orders, strategy = run(tmp_path / "full", quotes=later, actions=full)
    assert [o for o in trails(orders) if o.is_open] == []
    assert any(row.get("reason") == "owner_reserved_unprotected" for row in journal)
    assert no_owner_target(strategy)


def test_an_owner_add_is_covered_by_a_second_trail(tmp_path):
    add = [(at("0.7"), "OWNER-2", OrderSide.BUY, 50, "0.6000")]
    quotes = [*QUOTES, quote(at("0.8"), "0.5050", "0.5100")]
    journal, orders, strategy = run(tmp_path, quotes=quotes, actions=add)
    protects = [row for row in journal if row["kind"] == "protect"]
    # ADAPTED (ADR-0007:17, cancel plus one resubmit). Original: a second trail for the added
    # shares, ["100", "11", "50"].
    assert [row["quantity"] for row in protects] == ["100", "111", "161"]
    (house,) = [o for o in trails(orders) if o.is_open]
    assert house.leaves_qty.as_decimal() == held(orders) == 161
    assert no_owner_target(strategy)


def test_an_owner_sale_reduces_the_trail_by_quantity_only(tmp_path):
    sale = [(at("0.7"), "OWNER-3", OrderSide.SELL, 40, "0.4000")]
    quotes = [*QUOTES, quote(at("0.8"), "0.5050", "0.5100")]
    _, orders, strategy = run(tmp_path, quotes=quotes, actions=sale)
    # ADAPTED (ADR-0007:17, cancel plus one resubmit). Original: the trail came down by a quantity-
    # only modify.
    assert [kind for kind, _, _ in strategy.sent].count("modify") == 0
    house = [o for o in trails(orders) if o.is_open]
    assert len(house) == 1 and house[0].leaves_qty.as_decimal() == held(orders)
    for order in house:
        assert order.trailing_offset == D("250.00")  # the distance is unchanged
    assert no_owner_target(strategy)


def test_a_triggered_partly_filled_trail_is_left_to_complete(tmp_path):
    # An owner sale moves the position away from the trails, then the price falls through the
    # stop and the trail fills in two prints; three reconcile reads run inside each fill.
    fall = [*QUOTES, quote(at("2"), "0.4900", "0.4910"), quote(at("3"), "0.4800", "0.4810")]
    sale = [(at("1.5"), "OWNER-4", OrderSide.SELL, 5, "0.4000")]
    journal, orders, strategy = run(tmp_path, quotes=fall, actions=sale, reads=3)
    (fired,) = [o for o in trails(orders) if o.filled_qty.as_decimal() > 0]
    fills = [e for e in fired.events() if type(e).__name__ == "OrderFilled"]
    assert len(fills) >= 2 and fired.status is OrderStatus.FILLED  # its remainder completed
    assert all(cid != fired.client_order_id for kind, cid, _ in strategy.sent if kind != "submit")
    # ADAPTED (ADR-0007:17, cancel plus one resubmit): each quantity change re-arms, so the submit
    # count is not fixed. Original: `.count("submit") == 3` (the entry and two trails).
    assert len([o for o in trails(orders) if o.is_open]) <= 1
    assert any(row.get("reason") == "exit_triggered_left_to_complete" for row in journal)
    assert all(o.ts_init < fills[0].ts_event for o in trails(orders))  # no lower re-cover
    assert no_owner_target(strategy)


def test_the_session_boundary_keeps_the_same_trail_and_writes_one_record(tmp_path):
    cross = [*QUOTES, quote(OPEN - NS, "0.5050", "0.5100"), quote(OPEN + NS, "0.5050", "0.5100")]
    journal, orders, strategy = run(tmp_path, quotes=cross)
    records = [row for row in journal if row["kind"] == "session"]
    assert [(row["from"], row["to"]) for row in records] == [("pre", "rth")]
    # ADAPTED (ADR-0007:17, cancel plus one resubmit): the remainder's fill re-armed before the
    # boundary. Original: every trail, and no command but submits; now the open trail, and only that
    # re-arm's cancel.
    assert records[0]["trails"] == [str(o.client_order_id) for o in trails(orders) if o.is_open]
    assert records[0]["carry"] is True
    (fresh,) = [row for row in journal if row["kind"] == "session_quote"]
    assert fresh["gap_ns"] == NS and records[0]["boundary_ns"] == OPEN
    assert [kind for kind, _, _ in strategy.sent if kind != "submit"] == ["cancel"]


def test_every_cancel_and_modify_goes_through_the_house_guard() -> None:
    tree = ast.parse(MODULE.read_text())
    calls: dict[str, set[str]] = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                name = getattr(getattr(node, "func", None), "attr", None)
                if isinstance(node, ast.Call) and name in {"cancel_order", "modify_order"}:
                    calls.setdefault(name, set()).add(function.name)
    assert calls == {"cancel_order": {"_cancel"}, "modify_order": {"_modify"}}
