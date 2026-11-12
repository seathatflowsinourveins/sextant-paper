"""S1-P2: the trail's reduce-only flag is a per-venue value, read where the trail is built. On
Alpaca B's trail is reduce-only (ADR-0005:34); the official Interactive Brokers adapter denies
a reduce-only order before the venue (nautilus_trader v2.0.0rc8
crates/adapters/interactive_brokers/src/execution/core.rs:1032-1043, :2191-2196). With the
flag false, "never more SELL than the holding" rests on the one-trail rule alone: every trail is
armed at the unreserved holding of its moment and nothing grows it. Fixture values only.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model import OrderSide
from test_b_lifecycle import quote
from test_b_lifecycle_entry import QUOTES, at, run_entry
from test_b_lifecycle_protect import NATIVE, Owner, run, trails


class Watched(Owner):
    actions = (
        (at("0.7"), "OWNER-1", OrderSide.SELL, 30, "0.9000"),  # a reservation
        (at("1.5"), "OWNER-2", OrderSide.BUY, 50, "0.6000"),  # an add
        (at("2.0"), "OWNER-3", OrderSide.SELL, 20, "0.4000"),  # a sale that fills
    )

    def __init__(self, config):
        super().__init__(config)
        self.armed = []

    def _arm(self, book, now, shares):
        orders = self.cache.orders(instrument_id=book.instrument_id, strategy_id=self.strategy_id)
        reserved = sum(
            (
                o.leaves_qty.as_decimal()
                for o in orders
                if not o.is_closed
                and o.side is OrderSide.SELL
                and not self._house(o.client_order_id)
            ),
            Decimal(0),
        )
        self.armed.append((shares, self._held(book) - reserved))
        return super()._arm(book, now, shares)


def test_without_reduce_only_no_trail_is_ever_above_the_unreserved_holding(tmp_path):
    quotes = [*QUOTES, quote(at("0.8"), "0.5050", "0.5100"), quote(at("2.5"), "0.5050", "0.5100")]
    _, orders, strategy = run_entry(
        tmp_path, quotes=quotes, cls=Watched, native_trail_fraction=NATIVE, trail_reduce_only=False
    )
    house = trails(orders)
    assert len(house) >= 2 and not any(o.is_reduce_only for o in house)
    assert strategy.armed and all(shares <= unreserved for shares, unreserved in strategy.armed)
    assert "modify" not in [kind for kind, _, _ in strategy.sent]
    for order in house:  # nothing grows a trail: neither B nor the venue's reduce-only sync
        updates = [e.quantity for e in order.events() if type(e).__name__ == "OrderUpdated"]
        assert all(q <= order.events()[0].quantity for q in updates)
    assert len([o for o in house if o.is_open]) <= 1


def test_the_flag_defaults_to_alpacas_reduce_only(tmp_path):
    _, orders, _ = run(tmp_path, quotes=[*QUOTES, quote(at("0.8"), "0.5050", "0.5100")])
    assert trails(orders) and all(o.is_reduce_only for o in trails(orders))
