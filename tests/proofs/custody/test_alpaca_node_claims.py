"""Node witness for the ruled paper configuration: claims plus scoped reconciliation.

Lead ruling 01:25 EST 2026-11-11 (options A and D; LEAD-2201 item 8): the Strategy
claims external orders for the house universe and reconciliation is limited to it.
Pinned NT d53849b engine behaviour, manager.rs:
- a claimed external order report becomes an order of the claiming strategy
  (handle_external_order, 4621-4653), its fills inferred from cumulative status;
- reports outside reconciliation_instrument_ids are skipped (3473-3491, 1314-1319,
  and 1485-1490 for unresolved positions);
- a position the cache does not explain is unresolved when generate_missing_orders
  is off (3726-3730, 1478-1600) and the node aborts startup;
- reconciliation_lookback_mins None is unbounded (node_config.rs:442-443); E5 u9 keeps it
  unbounded and pages the order listing past the venue's 500-row cap.
The no-claim witness in test_alpaca_node_owner.py is the control.
"""

from datetime import UTC, datetime
from uuid import UUID

from .alpaca_venue import Venue as FakeVenue
from .test_alpaca_node_owner import IID, _owner, _run_node, _seeded

NOW = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = "2026-10-01T15:00:00Z"  # Older than every owner order after it.


def _later(index):
    return f"2026-10-02T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}Z"


def _holding(venue, symbol, qty):
    venue.positions.append(
        {
            "asset_id": str(UUID(int=100 + len(venue.positions))),
            "symbol": symbol,
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "0.1234",
            "qty": qty,
            "side": "long",
            "cost_basis": "1.1106",
        }
    )


def _filled(venue, name, qty, submitted):
    return _owner(
        venue,
        name,
        qty=qty,
        status="filled",
        filled_qty=qty,
        filled_avg_price="0.1234",
        submitted_at=submitted,
    )


def _owner_writes(venue, seeded, *rows):
    ids = {row["id"] for row in rows}
    return [r for r in venue.requests[seeded:] if r[0] != "GET" and r[1].rsplit("/", 1)[-1] in ids]


def _strategy_position(seen):
    return sum(p.quantity.as_decimal() for p in seen["positions"] if p.instrument_id == IID)


def test_claimed_open_owner_limit_and_in_window_fill_match_the_venue_position():
    venue = FakeVenue()
    working = _owner(venue, "owner-open", submitted_at=NOW)
    filled = _filled(venue, "owner-filled", "9", NOW)
    _holding(venue, "TEST", "9")
    seeded = _seeded(venue)
    seen = _run_node(venue, claims=[IID])
    assert "error" not in seen, seen.get("error")
    # Claimed: both owner orders become orders of the claiming strategy (4621-4653).
    assert {str(o.client_order_id) for o in seen["orders"]} == {"owner-open", "owner-filled"}
    assert all(o.strategy_id == seen["strategy"] for o in seen["orders"])
    assert str(_strategy_position(seen)) == "9", "Strategy sees the venue position"
    assert not _owner_writes(venue, seeded, working, filled)
    assert all(r[0] == "GET" for r in venue.requests[seeded:])


def test_owner_holding_older_than_any_window_on_a_long_history_starts_and_matches():
    # E5 u9: the lookback stays unbounded and the listing pages past 500 rows, so the
    # oldest owner fill still explains the holding (manager.rs:4621-4653, 1478-1600).
    venue = FakeVenue()
    filled = _filled(venue, "owner-old", "9", OLD)
    history = [
        _owner(venue, f"owner-past-{i}", status="canceled", submitted_at=_later(i))
        for i in range(600)
    ]
    _holding(venue, "TEST", "9")
    seeded = _seeded(venue)
    seen = _run_node(venue, claims=[IID])
    assert "error" not in seen, seen.get("error")
    assert str(_strategy_position(seen)) == "9", "Strategy sees the venue position"
    pages = [p for m, path, p in venue.requests[seeded:] if (m, path) == ("GET", "/orders")]
    assert any("until" in p for p in pages), "the listing paged past the venue cap"
    assert not _owner_writes(venue, seeded, filled, *history)
    assert all(r[0] == "GET" for r in venue.requests[seeded:])


def test_owner_holding_outside_the_house_universe_is_skipped_and_startup_proceeds():
    venue = FakeVenue()
    _holding(venue, "OTHER", "5")  # OTHER is neither loaded nor in the universe.
    seeded = _seeded(venue)
    seen = _run_node(venue, claims=[IID])
    assert "error" not in seen, seen.get("error")
    assert seen["positions"] == [] and str(_strategy_position(seen)) == "0"
    client = seen["clients"][0]
    assert [row["symbol"] for row, _ in client.withheld_positions] == ["OTHER"]
    assert all(r[0] == "GET" for r in venue.requests[seeded:])


def test_owner_bracket_refuses_startup_naming_it_and_is_never_a_house_order():
    venue = FakeVenue()
    bracket = _owner(venue, "owner-bracket", order_class="bracket", legs=[], submitted_at=NOW)
    seeded = _seeded(venue)
    seen = _run_node(venue, claims=[IID])
    assert "error" in seen, "an owner bracket is outside the managed-paper contract"
    assert "orders" not in seen
    failures = seen["clients"][0].failures
    assert any(row is not None and row.get("id") == bracket["id"] for row, _ in failures)
    assert any("owner-bracket" in str(error) for _, error in failures)
    assert not _owner_writes(venue, seeded, bracket)
