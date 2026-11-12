"""Stage 1 rehearsal finding 2 (MODELED): a quoted name with no trade yet.

rc8's RiskEngine prices a trail of the default trigger type from the last trade only
(nautilus_trader d53849b2 crates/risk/src/engine/mod.rs:2368-2391, denied at :2144 as
MARKET_PRICE_UNAVAILABLE), so with quotes alone B's trail is denied. A quote cannot change
that: the denied trail pages once and is armed again on the next trade, never on each quote,
so no order id is spent per quote. Fixture values only, on a real BacktestNode.
"""

from __future__ import annotations

from nautilus_trader.model import OrderSide, OrderStatus
from test_b_lifecycle import quote, run_node, trade
from test_b_lifecycle_entry import at, generation, house
from test_b_lifecycle_protect import NATIVE, Owner, held, trails


class Holder(Owner):
    actions = ((at("0.5"), "OWNER-1", OrderSide.BUY, 100, "0.6000"),)  # fills on the quote


def test_a_quote_only_name_pages_one_denial_and_arms_on_the_first_trade(tmp_path):
    quotes = [quote(at(s), "0.5050", "0.5100") for s in ("0", "1", "2", "3", "4")]
    journal, _, orders = run_node(
        tmp_path,
        lambda pub: [(at("-1"), generation(pub))],
        [trade(at("6"), "0.5100")],  # the name's first print comes after the quotes
        quotes=quotes,
        cls=Holder,
        start=at("-2"),
        generation_age=2,
        native_trail_fraction=NATIVE,
    )
    assert held(orders) == 100
    denied = [o for o in trails(orders) if o.status is OrderStatus.DENIED]
    pages = [r for r in journal if r.get("kind") == "page" and "denied" in str(r.get("reason"))]
    assert len(denied) == 1, f"{len(denied)} trails denied over {len(quotes)} quotes"
    assert len(pages) == 1
    assert len([r for r in journal if r.get("kind") == "protect"]) == 2  # denied, then placed
    (working,) = [o for o in house(orders) if o in trails(orders) and o.is_open]
    assert working.quantity.as_decimal() == 100 and working.ts_init == at("6")
